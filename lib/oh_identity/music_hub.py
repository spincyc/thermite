"""One Music Assistant websocket per gateway process, shared by every music
event stream (GET /api/v1/events/music), on hub.Fanout's terms: it opens
with the first stream, closes LINGER seconds after the last, reconnects
through BACKOFF, and streams `{"type":"unavailable"}` when it goes down.
Music Assistant down, starting or not claimed is no rare outage, so unlike
openHAB's hub a stream also hears it at once when it joins, or waits for
its first snapshot, while the connection fails.

A stream's first frame is `{"type":"snapshot","view":"music","music":...}`,
GET /api/v1/music's answer; after it, `{"type":"speaker","speaker":...}`
whenever what a client sees of a speaker changes, and a fresh snapshot when
Music Assistant's providers change (TIDAL signed in, a library added). Only
player_updated, queue_updated and queue_time_updated (and their added and
removed kin) of matched speakers' players and queues are kept; every other
event is dropped as it arrives. A Guest who may not see free text joins the
WITHOUT_TEXT view: the same frames without titles or art.
"""

import asyncio
import time
from typing import Any
import weakref

from oh_music import provision
from oh_music.client import Unavailable
from oh_music.events import Socket

from .hub import UNAVAILABLE, Fanout, encode
from .music import Sources
from .topics import WITHOUT_TEXT

VIEW = "music"
#: Upper bounds on accumulated partial-result reads (players/all,
#: player_queues/all, providers) so a misbehaving server cannot OOM the gateway.
MAX_ANSWER_ITEMS = 4096
MAX_PARTIAL_CHUNKS = 64
_hubs: weakref.WeakKeyDictionary[Any, Any] = weakref.WeakKeyDictionary()


class MusicHub(Fanout):
    """Music Assistant's players and queues for the matched speakers, as the
    websocket last reported them, fanned out to the music streams."""

    def __init__(self, music, *, queue=None, linger=None, backoff=None, quiet=None):
        super().__init__((VIEW, VIEW + WITHOUT_TEXT), queue=queue, linger=linger, backoff=backoff)
        self.music, self.quiet = music, quiet
        self.players, self.queues, self.sources = {}, {}, None
        self.sent = {}
        self._snapshots = {}
        # Speaker IDs narrowed by the last event (e.g. 1 Hz queue_time_updated),
        # or None when every speaker should be recomputed.
        self._affected = None
        # Whether the last connection failed before serving or since.
        self.down = False

    def join(self, view, meta=None):
        if self.task is None or self.task.done():
            # A new connection: one that failed before the hub closed says nothing of now.
            self.down = False
        subscriber = super().join(view, meta)
        if self.down and not self.ready:
            subscriber.push(UNAVAILABLE)
        return subscriber

    # ---- frames ---------------------------------------------------------------

    def speakers(self, see_text):
        ids = self.music.ids(self.players)
        return {ident: self.music.speaker(ident, self.players, ids, self.queues, see_text)
                for ident in self.music.spec["speakers"]}

    def snapshot(self, view):
        if view not in self._snapshots:
            see_text = view == VIEW
            overview = self.music.overview(self.players, self.queues, self.sources, see_text=see_text)
            self._snapshots[view] = encode({"type": "snapshot", "view": VIEW, "music": overview})
            self.sent[view] = {speaker["id"]: speaker for speaker in overview["speakers"]}
        return self._snapshots[view]

    def _queue_speakers(self, queue_id):
        """Matched speakers whose view reads `queue_id` (its owner and synced followers)."""
        return [
            ident for ident, player in self.players.items()
            if player["player_id"] == queue_id or player.get("synced_to") == queue_id
        ]

    def changed(self, only=None):
        """Send each view's speakers that changed since it was last sent.
        When narrowed to specific speaker IDs, recompute only those speakers."""
        targets = self._affected if only is None else only
        self._affected = None
        self._snapshots.clear()
        if not self.ready:
            return
        ids = self.music.ids(self.players) if targets is not None else None
        narrowed = [ident for ident in targets if ident in self.music.spec["speakers"]] if targets is not None else None
        for view, subscribers in self.subscribers.items():
            if not subscribers:
                self.sent.pop(view, None)
                continue
            last = self.sent.setdefault(view, {})
            see_text = view == VIEW
            current = (self.speakers(see_text) if narrowed is None else {
                ident: self.music.speaker(ident, self.players, ids, self.queues, see_text)
                for ident in narrowed
            })
            for ident, speaker in current.items():
                if last.get(ident) != speaker:
                    last[ident] = speaker
                    data = encode({"type": "speaker", "speaker": speaker})
                    for subscriber in list(subscribers):
                        subscriber.push(data)

    def resync(self):
        self._affected = None
        self._snapshots.clear()
        self.sent.clear()
        self._broadcast(self.snapshot)

    # ---- Music Assistant ------------------------------------------------------

    def keep_player(self, player):
        speaker = self.music.match(player)
        if speaker and isinstance(player.get("player_id"), str):
            self.players[speaker] = player
            return True
        return False

    def keep_queue(self, queue):
        mine = {player["player_id"] for player in self.players.values()}
        if isinstance(queue, dict) and queue.get("queue_id") in mine:
            self.queues[queue["queue_id"]] = queue
            return True
        return False

    def event(self, message):
        """Apply one Music Assistant event; whether it changed what is kept."""
        self._affected = None
        kind, data, ident = message.get("event"), message.get("data"), message.get("object_id")
        if kind in ("player_added", "player_updated"):
            return self.keep_player(data)
        if kind == "player_removed":
            gone = [speaker for speaker, player in self.players.items() if player["player_id"] == ident]
            for speaker in gone:
                del self.players[speaker]
            if isinstance(ident, str):
                self.queues.pop(ident, None)
            return bool(gone)
        if kind in ("queue_added", "queue_updated"):
            if self.keep_queue(data):
                self._affected = self._queue_speakers(data["queue_id"])
                return True
            return False
        if kind == "queue_time_updated" and ident in self.queues and type(data) in (int, float):
            self.queues[ident] = {**self.queues[ident], "elapsed_time": data, "elapsed_time_last_updated": time.time()}
            self._affected = self._queue_speakers(ident)
            return True
        if kind == "providers_updated" and isinstance(data, list):
            self.sources = Sources(data, self.music.spec["libraries"])
            if self.ready:
                # Before then, the first snapshot is still to come.
                self.resync()
        return False

    def _forget(self):
        self.players, self.queues, self.sources = {}, {}, None
        self._affected = None
        self._snapshots.clear()
        self.sent.clear()

    async def _session(self):
        owner = asyncio.current_task()
        try:
            await self._connected()
        except asyncio.CancelledError:
            raise
        except Exception:
            if self.task is owner:
                if not self.ready and not self.down:
                    # Fanout tells only the streams it served; these wait for a first snapshot.
                    self._broadcast(lambda view: UNAVAILABLE)
                self.down = True
            raise

    async def _connected(self):
        owner = asyncio.current_task()
        claim = await asyncio.to_thread(provision.read_claim, self.music.state)
        if self.task is not owner:
            return
        if provision.problem(claim):
            raise Unavailable("Music Assistant is not claimed")
        options = {} if self.quiet is None else {"quiet": self.quiet}
        socket = await Socket.open(self.music.spec["port"], claim["token"], **options)
        try:
            if self.task is not owner:
                return
            self.counters["upstream_connects"] += 1
            asked = {await socket.command("players/all"): "players", await socket.command("player_queues/all"): "queues",
                     await socket.command("providers"): "providers"}
            answers: dict[str, list[Any]] = {}
            partials: dict[str, int] = {}
            while True:
                message = await socket.receive()
                if self.task is not owner:
                    return
                if "event" in message:
                    self.counters["events"] += 1
                    if self.event(message):
                        self.changed()
                    continue
                name = asked.get(message.get("message_id"))
                if name is None:
                    continue
                if "error_code" in message:
                    raise Unavailable("Music Assistant refused a read")
                result = message.get("result")
                chunk = result if isinstance(result, list) else []
                collected = answers.setdefault(name, [])
                if len(collected) + len(chunk) > MAX_ANSWER_ITEMS:
                    raise Unavailable("Music Assistant answer too large")
                collected.extend(chunk)
                if message.get("partial"):
                    partials[name] = partials.get(name, 0) + 1
                    if partials[name] > MAX_PARTIAL_CHUNKS:
                        raise Unavailable("Music Assistant answer too large")
                    continue
                del asked[message["message_id"]]
                if name == "players":
                    self.players = {}
                    for player in answers.pop(name):
                        self.keep_player(player)
                elif name == "providers":
                    self.sources = Sources(answers.pop(name), self.music.spec["libraries"])
                if "queues" in answers and "players" not in asked.values():
                    # Music Assistant may answer in any order; a queue is kept by its player.
                    self.queues = {}
                    for each in answers.pop("queues"):
                        self.keep_queue(each)
                if not asked and not self.ready:
                    # Every read has answered: events that came before them are older.
                    self.ready, self.down = True, False
                    self.counters["resyncs"] += 1
                    self.resync()
        finally:
            await socket.close()


def hub_for(music):
    """The music's hub on the running event loop, created on first use."""
    loop = asyncio.get_running_loop()
    hub = _hubs.get(music)
    if hub is None or hub.loop is not loop:
        hub = _hubs[music] = MusicHub(music)
    return hub
