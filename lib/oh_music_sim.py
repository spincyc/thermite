"""Loopback Music Assistant 2.10.12 stand-in: what the gateway and the worker
use of it, with the state and the semantics they rely on.

It serves GET /info, POST /setup (the first admin), POST /api (one command,
bearer token), GET /ws (server info first, auth, then every event and
commands, with pings the client must answer) and GET /imageproxy/<id>
(generated JPEG or PNG covers). Its players are built from a house: a Sonos
by its UDN, a WiiM by its MAC and address, as Music Assistant names them.

What it models, as Music Assistant does: queue options (replace, play,
next, replace_next, add), a group plays its leader's queue, playing to a
member pulls it out of its group, set_members silently drops a speaker the
target cannot group with (a WiiM Amp Pro has no AirPlay, so it joins only
WiiMs; a Sonos and a WiiM Mini group over AirPlay, with the airplay provider
loaded), a group's volume is its loudest member's and moves proportionally,
skip seeks, a command to an unknown player succeeds and does nothing, a
failure is a bare 500, protocol players are hidden from players/all, a
revoked token's websockets close, provider setup flows (zero-input, form,
TIDAL's device login) and a single-instance provider's already_configured.

What it does not: audio, discovery, library sync, metadata, translations,
radio and podcasts, persistent group players, more than one user, rate
limits, or any field thermite does not read. Its catalog is invented.
"""

from __future__ import annotations

import base64
import collections
import hashlib
import http.server
import json
import re
import secrets
import socket
import struct
import threading
import time
import urllib.parse
import zlib

VERSION = "2.10.12"
#: Music Assistant's error codes, as its websocket names them.
MEDIA_NOT_FOUND, INVALID_DATA, PLAYER_UNAVAILABLE, INVALID_COMMAND, AUTHENTICATION_FAILED = 2, 3, 10, 12, 21
SIZES = (0, 80, 160, 256, 512, 1024)
#: WiiM models without AirPlay, which group only with WiiMs.
NO_AIRPLAY = ("Amp Pro",)
#: Invented music: (artist, album, year, [(title, seconds)]). The libraries'
#: albums are the first library's files and Music Assistant's library;
#: TIDAL's appear once it is signed in.
LIBRARY = (("Example Quartet", "First Light", 2019, (("Opening", 241), ("Morning Rain", 198), ("Long Road", 305))),
           ("Documentation Band", "Reserved Range", 2021, (("Example Net", 222), ("Test Pattern", 187))))
TIDAL = (("Placeholder Orchestra", "Sample Suite", 2023, (("Overture", 260), ("Interlude", 140), ("Finale", 331))),
         ("Fictional Duo", "Not Real", 2020, (("Imaginary", 214), ("Made Up", 199))),
         ("Placeholder Orchestra", "Encore", 2024, (("Encore", 188),), "single"))
TIDAL_PLAYLIST = "Example Mix"
TIDAL_PLAYLIST_ID = "5f3a8c2e-1b4d-4e6f-9a7b-0c2d4e6f8a1b"
#: RFC 6455's handshake GUID, and the opcodes the stand-in uses.
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
CONTINUATION, TEXT, CLOSE, PING, PONG = 0x0, 0x1, 0x8, 0x9, 0xA


class Error(Exception):
    """A Music Assistant error: its code; HTTP answers it with a bare 500,
    or 400 for invalid data or an invalid command."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def jpeg(rgb, size):
    """A baseline JPEG of one colour, size x size (a multiple of 8)."""
    r, g, b = rgb
    levels = (0.299 * r + 0.587 * g + 0.114 * b, 128 - 0.168736 * r - 0.331264 * g + 0.5 * b,
              128 + 0.5 * r - 0.418688 * g - 0.081312 * b)
    # Quantization 1 everywhere: a flat block's DC coefficient is 8 x (level - 128).
    dcs = [max(-1023, min(1023, round(8 * (level - 128)))) for level in levels]

    def segment(marker, payload):
        return b"\xff" + bytes([marker]) + struct.pack(">H", len(payload) + 2) + payload
    # The standard luminance DC table, and an AC table of end-of-block alone.
    dc_counts = bytes((0, 1, 5, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0))
    dc_codes, code, category = {}, 0, 0
    for length, count in enumerate(dc_counts, 1):
        for _ in range(count):
            dc_codes[category] = (code, length)
            code, category = code + 1, category + 1
        code <<= 1
    bits = []

    def put(value, length):
        bits.extend((value >> (length - 1 - i)) & 1 for i in range(length))
    previous = [0, 0, 0]
    for _ in range((size // 8) ** 2):
        for component in range(3):
            diff, previous[component] = dcs[component] - previous[component], dcs[component]
            category = abs(diff).bit_length()
            put(*dc_codes[category])
            if category:
                put(diff if diff > 0 else diff + (1 << category) - 1, category)
            put(0, 1)
    bits.extend([1] * (-len(bits) % 8))
    data = bytearray()
    for i in range(0, len(bits), 8):
        data.append(int("".join(map(str, bits[i:i + 8])), 2))
        if data[-1] == 0xFF:
            data.append(0)
    return (b"\xff\xd8" + segment(0xE0, b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00")
            + segment(0xDB, b"\x00" + b"\x01" * 64)
            + segment(0xC0, b"\x08" + struct.pack(">HH", size, size) + b"\x03\x01\x11\x00\x02\x11\x00\x03\x11\x00")
            + segment(0xC4, b"\x00" + dc_counts + bytes(range(12)))
            + segment(0xC4, b"\x10\x01" + bytes(15) + b"\x00")
            + segment(0xDA, b"\x03\x01\x00\x02\x00\x03\x00\x00\x3f\x00") + bytes(data) + b"\xff\xd9")


def frame(opcode, payload=b""):
    """One unmasked server frame, whole."""
    size = len(payload)
    length = bytes([size]) if size < 126 else bytes([126]) + struct.pack(">H", size) if size < 65536 else (
        bytes([127]) + struct.pack(">Q", size))
    return bytes([0x80 | opcode]) + length + payload


class Frames:
    """A websocket client's frames as they arrive: (opcode, final, payload).
    A client must mask every frame; one that does not is a protocol error."""

    def __init__(self):
        self.buffer = bytearray()

    def feed(self, data):
        self.buffer += data
        while len(self.buffer) >= 2:
            first, second = self.buffer[0], self.buffer[1]
            if not second & 0x80:
                raise ValueError("unmasked client frame")
            size, start = second & 0x7F, 2
            if size == 126:
                size, start = (struct.unpack(">H", self.buffer[2:4])[0], 4) if len(self.buffer) >= 4 else (None, 0)
            elif size == 127:
                size, start = (struct.unpack(">Q", self.buffer[2:10])[0], 10) if len(self.buffer) >= 10 else (None, 0)
            if size is None or len(self.buffer) < start + 4 + size:
                return
            mask = self.buffer[start:start + 4]
            payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(self.buffer[start + 4:start + 4 + size]))
            del self.buffer[:start + 4 + size]
            yield first & 0x0F, bool(first & 0x80), payload


def png(rgb, size):
    """A PNG of one colour, size x size."""
    def chunk(kind, payload):
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload))
    rows = b"".join(b"\x00" + bytes(rgb) * size for _ in range(size))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


def speakers_from_house(data):
    """The house's speakers as the stand-in builds players from them:
    oh_site_contract.music's match keys, with each device's model."""
    import oh_site_contract
    from oh_conf import House
    house = House(data)
    spec = oh_site_contract.music(house, {}, {"port": 1024, "libraries": []})["speakers"]
    models = {device["id"]: device.get("model") or "" for device in house.devices}
    return [{"id": ident, **speaker, "model": models[ident]} for ident, speaker in spec.items()]


class Sim:
    """Music Assistant's state. speakers: speakers_from_house's. preloaded:
    providers it starts with, as a fresh server loads some by default.
    heartbeat: seconds between its websocket pings. A test makes it fail:
    failing counts the next commands of a name to answer a bare 500,
    refusing the next authenticated API requests to answer 401, as Music
    Assistant does when its authentication fails inside, limiting the next
    logins to answer 401 whatever the password, as its rate limiter does
    after failed ones, and losing the next answers to a path (/setup): it
    acts, then drops the connection."""

    def __init__(self, speakers=(), *, preloaded=("chromecast", "dlna", "smart_fades"), heartbeat=25.0):
        self.lock = threading.RLock()
        self.port = 0
        self.status = "running"
        self.heartbeat = heartbeat
        self.users = {}
        self.tokens = {}
        self.onboard_done = False
        self.remote_access = False
        self.providers = {}
        self.flows = {}
        self.players = {}
        self.queues = {}
        self.peers = set()
        self.calls = []
        self.failing = collections.Counter()
        self.refusing = 0
        self.limiting = 0
        self.losing = collections.Counter()
        self.events = []
        self.speakers = list(speakers)
        self.tidal_code = None
        self.images = {}
        for domain in preloaded:
            self._create(domain)
        self.server_id = secrets.token_hex(16)

    # ---- providers ------------------------------------------------------------

    def _create(self, domain, values=None, *, instance=None):
        multi = domain in ("filesystem_local", "tidal")
        instance = instance or (f"{domain}--{secrets.token_hex(4)}" if multi else domain)
        kind = "music" if domain in ("filesystem_local", "tidal", "ambient_sounds") else "player"
        self.providers[instance] = {"type": kind, "domain": domain, "instance_id": instance, "name": None,
                                    "enabled": True, "values": dict(values or {})}
        if domain in ("sonos", "wiim"):
            for speaker in self.speakers:
                if speaker["provider"] == domain:
                    self._add_player(speaker)
        self._providers_updated()
        return instance

    def _providers_updated(self):
        self.emit("providers_updated", None, self.provider_list())

    def provider_list(self):
        return [{"type": entry["type"], "domain": entry["domain"], "instance_id": instance,
                 "name": entry["name"] or self._default_name(entry["domain"]), "supported_features": [],
                 "available": entry["enabled"], "lookup_key": instance} for instance, entry in self.providers.items()]

    @staticmethod
    def _default_name(domain):
        return {"filesystem_local": "Filesystem (local disk)", "tidal": "Tidal"}.get(domain, domain.title())

    def library_instances(self):
        """{instance: path} of the filesystem_local instances, in creation order."""
        return {instance: entry["values"].get("path") for instance, entry in self.providers.items()
                if entry["domain"] == "filesystem_local"}

    def tidal_instance(self):
        return next((instance for instance, entry in self.providers.items() if entry["domain"] == "tidal"), None)

    # ---- players --------------------------------------------------------------

    def _add_player(self, speaker):
        if speaker["provider"] == "sonos":
            player_id = speaker["udn"]
            identifiers = {"mac_address": speaker.get("mac"), "ip_address": speaker.get("ip"), "uuid": player_id}
            manufacturer = "Sonos"
        else:
            player_id = "wiim_uuid:" + hashlib.sha256(speaker["id"].encode()).hexdigest()[:32]
            identifiers = {"mac_address": (speaker.get("mac") or "").upper() or None, "ip_address": speaker.get("ip"),
                           "uuid": player_id[5:]}
            manufacturer = "Linkplay"
        self.players[player_id] = {
            "player_id": player_id, "provider": speaker["provider"], "type": "player", "name": speaker["label"],
            "available": True, "enabled": True, "powered": True, "playback_state": "idle", "volume_level": 20,
            "volume_muted": False, "group_volume": 20, "group_members": [], "synced_to": None, "active_group": None,
            "supported_features": ["volume_set", "set_members", "pause", "seek"], "current_media": None,
            "device_info": {"model": speaker.get("model") or "", "manufacturer": manufacturer, "identifiers": identifiers},
            "airplay": speaker["provider"] == "sonos" or not any(word in (speaker.get("model") or "")
                                                                 for word in NO_AIRPLAY)}
        self.queues[player_id] = self._fresh_queue(player_id, speaker["label"])
        if speaker["provider"] == "wiim" and self.players[player_id]["airplay"]:
            # Its AirPlay side: a protocol player Music Assistant hides.
            hidden = "ap" + hashlib.sha256(player_id.encode()).hexdigest()[:12]
            self.players[hidden] = {**self.players[player_id], "player_id": hidden, "provider": "airplay",
                                    "type": "protocol", "airplay": True}
        self.emit("player_added", player_id, self.public(self.players[player_id]))

    @staticmethod
    def _fresh_queue(queue_id, name):
        return {"queue_id": queue_id, "active": False, "display_name": name, "available": True, "items": 0,
                "shuffle_enabled": False, "repeat_mode": "off", "current_index": None, "state": "idle",
                "current_item": None, "elapsed_time": 0, "elapsed_time_last_updated": time.time(), "list": []}

    def add_player(self, player_id, provider, name, **identifiers):
        """A player no speaker of the house is (a test's stranger)."""
        with self.lock:
            speaker = {"id": name, "provider": provider, "label": name, "udn": player_id, **identifiers}
            self._add_player(speaker)
            return player_id

    def public(self, player):
        return {key: value for key, value in player.items() if key != "airplay"}

    def queue_public(self, queue):
        return {key: value for key, value in queue.items() if key != "list"}

    def player(self, player_id):
        player = self.players.get(player_id)
        if not player or player["type"] == "protocol":
            raise Error(PLAYER_UNAVAILABLE)
        return player

    def compatible(self, one, other):
        if one["provider"] == other["provider"]:
            return True
        return one["airplay"] and other["airplay"] and "airplay" in self.providers

    def leader_of(self, player):
        return self.players[player["synced_to"]] if player["synced_to"] in self.players else player

    def _changed(self, *players):
        for player in {player["player_id"]: player for player in players}.values():
            lead = self.leader_of(player)
            members = [self.players[member] for member in lead["group_members"] if member in self.players] or [lead]
            lead["group_volume"] = max(member["volume_level"] for member in members)
            if player is not lead:
                player.update(playback_state=lead["playback_state"], current_media=lead["current_media"])
            self.emit("player_updated", player["player_id"], self.public(player))

    def _detach(self, player):
        """Take a player out of the group it is in; a leader leaves its queue
        to the next member."""
        if player["synced_to"]:
            lead = self.players[player["synced_to"]]
            lead["group_members"] = [member for member in lead["group_members"] if member != player["player_id"]]
            if len(lead["group_members"]) < 2:
                lead["group_members"] = []
            player.update(synced_to=None, playback_state="idle", current_media=None)
            self._changed(player, lead)
        elif player["group_members"]:
            rest = [self.players[member] for member in player["group_members"] if member != player["player_id"]]
            heir = rest[0]
            heir.update(synced_to=None, group_members=[each["player_id"] for each in rest] if len(rest) > 1 else [],
                        playback_state=player["playback_state"], current_media=player["current_media"])
            self.queues[heir["player_id"]] = {**self.queues[player["player_id"]], "queue_id": heir["player_id"],
                                              "display_name": heir["name"]}
            for each in rest[1:]:
                each["synced_to"] = heir["player_id"]
            player.update(group_members=[], playback_state="idle", current_media=None)
            self.queues[player["player_id"]] = self._fresh_queue(player["player_id"], player["name"])
            self._queue_updated(player["player_id"])
            self._changed(player, *rest)
            self._queue_updated(heir["player_id"])

    def set_members(self, target_player, player_ids_to_add=None, player_ids_to_remove=None):
        lead = self.player(target_player)
        if lead["synced_to"]:
            self._detach(lead)
        for player_id in player_ids_to_remove or ():
            if player_id == target_player and lead["group_members"]:
                self._detach(lead)
            elif player_id in lead["group_members"]:
                self._detach(self.players[player_id])
        for player_id in player_ids_to_add or ():
            member = self.players.get(player_id)
            if not member or member["type"] == "protocol" or player_id == target_player:
                continue
            if not all(self.compatible(member, self.players[each]) for each in lead["group_members"] or [target_player]):
                continue  # Dropped without an error, as Music Assistant does.
            if member["synced_to"] or member["group_members"]:
                self._detach(member)
            lead["group_members"] = (lead["group_members"] or [target_player]) + [player_id]
            member["synced_to"] = target_player
            self._changed(member)
        self._changed(lead)

    def ungroup(self, player_id):
        player = self.players.get(player_id)
        if player and player["group_members"] and not player["synced_to"]:
            # A leader: its group dissolves, and it plays on.
            for member in player["group_members"]:
                if member != player_id:
                    self._detach(self.players[member])
        elif player:
            self._detach(player)

    def volume_set(self, player_id, volume_level):
        player = self.player(player_id)
        player["volume_level"] = max(0, min(100, int(volume_level)))
        self._changed(player, self.leader_of(player))

    def group_volume(self, player_id, volume_level):
        lead = self.leader_of(self.player(player_id))
        members = [self.players[member] for member in lead["group_members"]] or [lead]
        level, current = max(0, min(100, int(volume_level))), lead["group_volume"]
        for member in members:
            member["volume_level"] = level if not current else max(0, min(100, round(member["volume_level"] * level / current)))
        self._changed(*members)

    # ---- queues ---------------------------------------------------------------

    def _queue_updated(self, queue_id):
        queue = self.queues[queue_id]
        queue["items"] = len(queue["list"])
        index = queue["current_index"]
        queue["current_item"] = queue["list"][index] if index is not None and index < len(queue["list"]) else None
        self.emit("queue_updated", queue_id, self.queue_public(queue))

    def _start(self, queue_id, index):
        queue, player = self.queues[queue_id], self.players[queue_id]
        if not queue["list"]:
            return
        index = max(0, min(index, len(queue["list"]) - 1))
        item = queue["list"][index]
        now = time.time()
        queue.update(current_index=index, state="playing", active=True, elapsed_time=0, elapsed_time_last_updated=now)
        track = item["media_item"]
        player.update(playback_state="playing", current_media={
            "uri": track["uri"], "media_type": "track", "title": track["name"],
            "artist": " / ".join(artist["name"] for artist in track["artists"]), "album": track["album"]["name"],
            "image_url": f"http://127.0.0.1:{self.port}/imageproxy/{track['album']['image']['proxy_id']}?size=512&fmt=jpeg",
            "duration": track["duration"], "source_id": queue_id, "queue_item_id": item["queue_item_id"],
            "elapsed_time": 0, "elapsed_time_last_updated": now})
        self._changed(player, *[self.players[member] for member in player["group_members"] if member != queue_id])
        self._queue_updated(queue_id)

    def play_media(self, queue_id, media, option=None, start_item=None, shuffle=None, **_):
        player = self.player(queue_id)
        if player["synced_to"]:
            # Playing to a member pulls it out of its group.
            self._detach(player)
        tracks = []
        for uri in media if isinstance(media, list) else [media]:
            tracks += self.tracks(uri)
        if start_item is not None:
            # From the track named, by its URI or ID; none named, none at all.
            at = next((index for index, track in enumerate(tracks) if start_item in (track["uri"], track["item_id"])), None)
            tracks = [] if at is None else tracks[at:]
        option = option or ("play" if "://track/" in str(media) else "replace")
        queue = self.queues[queue_id]
        if shuffle is not None:
            queue["shuffle_enabled"] = bool(shuffle)
            if shuffle:
                tracks = tracks[::-1]  # Shuffled, as a test can tell.
        items = [{"queue_item_id": secrets.token_hex(16), "name": track["artists"][0]["name"] + " - " + track["name"],
                  "duration": track["duration"], "media_item": track, "image": track["album"]["image"],
                  "available": True} for track in tracks]
        current = queue["current_index"]
        after = 0 if current is None else current + 1
        if option == "replace":
            queue["list"] = items
            self._renumber(queue)
            self._start(queue_id, 0)
            return
        if option == "replace_next":
            queue["list"] = queue["list"][:after] + items
        elif option == "add":
            queue["list"] = queue["list"] + items
        elif option in ("next", "play"):
            queue["list"] = queue["list"][:after] + items + queue["list"][after:]
        else:
            raise Error(INVALID_DATA)
        self._renumber(queue)
        if option == "play" or option == "add" and current is None:
            self._start(queue_id, after if option == "play" else 0)
        else:
            self._queue_updated(queue_id)

    @staticmethod
    def _renumber(queue):
        for index, item in enumerate(queue["list"]):
            item["index"] = index

    def transport(self, command, player_id):
        player = self.leader_of(self.player(player_id))
        queue_id = player["player_id"]
        queue = self.queues[queue_id]
        if command == "play":
            if player["playback_state"] == "paused":
                player["playback_state"], queue["state"] = "playing", "playing"
            elif player["playback_state"] == "idle" and queue["list"]:
                return self._start(queue_id, queue["current_index"] or 0)
        elif command == "pause" and player["playback_state"] == "playing":
            player["playback_state"], queue["state"] = "paused", "paused"
        elif command == "stop":
            player["playback_state"], queue["state"] = "idle", "idle"
        elif command in ("next", "previous") and queue["list"]:
            index = (queue["current_index"] or 0) + (1 if command == "next" else -1)
            if queue["repeat_mode"] == "all":
                index %= len(queue["list"])
            return self._start(queue_id, max(0, min(index, len(queue["list"]) - 1)))
        self._changed(player, *[self.players[member] for member in player["group_members"] if member != queue_id])
        self._queue_updated(queue_id)

    def seek(self, queue_id, position):
        queue = self.queues[self.leader_of(self.player(queue_id))["player_id"]]
        queue.update(elapsed_time=max(0, int(position)), elapsed_time_last_updated=time.time())
        # A jump is reported on the queue; the player's position is not.
        self.emit("queue_time_updated", queue["queue_id"], queue["elapsed_time"])

    # ---- the catalog ----------------------------------------------------------

    def _image(self, key):
        proxy = hashlib.sha256(key.encode()).hexdigest()
        self.images[proxy] = tuple(hashlib.sha256(proxy.encode()).digest()[:3])
        return {"type": "thumb", "path": key, "provider": "builtin", "remotely_accessible": False, "proxy_id": proxy}

    def catalog(self):
        """Every media item, by URI: Music Assistant's library, the first
        library's files, and TIDAL's once it is signed in."""
        items = {}
        sources = [("library", "library", LIBRARY)]
        libraries = list(self.library_instances())
        if libraries:
            sources.append((libraries[0], "file", LIBRARY))
        if self.tidal_instance() and self.providers[self.tidal_instance()]["enabled"]:
            sources.append((self.tidal_instance(), "tidal", TIDAL))
        for provider, scheme, albums in sources:
            tidal_tracks, artist_ids = [], {}
            for number, (artist, album, year, tracks, *kind) in enumerate(albums, 1):
                image = self._image(f"{scheme}:{album}")
                if scheme == "file":
                    artist_id, album_id = artist, f"{artist}/{album}"
                else:
                    # An artist of several albums is one artist.
                    artist_id, album_id = artist_ids.setdefault(artist, str(number * 10)), str(number * 100)
                artist_item = {"item_id": artist_id, "provider": provider, "name": artist, "media_type": "artist",
                               "uri": f"{provider}://artist/{artist_id}", "available": True}
                album_item = {"item_id": album_id, "provider": provider, "name": album, "media_type": "album",
                              "uri": f"{provider}://album/{album_id}", "available": True, "image": image}
                items.setdefault(artist_item["uri"], {**artist_item, "metadata": {"images": [image]}})
                items[album_item["uri"]] = {**album_item, "year": year, "artists": [artist_item],
                                            "album_type": kind[0] if kind else "album", "metadata": {"images": [image]}}
                for position, (title, seconds) in enumerate(tracks, 1):
                    track_id = (f"{album_id}/{position:02d} - {title}.flac" if scheme == "file"
                                else str(number * 100 + position))
                    track = {"item_id": track_id, "provider": provider, "name": title, "media_type": "track",
                             "uri": f"{provider}://track/{track_id}", "duration": seconds, "track_number": position,
                             "disc_number": 1, "artists": [artist_item], "album": album_item,
                             "metadata": {"images": [image]}, "favorite": False}
                    items[track["uri"]] = track
                    tidal_tracks.append(track)
            if scheme == "tidal":
                image = self._image("tidal:playlist")
                # TIDAL names a playlist by a UUID, and its other items by number.
                items[f"{provider}://playlist/{TIDAL_PLAYLIST_ID}"] = {
                    "item_id": TIDAL_PLAYLIST_ID, "provider": provider, "name": TIDAL_PLAYLIST, "media_type": "playlist",
                    "uri": f"{provider}://playlist/{TIDAL_PLAYLIST_ID}", "owner": "TIDAL",
                    "metadata": {"images": [image]}, "tracks": tidal_tracks}
        return items

    def tracks(self, uri):
        items = self.catalog()
        item = items.get(uri)
        if item is None:
            raise Error(MEDIA_NOT_FOUND)
        kind = item["media_type"]
        if kind == "track":
            return [item]
        if kind == "album":
            return [each for each in items.values() if each["media_type"] == "track" and each["album"]["uri"] == uri]
        if kind == "playlist":
            return list(item["tracks"])
        return [each for each in items.values() if each["media_type"] == "track" and each["artists"][0]["uri"] == uri]

    def search(self, search_query, media_types=None, limit=25, providers=None, **_):
        wanted = (search_query or "").lower()
        kinds = media_types or ["artist", "album", "track", "playlist"]
        results = {kind + "s": [] for kind in ("artist", "album", "track", "playlist", "genre", "radio", "audiobook",
                                               "podcast")}
        for item in self.catalog().values():
            if providers is not None and item["provider"] not in providers or item["media_type"] not in kinds:
                continue
            if item["provider"] != "library" and item["provider"] != self.tidal_instance():
                continue  # Files are found through Music Assistant's library.
            names = [item["name"], *(artist["name"] for artist in item.get("artists", ()))]
            if any(wanted in name.lower() for name in names):
                listed = results[item["media_type"] + "s"]
                if len(listed) < limit:
                    listed.append({key: value for key, value in item.items() if key != "tracks"})
        return results

    def browse(self, path=None, **_):
        if not path or path == "root":
            return [{"item_id": "root", "provider": entry["domain"], "path": f"{instance}://", "uri": f"{instance}://",
                     "name": entry["name"] or self._default_name(entry["domain"]), "media_type": "folder"}
                    for instance, entry in self.providers.items() if entry["type"] == "music"]
        instance, _, sub = path.partition("://")
        back = {"item_id": "root", "provider": "library", "path": "root", "name": "..", "media_type": "folder"}
        if instance not in self.providers:
            return [back]
        items = self.catalog()
        if self.providers[instance]["domain"] == "tidal":
            if not sub:
                return [back] + [{"item_id": kind, "provider": instance, "path": f"{instance}://{kind}",
                                  "name": kind.title(), "media_type": "folder"} for kind in ("albums", "playlists")]
            kind = sub.rstrip("s")
            return [back] + [{k: v for k, v in item.items() if k != "tracks"} for item in items.values()
                             if item["provider"] == instance and item["media_type"] == kind]
        # A library: its artists' folders, their albums' folders, then the files.
        if instance != next(iter(self.library_instances()), None):
            return [back]
        parts = [part for part in sub.split("/") if part]
        listing = [back]
        if len(parts) < 2:
            seen = []
            for item in items.values():
                if item["provider"] == instance and item["media_type"] == "track":
                    name = item["artists"][0]["name"] if not parts else item["album"]["name"]
                    if (not parts or item["artists"][0]["name"] == parts[0]) and name not in seen:
                        seen.append(name)
            for name in seen:
                relative = "/".join([*parts, name])
                listing.append({"item_id": relative, "provider": instance, "path": f"{instance}://{relative}",
                                "uri": f"{instance}://folder/{relative}", "name": name, "media_type": "folder"})
            return listing
        return listing + [item for item in items.values() if item["provider"] == instance
                          and item["media_type"] == "track" and item["item_id"].startswith(sub.rstrip("/") + "/")]

    def artist(self, item_id, provider_instance_id_or_domain, **_):
        uri = f"{provider_instance_id_or_domain}://artist/{item_id}"
        if uri not in self.catalog():
            raise Error(MEDIA_NOT_FOUND)
        return uri

    def children(self, command, item_id, provider_instance_id_or_domain, **_):
        kind = {"music/albums/album_tracks": "album", "music/playlists/playlist_tracks": "playlist",
                "music/artists/artist_albums": "artist"}[command]
        uri = f"{provider_instance_id_or_domain}://{kind}/{item_id}"
        items = self.catalog()
        if uri not in items:
            raise Error(MEDIA_NOT_FOUND)
        if kind == "artist":
            return [item for item in items.values() if item["media_type"] == "album" and item["artists"][0]["uri"] == uri]
        return self.tracks(uri)

    # ---- flows ----------------------------------------------------------------

    def _flow(self, kind, domain, *, step, instance=None):
        flow_id = secrets.token_hex(8)
        self.flows[flow_id] = {"domain": domain, "kind": kind, "instance": instance, "step": step}
        return {"flow_id": flow_id, **step}

    def setup(self, provider_domain):
        owner = f"provider.{provider_domain}"
        if provider_domain not in ("filesystem_local", "tidal") and any(
                entry["domain"] == provider_domain for entry in self.providers.values()):
            return {"flow_id": secrets.token_hex(8), "step_id": "abort", "type": "abort", "reason": "already_configured",
                    "translation_owner": owner}
        if provider_domain == "filesystem_local":
            return self._flow("setup", provider_domain, step={
                "step_id": "user", "type": "form", "last_step": True,
                "entries": [{"key": "content_type", "type": "string", "value": "music"},
                            {"key": "path", "type": "string", "value": "/media"}]})
        if provider_domain == "tidal":
            return self._tidal_step(self._flow("setup", "tidal", step={}))
        instance = self._create(provider_domain)
        return {"flow_id": secrets.token_hex(8), "step_id": f"finish_{instance}", "type": "finish",
                "result": {"instance_id": instance}}

    def _tidal_step(self, flow):
        self.tidal_code = "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(5))
        step = {"step_id": "device_login", "type": "external", "url": f"https://link.tidal.com/{self.tidal_code}",
                "expires_at": time.time() + 300}
        self.flows[flow["flow_id"]]["step"] = step
        return {"flow_id": flow["flow_id"], **step}

    def reconfigure(self, instance_id):
        entry = self.providers.get(instance_id)
        if entry is None:
            raise Error(999)
        if entry["domain"] != "tidal":
            raise Error(999)
        return self._tidal_step(self._flow("reconfigure", "tidal", step={}, instance=instance_id))

    def submit(self, flow_id, values):
        flow = self.flows.get(flow_id)
        if flow is None or flow["step"].get("type") != "form":
            raise Error(999)
        if not str(values.get("path", "")).startswith("/media/") or values.get("content_type") != "music":
            return {"flow_id": flow_id, **flow["step"], "errors": {"base": "invalid_path"}}
        del self.flows[flow_id]
        instance = self._create(flow["domain"], values)
        return {"flow_id": flow_id, "step_id": f"finish_{instance}", "type": "finish", "result": {"instance_id": instance}}

    def flow_get(self, flow_id):
        flow = self.flows.get(flow_id)
        if flow is None:
            raise Error(999)  # Music Assistant forgets a flow once it ends.
        return {"flow_id": flow_id, **flow["step"]}

    def approve_tidal(self):
        """The owner approved the code at link.tidal.com: the flow finishes."""
        with self.lock:
            flow_id = next(ident for ident, flow in self.flows.items() if flow["domain"] == "tidal")
            flow = self.flows.pop(flow_id)
            if flow["instance"] is None:
                self._create("tidal")
            else:
                self.providers[flow["instance"]]["enabled"] = True
                self._providers_updated()

    def expire_tidal(self):
        """The code expired: the provider shows a fresh one."""
        with self.lock:
            flow_id = next(ident for ident, flow in self.flows.items() if flow["domain"] == "tidal")
            self._tidal_step({"flow_id": flow_id})

    # ---- commands -------------------------------------------------------------

    def info(self):
        return {"server_id": self.server_id, "server_version": VERSION, "schema_version": 65,
                "min_supported_schema_version": 28, "base_url": f"http://127.0.0.1:{self.port}",
                "internal_url": f"http://127.0.0.1:{self.port}", "homeassistant_addon": False,
                "onboard_done": self.onboard_done, "name": "Music Assistant", "status": self.status,
                "external_url": None, "has_remote_access": self.remote_access}

    def create_user(self, username, password):
        """POST /setup: the first admin and a session token; None once a user exists."""
        with self.lock:
            if self.users or len(username) < 2 or len(password) < 8:
                return None
            self.users[username] = password
            return self._token(username, "setup", long_lived=False)

    def login(self, username, password):
        """POST /auth/login: a session token for the user's password, or None."""
        with self.lock:
            limited = self.limiting > 0
            self.limiting -= limited
            if limited or self.users.get(username) != password:
                return None
            return self._token(username, "login", long_lived=False)

    def _token(self, user, name, *, long_lived):
        token = secrets.token_urlsafe(48)
        now = time.time()
        self.tokens[token] = {"token_id": secrets.token_urlsafe(16), "user_id": user, "name": name,
                              "token_hash": hashlib.sha256(token.encode()).hexdigest(), "created_at": now,
                              "expires_at": now + (365 if long_lived else 30) * 86400, "is_long_lived": long_lived}
        return token

    def authorized(self, token):
        entry = self.tokens.get(token or "")
        return entry is not None and entry["expires_at"] > time.time()

    def revoke(self, token_id):
        for token, entry in list(self.tokens.items()):
            if entry["token_id"] == token_id:
                del self.tokens[token]
                for peer in list(self.peers):
                    if peer.token == token:
                        peer.closing = True
                return
        raise Error(INVALID_DATA)

    def command(self, token, command, args):
        """One API command's result; Error for Music Assistant's failures."""
        with self.lock:
            self.calls.append((command, args))
            if self.failing[command] > 0:
                self.failing[command] -= 1
                raise Error(999)
            return self._command(token, command, args)

    def _command(self, token, command, args):
        players = {"players/cmd/" + name: name for name in ("play", "pause", "stop", "next", "previous")}
        queues = {"player_queues/" + name: name for name in ("play", "pause", "stop", "next", "previous")}
        if command == "info":
            return self.info()
        if command == "auth/token/create":
            return self._token(self.tokens[token]["user_id"], args["name"], long_lived=True)
        if command == "auth/tokens":
            # The user's newest 100, as Music Assistant lists them.
            user = self.tokens[token]["user_id"]
            mine = sorted((entry for entry in self.tokens.values() if entry["user_id"] == user),
                          key=lambda entry: entry["created_at"], reverse=True)
            return [dict(entry) for entry in mine[:100]]
        if command == "auth/token/revoke":
            return self.revoke(args["token_id"])
        if command == "auth/logout":
            return self.revoke(self.tokens[token]["token_id"])
        if command == "config/onboard_complete":
            self.onboard_done = True
            return None
        if command == "remote_access/configure":
            self.remote_access = bool(args["enabled"])
            return {"enabled": self.remote_access}
        if command == "providers":
            return self.provider_list()
        if command == "config/providers":
            domain = args.get("provider_domain")
            return [{"type": entry["type"], "domain": entry["domain"], "instance_id": instance, "name": entry["name"],
                     "enabled": entry["enabled"], "status": "loaded", "values": {}}
                    for instance, entry in self.providers.items() if domain is None or entry["domain"] == domain]
        if command == "config/providers/get_value":
            entry = self.providers.get(args["instance_id"])
            if entry is None:
                raise Error(999)
            return entry["values"].get(args["key"], [] if args["key"] == "manual_discovery_ip_addresses" else None)
        if command == "config/providers/save":
            entry = self.providers.get(args.get("instance_id"))
            if entry is None:
                raise Error(INVALID_DATA)
            values = dict(args["values"])
            if "name" in values:
                entry["name"] = values.pop("name")
            entry["values"].update(values)
            self._providers_updated()
            return {"instance_id": args["instance_id"], "domain": entry["domain"], "name": entry["name"]}
        if command == "config/providers/remove":
            entry = self.providers.pop(args["instance_id"], None)
            if entry is None:
                raise Error(999)
            if entry["domain"] in ("sonos", "wiim"):
                for player_id, player in list(self.players.items()):
                    if player["provider"] == entry["domain"]:
                        del self.players[player_id]
                        self.queues.pop(player_id, None)
                        self.emit("player_removed", player_id, None)
            self._providers_updated()
            return None
        if command == "config/providers/setup":
            return self.setup(args["provider_domain"])
        if command == "config/providers/reconfigure":
            return self.reconfigure(args["instance_id"])
        if command == "config/flows/submit":
            return self.submit(args["flow_id"], args["values"])
        if command == "config/flows/get":
            return self.flow_get(args["flow_id"])
        if command == "config/flows/abort":
            if self.flows.pop(args["flow_id"], None) is None:
                raise Error(999)
            return None
        if command == "players/all":
            return [self.public(player) for player in self.players.values()
                    if player["type"] != "protocol" or args.get("return_protocol_players")]
        if command == "players/get":
            return self.public(self.player(args["player_id"]))
        if command in players:
            if args["player_id"] in self.players:  # An unknown player: nothing happens, no error.
                self.transport(players[command], args["player_id"])
            return None
        if command == "players/cmd/volume_set":
            return self.volume_set(args["player_id"], args["volume_level"])
        if command == "players/cmd/group_volume":
            return self.group_volume(args["player_id"], args["volume_level"])
        if command == "players/cmd/set_members":
            return self.set_members(args["target_player"], args.get("player_ids_to_add"), args.get("player_ids_to_remove"))
        if command == "players/cmd/ungroup":
            return self.ungroup(args["player_id"])
        if command == "player_queues/all":
            return [self.queue_public(queue) for queue in self.queues.values()]
        if command == "player_queues/get":
            queue = self.queues.get(args["queue_id"])
            return self.queue_public(queue) if queue else None
        if command == "player_queues/items":
            queue = self.queues.get(args["queue_id"])
            offset, limit = int(args.get("offset", 0)), int(args.get("limit", 500))
            return [{key: value for key, value in item.items()} for item in (queue or {"list": []})["list"][offset:offset + limit]]
        if command == "player_queues/play_media":
            return self.play_media(**args)
        if command in queues:
            return self.transport(queues[command], args["queue_id"])
        if command == "player_queues/seek":
            return self.seek(args["queue_id"], args["position"])
        if command == "player_queues/skip":
            queue = self.queues[args["queue_id"]]
            return self.seek(args["queue_id"], queue["elapsed_time"] + int(args.get("seconds", 10)))
        if command == "player_queues/shuffle":
            self.queues[args["queue_id"]]["shuffle_enabled"] = bool(args["shuffle_enabled"])
            return self._queue_updated(args["queue_id"])
        if command == "player_queues/repeat":
            if args["repeat_mode"] not in ("off", "one", "all"):
                raise Error(INVALID_DATA)
            self.queues[args["queue_id"]]["repeat_mode"] = args["repeat_mode"]
            return self._queue_updated(args["queue_id"])
        if command == "music/search":
            return self.search(**args)
        if command == "music/browse":
            return self.browse(**args)
        if command in ("music/albums/album_tracks", "music/playlists/playlist_tracks", "music/artists/artist_albums"):
            return self.children(command, **args)
        if command == "music/artists/top_tracks":
            return self.tracks(self.artist(**args))[:10]
        if command == "music/artists/similar_artists":
            uri = self.artist(**args)
            provider = uri.partition("://")[0]
            return [item for item in self.catalog().values() if item["media_type"] == "artist" and item["provider"] == provider
                    and item["uri"] != uri][:args.get("limit", 25)]
        if command == "music/item_by_uri":
            item = self.catalog().get(args["uri"])
            if item is None:
                raise Error(MEDIA_NOT_FOUND)
            return item
        raise Error(INVALID_COMMAND)

    # ---- events ---------------------------------------------------------------

    def emit(self, event, object_id, data):
        # UTF-8 as it is, as Music Assistant's encoder writes it.
        message = json.dumps({"event": event, "object_id": object_id, "data": data}, separators=(",", ":"),
                             ensure_ascii=False)
        self.events.append((event, object_id))
        for peer in list(self.peers):
            if peer.token is not None:
                peer.outbox.append(message)


class Peer:
    """One websocket client: what to send it, from any thread."""

    def __init__(self):
        self.outbox = collections.deque()
        self.token = None
        self.closing = False
        self.pongs = 0


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64


class Server:
    """Music Assistant's webserver on loopback for a Sim."""

    def __init__(self, sim: Sim, port=0) -> None:
        stopping = self.stopping = threading.Event()
        connections = self.connections = set()

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            # One write per answer: headers and body apart wait on the client's delayed ACK.
            wbufsize = 65536

            def setup(self):
                super().setup()
                with sim.lock:
                    connections.add(self.connection)

            def finish(self):
                with sim.lock:
                    connections.discard(self.connection)
                super().finish()

            def log_message(self, *_):
                pass

            def answer(self, status, body, kind="application/json", headers=()):
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(data)))
                for name, value in headers:
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(data)

            def lost(self, path):
                """Whether this request's answer is lost: the connection drops unanswered."""
                with sim.lock:
                    if sim.losing[path] <= 0:
                        return False
                    sim.losing[path] -= 1
                self.close_connection = True
                return True

            def read_json(self):
                length = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(length) or b"null")

            def do_GET(self):
                url = urllib.parse.urlsplit(self.path)
                if url.path == "/info":
                    with sim.lock:
                        return self.answer(200, sim.info())
                if url.path == "/ws":
                    return self.websocket()
                match = re.fullmatch(r"/imageproxy/([^/]*)", url.path)
                if match:
                    params = urllib.parse.parse_qs(url.query)
                    size, fmt = params.get("size", ["0"])[0], params.get("fmt", ["png"])[0]
                    if not re.fullmatch(r"[0-9a-f]{64}", match[1]) or not size.isdigit() or int(size) not in SIZES:
                        return self.answer(400, b"Invalid image request", "text/plain")
                    with sim.lock:
                        colour = sim.images.get(match[1])
                    if colour is None:
                        return self.answer(404, b"Not found", "text/plain")
                    pixels = min(int(size) or 512, 512)
                    image, kind = (jpeg(colour, pixels), "image/jpeg") if fmt in ("jpg", "jpeg") else (png(colour, pixels), "image/png")
                    return self.answer(200, image, kind, (("Cache-Control", "max-age=31536000"),
                                                          ("Access-Control-Allow-Origin", "*")))
                return self.answer(404, b"Not found", "text/plain")

            def do_POST(self):
                url = urllib.parse.urlsplit(self.path)
                try:
                    body = self.read_json()
                except ValueError:
                    return self.answer(400, b"Invalid JSON", "text/plain")
                if url.path == "/setup":
                    if not isinstance(body, dict):
                        return self.answer(400, b"Body required", "text/plain")
                    token = sim.create_user(str(body.get("username", "")).strip(), str(body.get("password", "")))
                    if self.lost(url.path):
                        return None
                    if token is None:
                        return self.answer(400, {"success": False, "error": "Setup already completed"})
                    return self.answer(200, {"success": True, "token": token,
                                             "user": {"username": body["username"], "role": "admin"}})
                if url.path == "/auth/login":
                    with sim.lock:
                        if not sim.users:
                            return self.answer(403, {"success": False, "error": "Setup required"})
                    if not isinstance(body, dict) or not isinstance(body.get("credentials"), dict):
                        return self.answer(400, b"Invalid request body", "text/plain")
                    credentials = body["credentials"]
                    token = sim.login(str(credentials.get("username", "")), str(credentials.get("password", "")))
                    if token is None:
                        return self.answer(401, {"success": False, "error": "Invalid username or password"})
                    return self.answer(200, {"success": True, "token": token})
                if url.path != "/api":
                    return self.answer(404, b"Not found", "text/plain")
                with sim.lock:
                    if not sim.users:
                        return self.answer(503, b"Setup required", "text/plain")
                if not isinstance(body, dict) or not isinstance(body.get("command"), str):
                    return self.answer(400, b"Invalid request", "text/plain")
                token = (self.headers.get("Authorization") or "").removeprefix("Bearer ")
                with sim.lock:
                    # Only a request with a token is refused as a failed authentication.
                    refused = bool(token) and sim.refusing > 0
                    sim.refusing -= refused
                    if refused or not sim.authorized(token):
                        return self.answer(401, b"Authentication required", "text/plain",
                                           (("WWW-Authenticate", "Bearer"),))
                try:
                    result = sim.command(token, body["command"], dict(body.get("args") or {}))
                except Error as error:
                    if error.code in (INVALID_DATA, INVALID_COMMAND):
                        return self.answer(400, b"Invalid request", "text/plain")
                    return self.answer(500, b"Internal server error", "text/plain")
                except Exception:
                    return self.answer(500, b"Internal server error", "text/plain")
                return self.answer(200, result)

            def websocket(self):
                key = self.headers.get("Sec-WebSocket-Key")
                if (self.headers.get("Upgrade") or "").lower() != "websocket" or not key:
                    return self.answer(400, b"Upgrade required", "text/plain")
                self.send_response(101)
                self.send_header("Upgrade", "websocket")
                self.send_header("Connection", "Upgrade")
                self.send_header("Sec-WebSocket-Accept",
                                 base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode())
                self.end_headers()
                self.wfile.flush()
                self.close_connection = True
                connection, frames, peer = self.connection, Frames(), Peer()
                connection.settimeout(0.02)
                with sim.lock:
                    sim.peers.add(peer)
                    peer.outbox.append(json.dumps(sim.info()))
                    if not sim.users:
                        peer.outbox.append(json.dumps({"message_id": "connection", "error_code": 503,
                                                       "details": "Setup required"}))
                        peer.closing = True
                pinged, waiting, text = time.monotonic(), False, bytearray()
                closing = struct.pack(">H", 1000)
                try:
                    while not stopping.is_set():
                        while peer.outbox:
                            connection.sendall(frame(TEXT, peer.outbox.popleft().encode()))
                        if peer.closing:
                            connection.sendall(frame(CLOSE, closing))
                            return
                        if time.monotonic() - pinged >= sim.heartbeat:
                            if waiting:
                                return  # Unanswered: aiohttp's heartbeat drops the client.
                            connection.sendall(frame(PING, b"thermite-sim"))
                            pinged, waiting = time.monotonic(), True
                        try:
                            data = connection.recv(65536)
                        except socket.timeout:
                            continue
                        if not data:
                            return
                        for opcode, final, payload in frames.feed(data):
                            if opcode == PONG:
                                waiting = False
                                peer.pongs += 1
                            elif opcode == PING:
                                connection.sendall(frame(PONG, payload))
                            elif opcode == CLOSE:
                                connection.sendall(frame(CLOSE, closing))
                                return
                            elif opcode in (TEXT, CONTINUATION):
                                text += payload
                                if final:
                                    message, text = text.decode(), bytearray()
                                    self.message(peer, message)
                except (OSError, ValueError, UnicodeError):
                    return
                finally:
                    with sim.lock:
                        sim.peers.discard(peer)

            def message(self, peer, data):
                try:
                    message = json.loads(data)
                    message_id, command, args = message["message_id"], message["command"], message.get("args") or {}
                except (ValueError, KeyError, TypeError):
                    peer.closing = True
                    return
                with sim.lock:
                    if command == "auth":
                        token = args.get("token")
                        if sim.authorized(token):
                            peer.token = token
                            reply = {"message_id": message_id, "result": {"authenticated": True,
                                                                         "user": {"username": sim.tokens[token]["user_id"]}}}
                        else:
                            reply = {"message_id": message_id, "error_code": AUTHENTICATION_FAILED,
                                     "details": "Authentication failed"}
                            peer.closing = True
                        peer.outbox.append(json.dumps(reply))
                        return
                    if peer.token is None or not sim.authorized(peer.token):
                        peer.outbox.append(json.dumps({"message_id": message_id, "error_code": 20,
                                                       "details": "Authentication required"}))
                        return
                    try:
                        result = sim.command(peer.token, command, dict(args))
                        reply = {"message_id": message_id, "result": result, "partial": False}
                    except Error as error:
                        reply = {"message_id": message_id, "error_code": error.code, "details": "Error"}
                    except Exception:
                        reply = {"message_id": message_id, "error_code": 999, "details": "Error"}
                    peer.outbox.append(json.dumps(reply))

        self.sim = sim
        self.httpd = _Server(("127.0.0.1", port), Handler)
        sim.port = self.httpd.server_address[1]
        self.thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return self.httpd.server_address[1]

    @property
    def address(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"{host}:{port}"

    def start(self):
        self.thread = threading.Thread(target=self.httpd.serve_forever, args=(0.05,), daemon=True)
        self.thread.start()
        return self

    def stop(self):
        """Stop serving and drop every open connection, as a stopped server's
        clients see it."""
        self.stopping.set()
        if self.thread:
            self.httpd.shutdown()
            self.thread.join(timeout=3)
            self.thread = None
        self.httpd.server_close()
        with self.sim.lock:
            open_connections = list(self.connections)
        for connection in open_connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def __enter__(self):
        return self.start()

    def __exit__(self, *_):
        self.stop()
