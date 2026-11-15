"""Music: TIDAL and the house's music libraries on its Sonos and WiiM speakers,
alone or grouped, through Music Assistant (oh_music), which only the gateway
drives.

The contract names the speakers (oh_site_contract.music). Each is matched to
its Music Assistant player as Music Assistant names it: a Sonos by its UDN,
a WiiM by the MAC it reports, else its address. A player no speaker matches
is never shown or commanded, nor is a protocol player (the AirPlay side of a
WiiM), which players/all omits. Every Music Assistant value a client sees is
projected here to known fields, each checked and bounded; none of its JSON,
errors or URLs is forwarded. Its free text (titles, names) is TEXT, which a
Guest sees only while Guests may see free text (security.guests_see_text).

A command on a speaker is RuntimeAdapter.dispatch_device's: any principal
sends it; each routed room that owns the speaker, locked, refuses it; a room
that shares it refuses only what disturbs its held route (resource
device:<speaker>); either refusal is device_locked. A command that reaches a
group (playing to, or the volume of, a group, and grouping) checks every
speaker in it. Each command is one trail line: its action, its speaker and a
plain value, never a search, a title or a reference.

A group member plays its leader's queue (Music Assistant would pull a member
it plays to out of its group), so play and transport on a member reach its
leader, and the answer says which speaker's queue it was.
"""

import math
import re
import threading
import time
import weakref
from pathlib import Path

from oh_music.client import Client, Failed, Unauthorized, Unavailable
from oh_music import provision, tidal

from . import trail
from .runtime import SENSITIVE_TEXT
from .services import Problem

#: Free text from Music Assistant, as oh_site_contract.TEXT: printable,
#: without a credential word, cut to its length.
TEXT_LENGTH = 192
#: A media item's kinds a client may name; folder only for a library's or
#: TIDAL's folders.
KINDS = ("track", "album", "artist", "playlist")
REF = re.compile(r"([a-z][a-z0-9_]{0,63}(?:--[A-Za-z0-9]{1,32})?)://(track|album|artist|playlist|folder)/(.*)", re.S)
REF_LENGTH = 1024
#: A reference's ID, by its provider: Music Assistant's library numbers its
#: items; TIDAL's are numbers or UUIDs, and its folders words; a library's
#: are paths relative to it (oh_identity.music.valid_id).
LIBRARY_ID = re.compile(r"[0-9]{1,20}")
TIDAL_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
TIDAL_FOLDER = re.compile(r"(?:[A-Za-z0-9_-]{1,64}(?:/[A-Za-z0-9_-]{1,64}){0,7})?")
ART = re.compile(r"[0-9a-f]{64}")
ART_URL = re.compile(r"/imageproxy/([0-9a-f]{64})(?:[?/]|$)")
#: The art sizes a client may ask for, of Music Assistant's.
ART_SIZES = ("80", "160", "256", "512")
#: A search's query length, and results per kind.
QUERY_LENGTH = 100
SEARCH_LIMIT = 25
#: Items of one page of a browse or a queue, at most and by default.
PAGE = 200
DEFAULT_PAGE = 100
OFFSET = 100000
#: Seconds the gateway waits for a group to show its members after it asks.
GROUP_SETTLE = 3.0
#: Player types that are a speaker of their own (not a group or protocol player).
PLAYER_TYPES = ("player", "stereo_pair")
STATES = ("playing", "paused", "idle")
REPEAT = ("off", "one", "all")
OPTIONS = ("replace", "next", "add")
TRANSPORT = ("play", "pause", "stop", "next", "previous", "seek", "shuffle", "repeat")
#: Seconds of one seek, at most: a day.
SEEK = 24 * 3600
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_music = weakref.WeakKeyDictionary()


def text(value):
    """Music Assistant free text a client may see: printable, without a
    credential word, cut to TEXT_LENGTH; else None."""
    if not isinstance(value, str):
        return None
    value = value.strip()[:TEXT_LENGTH].strip()
    if not value or not value.isprintable() or SENSITIVE_TEXT.search(value):
        return None
    return value


def number(value, high):
    return value if type(value) in (int, float) and math.isfinite(value) and 0 <= value <= high else None


def mac(value):
    """A MAC address as one form: lowercase, colon-separated."""
    if not isinstance(value, str):
        return None
    digits = re.sub(r"[:-]", "", value).lower()
    return ":".join(digits[i:i + 2] for i in range(0, 12, 2)) if re.fullmatch(r"[0-9a-f]{12}", digits) else None


def art(raw):
    """The image proxy ID of an item, from its image or its first image."""
    images = [raw.get("image")] + list((raw.get("metadata") or {}).get("images") or ())[:8]
    for image in images:
        proxy = image.get("proxy_id") if isinstance(image, dict) else None
        if isinstance(proxy, str) and ART.fullmatch(proxy):
            return proxy
    return None


class Sources:
    """Music Assistant's music providers thermite knows: TIDAL's instances
    and each library's filesystem_local instance (provision.LIBRARY_NAME)."""

    def __init__(self, providers, libraries):
        names = {provision.LIBRARY_NAME.format(name): name for name in libraries}
        self.tidal, self.libraries, self.available = [], {}, {}
        for entry in providers or ():
            if not isinstance(entry, dict) or not isinstance(entry.get("instance_id"), str):
                continue
            instance, available = entry["instance_id"], entry.get("available") is True
            if entry.get("domain") == "tidal":
                self.tidal.append(instance)
            elif entry.get("domain") == "filesystem_local" and entry.get("name") in names:
                self.libraries[instance] = names[entry["name"]]
            else:
                continue
            self.available[instance] = available
        self.tidal.sort()

    def known(self, provider):
        return provider == "library" or provider in self.available

    def summary(self, libraries):
        return {"tidal": any(self.available[instance] for instance in self.tidal),
                "libraries": {name: any(self.available[instance] for instance, each in self.libraries.items()
                                        if each == name) for name in libraries}}

    def source(self, provider):
        if provider in self.libraries:
            return {"source": "library", "library": self.libraries[provider]}
        return {"source": "tidal" if provider in self.available else "library"}


def valid_id(provider, kind, ident):
    """Whether an ID is one the provider's kind gives: a library item's
    number, TIDAL's ID or folder, or a path relative to a library without
    an empty, `.` or `..` part."""
    domain = provider.partition("--")[0]
    if domain == "library":
        return kind != "folder" and bool(LIBRARY_ID.fullmatch(ident))
    if domain == "tidal":
        return bool((TIDAL_FOLDER if kind == "folder" else TIDAL_ID).fullmatch(ident))
    if domain == "filesystem_local":
        return kind == "folder" and ident == "" or all(part not in ("", ".", "..") for part in ident.split("/"))
    return False


def parse_ref(value, sources=None):
    """(provider, kind, ID) of a reference a client names or Music Assistant
    returns; Problem("invalid_ref") unless it names a media item or a
    folder, of TIDAL, a library or Music Assistant's library, by an ID that
    provider gives (valid_id), and, given the sources, a provider instance
    thermite knows."""
    match = REF.fullmatch(value) if isinstance(value, str) and len(value) <= REF_LENGTH else None
    if not match or not value.isprintable() or not valid_id(*match.groups()):
        raise Problem("invalid_ref")
    provider, kind, ident = match.groups()
    if sources is not None and not sources.known(provider):
        raise Problem("invalid_ref")
    return provider, kind, ident


class Music:
    """The gateway's music for one adapter's contract. state is the gateway's
    private state, where the worker stores the claim (oh_music.provision);
    connect(port, token) opens a Client."""

    def __init__(self, adapter, state, *, connect=Client, settle=GROUP_SETTLE, clock=time.monotonic, sleep=time.sleep):
        self.adapter, self.spec = adapter, adapter.contract.music
        self.state, self.connect = Path(state), connect
        self.settle, self.clock, self.sleep = settle, clock, sleep
        speakers = self.spec["speakers"]
        self._udn = {spec["udn"]: ident for ident, spec in speakers.items() if spec["provider"] == "sonos"}
        self._mac = {mac(spec["mac"]): ident for ident, spec in speakers.items()
                     if spec["provider"] == "wiim" and "mac" in spec}
        self._ip = {spec["ip"]: ident for ident, spec in speakers.items() if spec["provider"] == "wiim" and "ip" in spec}
        self._client, self._guard = None, threading.Lock()
        self.flow = None

    # ---- Music Assistant ----------------------------------------------------

    def client(self):
        """A client with the stored token, reused while the token is."""
        claim = provision.read_claim(self.state)
        problem = provision.problem(claim, time.time())
        if problem:
            raise Problem(problem, 503)
        with self._guard:
            if self._client is None or self._client.token != claim["token"]:
                if self._client is not None:
                    self._client.close()
                self._client = self.connect(self.spec["port"], claim["token"])
            return self._client

    def call(self, command, **args):
        try:
            return self.client().call(command, **args)
        except (Unavailable, Unauthorized):
            raise Problem("music_unavailable", 503) from None
        except Failed:
            raise Problem("music_failed", 502) from None

    def sources(self):
        return Sources(self.call("providers"), self.spec["libraries"])

    def match(self, player):
        """The speaker a Music Assistant player is, or None."""
        if not isinstance(player, dict) or player.get("type", "player") not in PLAYER_TYPES:
            return None
        provider, ident = player.get("provider"), player.get("player_id")
        if provider == "sonos":
            return self._udn.get(ident)
        if provider != "wiim":
            return None
        identifiers = (player.get("device_info") or {}).get("identifiers") or {}
        found = mac(identifiers.get("mac_address"))
        if found in self._mac:
            return self._mac[found]
        speaker = self._ip.get(identifiers.get("ip_address"))
        # An address alone matches only a speaker with no MAC, or a player that reports none.
        return speaker if speaker and (found is None or "mac" not in self.spec["speakers"][speaker]) else None

    def matched(self, players):
        """{speaker: its player} of the players Music Assistant lists."""
        found = {}
        for player in players or ():
            speaker = self.match(player)
            if speaker and isinstance(player.get("player_id"), str) and speaker not in found:
                found[speaker] = player
        return found

    def players(self):
        return self.matched(self.call("players/all"))

    # ---- projection ---------------------------------------------------------

    def overview(self, players, queues, sources, *, see_text=True):
        """The speakers as a client sees them, with the sources thermite has:
        GET /api/v1/music and the snapshot of its event stream. players are
        matched ({speaker: player}); queues, Music Assistant's by queue ID."""
        ids = {player["player_id"]: speaker for speaker, player in players.items()}
        return {"version": 1, "available": True, "problem": None,
                "sources": sources.summary(self.spec["libraries"]),
                "speakers": [self.speaker(ident, players, ids, queues, see_text) for ident in self.spec["speakers"]]}

    def unavailable(self, problem):
        return {"version": 1, "available": False, "problem": problem, "sources": None,
                "speakers": [self.speaker(ident, {}, {}, {}, True) for ident in self.spec["speakers"]]}

    def leader(self, player, players, ids):
        """(the speaker whose queue this player plays, its group's speakers)."""
        own = player["player_id"]
        leader = player.get("synced_to") if isinstance(player.get("synced_to"), str) else own
        lead = players.get(ids.get(leader)) if leader != own else player
        members = (lead or {}).get("group_members") or []
        speakers = [ids[member] for member in members if isinstance(member, str) and member in ids]
        return ids.get(leader), speakers if len(members) > 1 else []

    def speaker(self, ident, players, ids, queues, see_text):
        spec = self.spec["speakers"][ident]
        view = {"id": ident, "label": spec["label"], "room": spec["room"], "room_label": spec["room_label"],
                "available": False, "state": "unavailable", "volume": None, "muted": None, "group": None,
                "now_playing": None, "shuffle": None, "repeat": None}
        player = players.get(ident)
        if not player or player.get("available") is not True:
            return view
        state = player.get("playback_state")
        volume = player.get("volume_level")
        view.update(available=True, state=state if state in STATES else "idle",
                    volume=volume if type(volume) is int and 0 <= volume <= 100 else None,
                    muted=player.get("volume_muted") if type(player.get("volume_muted")) is bool else None)
        leader, members = self.leader(player, players, ids)
        if members:
            lead = players.get(leader) or {}
            group_volume = lead.get("group_volume")
            view["group"] = {"leader": leader, "members": members,
                             "volume": group_volume if type(group_volume) is int and 0 <= group_volume <= 100 else None}
        queue = queues.get((players.get(leader) or player)["player_id"]) or {}
        if type(queue.get("shuffle_enabled")) is bool:
            view["shuffle"] = queue["shuffle_enabled"]
        if queue.get("repeat_mode") in REPEAT:
            view["repeat"] = queue["repeat_mode"]
        media = player.get("current_media")
        if isinstance(media, dict) and view["state"] != "idle":
            now = {"duration": number(media.get("duration"), 10 ** 6), "elapsed": number(media.get("elapsed_time"), 10 ** 6),
                   "at": number(media.get("elapsed_time_last_updated"), 10 ** 11)}
            # A jump (a seek) is reported on the queue, not the player.
            if (number(queue.get("elapsed_time_last_updated"), 10 ** 11) or 0) > (now["at"] or 0) and (
                    media.get("source_id") in (None, queue.get("queue_id"))):
                now.update(elapsed=number(queue.get("elapsed_time"), 10 ** 6), at=queue["elapsed_time_last_updated"])
            if see_text:
                found = ART_URL.search(media.get("image_url") or "") if isinstance(media.get("image_url"), str) else None
                now.update(title=text(media.get("title")), artist=text(media.get("artist")),
                           album=text(media.get("album")), art=found[1] if found else None)
            view["now_playing"] = now
        return view

    def item(self, raw, sources):
        """A search, browse or queue entry, or None for one thermite does not
        show: a radio, a podcast, a provider it does not know."""
        if not isinstance(raw, dict):
            return None
        kind = raw.get("media_type")
        try:
            if kind == "folder":
                instance, _, path = (raw.get("path") or "").partition("://")
                if not sources.known(instance) or raw.get("name") == "..":
                    return None
                ref = f"{instance}://folder/{path}"
            elif kind in KINDS:
                ref = raw.get("uri")
            else:
                return None
            provider, kind, _ = parse_ref(ref, sources)
        except Problem:
            return None
        entry = {"ref": ref, "kind": kind, "name": text(raw.get("name")), **sources.source(provider)}
        if kind != "folder":
            artists = [name for artist in (raw.get("artists") or [])[:5] if isinstance(artist, dict)
                       and (name := text(artist.get("name")))]
            album = raw.get("album")
            year = raw.get("year")
            entry.update(artists=artists, album=text(album.get("name")) if isinstance(album, dict) else None,
                         year=year if type(year) is int and 0 < year < 10000 else None,
                         duration=number(raw.get("duration"), 10 ** 6), art=art(raw))
        return entry

    # ---- reads --------------------------------------------------------------

    def read(self, see_text=True):
        """GET /api/v1/music."""
        try:
            sources = self.sources()
            players = self.players()
            queues = {queue["queue_id"]: queue for queue in self.call("player_queues/all") or ()
                      if isinstance(queue, dict) and isinstance(queue.get("queue_id"), str)}
        except Problem as problem:
            if problem.code in ("music_unavailable", "music_unclaimed"):
                return self.unavailable(problem.code)
            raise
        return self.overview(players, queues, sources, see_text=see_text)

    def search(self, query, kind, source):
        query = _CONTROL.sub("", query).strip()[:QUERY_LENGTH].strip()
        if not query or kind not in ("", *KINDS) or source not in ("", "library", "tidal"):
            raise Problem("invalid_query")
        sources = self.sources()
        providers = (["library"] if source != "tidal" else []) + (sources.tidal if source != "library" else [])
        kinds = [kind] if kind else list(KINDS)
        answer = {"query": query, "kind": kind or None, "source": source or None, **{each + "s": [] for each in KINDS}}
        if not providers:
            return answer
        found = self.call("music/search", search_query=query, media_types=kinds, limit=SEARCH_LIMIT, providers=providers)
        for each in kinds:
            listed = (found or {}).get(each + "s") if isinstance(found, dict) else None
            answer[each + "s"] = [entry for raw in (listed or [])[:SEARCH_LIMIT * len(providers)]
                                  if (entry := self.item(raw, sources)) and entry["kind"] == each]
        return answer

    def browse(self, ref, offset, limit):
        sources = self.sources()
        if ref is None:
            items = [{"ref": f"{instance}://folder/", "kind": "folder", "name": name, "source": "library",
                      "library": name} for instance, name in sorted(sources.libraries.items(), key=lambda pair: pair[1])]
            items += [{"ref": f"{instance}://folder/", "kind": "folder", "name": "TIDAL", "source": "tidal"}
                      for instance in sources.tidal]
        else:
            provider, kind, ident = parse_ref(ref, sources)
            if kind == "folder":
                listed = self.call("music/browse", path=f"{provider}://{ident}")
            elif kind == "track":
                raise Problem("invalid_query")
            else:
                command = {"album": "music/albums/album_tracks", "playlist": "music/playlists/playlist_tracks",
                           "artist": "music/artists/artist_albums"}[kind]
                listed = self.call(command, item_id=ident, provider_instance_id_or_domain=provider)
            items = [entry for raw in (listed if isinstance(listed, list) else [])[:OFFSET]
                     if (entry := self.item(raw, sources))]
        return {"ref": ref, "offset": offset, "limit": limit, "total": len(items), "items": items[offset:offset + limit]}

    def queue(self, speaker, offset, limit):
        self.known(speaker)
        players = self.players()
        player = self.available(speaker, players)
        ids = {each["player_id"]: ident for ident, each in players.items()}
        leader, _ = self.leader(player, players, ids)
        owner = players.get(leader, player)
        queue = self.call("player_queues/get", queue_id=owner["player_id"])
        listed = self.call("player_queues/items", queue_id=owner["player_id"], limit=limit, offset=offset)
        sources = self.sources()
        items = []
        for raw in listed if isinstance(listed, list) else ():
            if not isinstance(raw, dict):
                continue
            media = raw.get("media_item") if isinstance(raw.get("media_item"), dict) else {}
            entry = self.item(media, sources) or {"ref": None, "kind": "track", "name": None, "artists": [],
                                                  "album": None, "year": None, "duration": None, "art": None}
            index = raw.get("index")
            entry.update(index=index if type(index) is int and index >= 0 else None,
                         name=entry["name"] or text(raw.get("name")),
                         duration=entry.get("duration") or number(raw.get("duration"), 10 ** 6),
                         art=entry.get("art") or art(raw))
            items.append(entry)
        queue = queue if isinstance(queue, dict) else {}
        total, current = queue.get("items"), queue.get("current_index")
        return {"speaker": speaker, "queue": ids.get(owner["player_id"], speaker), "offset": offset, "limit": limit,
                "total": total if type(total) is int and total >= 0 else len(items),
                "current": current if type(current) is int and current >= 0 else None, "items": items}

    def image(self, proxy_id, size):
        try:
            client = self.client()
            return client.image(proxy_id, size)
        except (Unavailable, Unauthorized):
            raise Problem("music_unavailable", 503) from None
        except Failed:
            raise Problem("not_found", 404) from None

    # ---- commands -------------------------------------------------------------

    def known(self, speaker):
        if speaker not in self.spec["speakers"]:
            raise Problem("unknown_speaker", 404)

    def available(self, speaker, players):
        """A speaker's player; music_failed while Music Assistant has no
        available player for it."""
        self.known(speaker)
        player = players.get(speaker)
        if not player or player.get("available") is not True:
            raise Problem("music_failed", 502)
        return player

    def guarded(self, user, origin, speaker, action, value, involved, send):
        """send() under the locks and policy of every room that commands an
        involved speaker, with one trail line (dispatch_device's semantics)."""
        rooms = {}
        for ident in involved:
            for room, role in self.spec["speakers"][ident]["rooms"].items():
                rooms[room] = "own" if role == "own" or rooms.get(room) == "own" else "shared"
        resources = tuple("device:" + ident for ident in sorted(set(involved)))
        record = trail.Command(origin, user, room=None, device=speaker, action="music." + action, item=None, value=value)
        try:
            with self.adapter.command_lock(tuple(sorted(rooms)), resources):
                from .auth import revalidate
                # After the lock wait and before I/O, as a device's command.
                revalidate(user)
                for room, role in sorted(rooms.items()):
                    self.adapter.allowed(room, None if role == "own" else (None, action, value, resources), "device_locked")
                result = send()
        except BaseException as error:
            record.finish(error)
            raise
        record.finish()
        return result

    def target(self, speaker):
        """(the speaker's player, its queue's speaker and player, every speaker
        of its group) for a command on what it plays."""
        players = self.players()
        player = self.available(speaker, players)
        ids = {each["player_id"]: ident for ident, each in players.items()}
        leader, members = self.leader(player, players, ids)
        if leader is None or leader not in players:
            # A member of a group no speaker leads: it plays on its own.
            return player, speaker, player, [speaker]
        return player, leader, players[leader], members or [speaker]

    def play(self, user, speaker, ref, option, *, origin=None):
        self.known(speaker)
        if option not in OPTIONS:
            raise Problem("invalid_command")
        provider, kind, _ = parse_ref(ref, self.sources())
        if kind == "folder":
            raise Problem("invalid_ref")
        _, owner, lead, group = self.target(speaker)
        self.guarded(user, origin, speaker, "play", option, group,
                     lambda: self.call("player_queues/play_media", queue_id=lead["player_id"], media=ref, option=option))
        return {"accepted": True, "speaker": speaker, "action": "play", "queue": owner}

    def transport(self, user, speaker, action, value, *, origin=None):
        self.known(speaker)
        if action not in TRANSPORT:
            raise Problem("invalid_command")
        if action == "seek" and (type(value) is not int or not 0 <= value <= SEEK) or (
                action == "shuffle" and type(value) is not bool) or (action == "repeat" and value not in REPEAT) or (
                action not in ("seek", "shuffle", "repeat") and value is not None):
            raise Problem("invalid_command")
        _, owner, lead, group = self.target(speaker)
        queue = lead["player_id"]
        if action == "seek":
            command, args = "player_queues/seek", {"queue_id": queue, "position": value}
        elif action == "shuffle":
            command, args = "player_queues/shuffle", {"queue_id": queue, "shuffle_enabled": value}
        elif action == "repeat":
            command, args = "player_queues/repeat", {"queue_id": queue, "repeat_mode": value}
        else:
            command, args = "players/cmd/" + action, {"player_id": queue}
        shown = None if value is None else str(value).lower()
        self.guarded(user, origin, speaker, action, shown, group, lambda: self.call(command, **args))
        return {"accepted": True, "speaker": speaker, "action": action, "queue": owner}

    def volume(self, user, speaker, data, *, origin=None):
        self.known(speaker)
        if len(data) != 1 or set(data) - {"level", "group_level"}:
            raise Problem("invalid_command")
        (key, level), = data.items()
        if type(level) is not int or not 0 <= level <= 100:
            raise Problem("invalid_command")
        player, owner, lead, group = self.target(speaker)
        if key == "level":
            self.guarded(user, origin, speaker, "volume", level, [speaker],
                         lambda: self.call("players/cmd/volume_set", player_id=player["player_id"], volume_level=level))
        else:
            self.guarded(user, origin, speaker, "group_volume", level, group,
                         lambda: self.call("players/cmd/group_volume", player_id=lead["player_id"], volume_level=level))
        return {"accepted": True, "speaker": speaker, "action": key, "level": level}

    def group(self, user, speaker, members, *, origin=None):
        """Join members to the speaker's group, as its leader; which joined and
        which Music Assistant left out (a speaker it cannot group with this
        one, or one it has no player for), read back."""
        self.known(speaker)
        if (not isinstance(members, list) or not members or any(not isinstance(member, str) for member in members)
                or len(set(members)) != len(members) or speaker in members):
            raise Problem("invalid_command")
        if any(member not in self.spec["speakers"] for member in members):
            raise Problem("unknown_speaker", 404)
        players = self.players()
        leader = self.available(speaker, players)
        known = [member for member in members if (players.get(member) or {}).get("available") is True]
        if known:
            self.guarded(user, origin, speaker, "group", ",".join(members), [speaker, *members], lambda: self.call(
                "players/cmd/set_members", target_player=leader["player_id"],
                player_ids_to_add=[players[member]["player_id"] for member in known]))
        else:
            # Nothing Music Assistant can group: refused without a command, still traced.
            self.guarded(user, origin, speaker, "group", ",".join(members), [speaker, *members], lambda: None)
        joined = self.settled(speaker, known)
        return {"accepted": True, "speaker": speaker, "joined": [member for member in members if member in joined],
                "refused": [member for member in members if member not in joined]}

    def settled(self, speaker, wanted):
        """The speakers in the speaker's group once every wanted one is, or
        when GROUP_SETTLE seconds have passed: Music Assistant drops a member
        it cannot group without an error."""
        until = self.clock() + self.settle
        while True:
            players = self.players()
            ids = {each["player_id"]: ident for ident, each in players.items()}
            player = players.get(speaker)
            members = set(self.leader(player, players, ids)[1]) if player else set()
            if set(wanted) <= members or self.clock() >= until:
                return members
            self.sleep(0.25)

    def ungroup(self, user, speaker, *, origin=None):
        self.known(speaker)
        players = self.players()
        player = self.available(speaker, players)
        ids = {each["player_id"]: ident for ident, each in players.items()}
        _, members = self.leader(player, players, ids)
        self.guarded(user, origin, speaker, "ungroup", None, members or [speaker],
                     lambda: self.call("players/cmd/ungroup", player_id=player["player_id"]))
        return {"accepted": True, "speaker": speaker, "action": "ungroup"}

    # ---- TIDAL ----------------------------------------------------------------

    def tidal_status(self):
        """Whether TIDAL is signed in, and the sign-in this gateway started."""
        try:
            client = self.client()
            if self.flow is not None and self.flow["state"] == "waiting":
                self.flow = {**tidal.poll(client, self.flow["flow"]), "started": self.flow["started"]}
            signed_in = tidal.signed_in(client)
        except (Unavailable, Unauthorized):
            raise Problem("music_unavailable", 503) from None
        except Failed:
            raise Problem("music_failed", 502) from None
        sign_in = None
        if self.flow is not None:
            sign_in = {key: self.flow.get(key) for key in ("state", "code", "url", "expires_at")}
        return {"signed_in": signed_in, "sign_in": sign_in}

    def tidal_start(self):
        try:
            client = self.client()
            if self.flow is not None and self.flow["state"] == "waiting":
                tidal.cancel(client, self.flow["flow"])
            self.flow = {**tidal.start(client), "started": time.time()}
        except (Unavailable, Unauthorized):
            raise Problem("music_unavailable", 503) from None
        except Failed:
            raise Problem("music_failed", 502) from None
        return self.tidal_status()

    def tidal_finish(self):
        """End this gateway's sign-in: cancelled in Music Assistant while it
        still waits. The answer is the status it ended with."""
        if self.flow is None:
            raise Problem("tidal_not_started", 409)
        answer = self.tidal_status()
        if self.flow["state"] == "waiting":
            try:
                tidal.cancel(self.client(), self.flow["flow"])
            except (Unavailable, Unauthorized):
                raise Problem("music_unavailable", 503) from None
            answer["sign_in"]["state"] = "cancelled"
        self.flow = None
        return answer


def music_for(adapter):
    """The adapter's music; music_not_configured for a host without it."""
    if adapter.contract.music is None:
        raise Problem("music_not_configured", 404)
    music = _music.get(adapter)
    if music is None:
        from django.conf import settings
        music = _music[adapter] = Music(adapter, settings.STATE)
    return music


def install(adapter, music):
    """Use this music for the adapter (tests)."""
    _music[adapter] = music
