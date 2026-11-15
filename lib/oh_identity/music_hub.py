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
import weakref

from oh_music import provision
from oh_music.client import Unavailable
from oh_music.events import Socket

from .hub import UNAVAILABLE, Fanout, encode
from .music import Sources
from .topics import WITHOUT_TEXT

VIEW = "music"
_hubs = weakref.WeakKeyDictionary()


class MusicHub(Fanout):
    """Music Assistant's players and queues for the matched speakers, as the
    websocket last reported them, fanned out to the music streams."""

    def __init__(self, music, *, queue=None, linger=None, backoff=None, quiet=None):
        super().__init__((VIEW, VIEW + WITHOUT_TEXT), queue=queue, linger=linger, backoff=backoff)
        self.music, self.quiet = music, quiet
        self.players, self.queues, self.sources = {}, {}, None
        self.sent = {}
        self._snapshots = {}
        # Whether the last connection failed before serving or since.
        self.down = False

    def join(self, view, meta=None):
        subscriber = super().join(view, meta)
        if self.down and not self.ready:
            subscriber.push(UNAVAILABLE)
        return subscriber

    # ---- frames ---------------------------------------------------------------

    def speakers(self, see_text):
        ids = {player["player_id"]: speaker for speaker, player in self.players.items()}
        return {ident: self.music.speaker(ident, self.players, ids, self.queues, see_text)
                for ident in self.music.spec["speakers"]}

    def snapshot(self, view):
        if view not in self._snapshots:
            see_text = view == VIEW
            overview = self.music.overview(self.players, self.queues, self.sources, see_text=see_text)
            self._snapshots[view] = encode({"type": "snapshot", "view": VIEW, "music": overview})
            self.sent[view] = {speaker["id"]: speaker for speaker in overview["speakers"]}
        return self._snapshots[view]

    def changed(self):
        """Send each view's speakers that changed since it was last sent."""
        self._snapshots.clear()
        if not self.ready:
            return
        for view, subscribers in self.subscribers.items():
            if not subscribers:
                self.sent.pop(view, None)
                continue
            last = self.sent.setdefault(view, {})
            for ident, speaker in self.speakers(view == VIEW).items():
                if last.get(ident) != speaker:
                    last[ident] = speaker
                    data = encode({"type": "speaker", "speaker": speaker})
                    for subscriber in list(subscribers):
                        subscriber.push(data)

    def resync(self):
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
        kind, data, ident = message.get("event"), message.get("data"), message.get("object_id")
        if kind in ("player_added", "player_updated"):
            return self.keep_player(data)
        if kind == "player_removed":
            gone = [speaker for speaker, player in self.players.items() if player["player_id"] == ident]
            for speaker in gone:
                del self.players[speaker]
            return bool(gone)
        if kind in ("queue_added", "queue_updated"):
            return self.keep_queue(data)
        if kind == "queue_time_updated" and ident in self.queues and type(data) in (int, float):
            self.queues[ident] = {**self.queues[ident], "elapsed_time": data, "elapsed_time_last_updated": time.time()}
            return True
        if kind == "providers_updated" and isinstance(data, list):
            self.sources = Sources(data, self.music.spec["libraries"])
            if self.ready:
                # Before then, the first snapshot is still to come.
                self.resync()
        return False

    def _forget(self):
        self.players, self.queues, self.sources = {}, {}, None
        self._snapshots.clear()
        self.sent.clear()

    async def _session(self):
        try:
            await self._connected()
        except asyncio.CancelledError:
            raise
        except Exception:
            if not self.ready and not self.down:
                # Fanout tells only the streams it served; these wait for a first snapshot.
                self._broadcast(lambda view: UNAVAILABLE)
            self.down = True
            raise

    async def _connected(self):
        claim = await asyncio.to_thread(provision.read_claim, self.music.state)
        if provision.problem(claim, time.time()):
            raise Unavailable("Music Assistant is not claimed")
        options = {} if self.quiet is None else {"quiet": self.quiet}
        socket = await Socket.open(self.music.spec["port"], claim["token"], **options)
        try:
            self.counters["upstream_connects"] += 1
            asked = {await socket.command("players/all"): "players", await socket.command("player_queues/all"): "queues",
                     await socket.command("providers"): "providers"}
            answers = {}
            while True:
                message = await socket.receive()
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
                answers.setdefault(name, []).extend(result if isinstance(result, list) else [])
                if message.get("partial"):
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
