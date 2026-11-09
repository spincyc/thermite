"""Client event streams: admission, the snapshot-first view stream and
bounded authorization revalidation. hub.py holds the one upstream."""

import asyncio
import threading
import time
import weakref

from asgiref.sync import sync_to_async
from django.contrib.sessions.models import Session
from django.http import StreamingHttpResponse
from django.utils import timezone

from .addresses import peer_bucket
from .auth import not_allowed, principal, problem_response, require_admin
from .guest import listener as guest_listener
from .hub import hub_for, notify
from .models import PersonalToken, User
from .runtime import get_adapter
from .services import Problem
from .topics import VIEWS, views as view_topics

#: Seconds a client stream may stay quiet before the gateway sends a
#: heartbeat. It is a data frame, {"type":"heartbeat"}, not an SSE comment:
#: an EventSource hides comments, and the clients' watchdogs
#: (web/remote/access.js STREAM_HEARTBEAT_MS) reopen a stream that shows no
#: frame for three heartbeats.
HEARTBEAT = 5
HEARTBEAT_FRAME = b'data: {"type":"heartbeat"}\n\n'
# A busy stream rechecks its credential at most this often; a quiet one at
# every heartbeat.
REVALIDATE = 1.0
clock = time.monotonic
# Streams are unauthenticated for Guests. A page holds one, a terminal remote
# one more while it pairs, and a stream lingers up to one heartbeat after its
# client goes; so a browser with a few visible pages and a terminal fits. A
# stream costs a queue in the gateway, not an upstream connection, but holds
# one of the gateway's 100 in-flight requests (oh_access/service.py
# MAX_REQUESTS); the total leaves over half of them for ordinary requests.
# Over plain HTTP (the Guest listener) a browser allows six connections per
# origin across its tabs, so one stream per page leaves room for requests.
PEER_STREAMS = 8
TOTAL_STREAMS = 48
ERROR = b'data: {"type":"unavailable","error":"runtime_unavailable"}\n\n'


class Slots:
    """Concurrent stream admission per peer bucket and in total."""

    def __init__(self, per_peer=PEER_STREAMS, total=TOTAL_STREAMS):
        self.per_peer, self.total = per_peer, total
        self._guard = threading.Lock()
        self._peers = {}

    def acquire(self, peer):
        with self._guard:
            if sum(self._peers.values()) >= self.total or self._peers.get(peer, 0) >= self.per_peer:
                return None
            self._peers[peer] = self._peers.get(peer, 0) + 1
        released = False

        def release():
            nonlocal released
            with self._guard:
                if released:
                    return
                released = True
                self._peers[peer] -= 1
                if not self._peers[peer]:
                    del self._peers[peer]
        return release

    def count(self):
        with self._guard:
            return sum(self._peers.values())

    def counts(self, peer):
        """(this peer's streams, all streams), for the stream trail."""
        with self._guard:
            return self._peers.get(peer, 0), sum(self._peers.values())


slots = Slots()


class EventStream(StreamingHttpResponse):
    """Releases its admission slot however the response ends."""

    def __init__(self, content, release, **options):
        super().__init__(content, **options)
        self._release = release

    def close(self):
        try:
            super().close()
        finally:
            self._release()


def valid(snapshot):
    from .security import get_state
    state = get_state()
    if snapshot.get("security_revision") != state.revision:
        return False
    if snapshot.get("guest"):
        return True
    if not state.enabled:
        return False
    if not User.objects.filter(pk=snapshot["user"], is_active=True, auth_revision=snapshot["revision"]).exists():
        return False
    if snapshot.get("token"):
        return PersonalToken.objects.filter(pk=snapshot["token"], revoked=False, expires_at__gt=timezone.now(), auth_revision=snapshot["revision"]).exists()
    return Session.objects.filter(session_key=snapshot.get("session"), expire_date__gt=timezone.now()).exists()


def opened(meta):
    """A client stream has started. meta: its view; origin, "guest" for the
    plain-HTTP Guest listener, else "https"; principal, "guest", "session" or
    "token"; the request's HTTP version; and the peer's keyed digest, never
    its address."""
    notify("stream_open", meta)


def closed(meta, seconds, reason):
    """A client stream has ended after `seconds`; reason is "client" (it went
    away or the gateway stopped), "revoked" (its credential ended) or "error"."""
    notify("stream_close", {**meta, "seconds": round(seconds, 3), "reason": reason})


async def stream(adapter, view, snapshot, release=None, meta=None):
    """One client stream of a view: the hub's snapshot, then the view's events,
    with a heartbeat frame after each HEARTBEAT seconds without one. Ends when
    the credential is revoked."""
    meta = meta or {"view": view}
    hub = subscriber = None
    started, reason = clock(), "client"
    try:
        if not await sync_to_async(valid)(snapshot):
            reason = "revoked"
            return
        opened(meta)
        hub = hub_for(adapter)
        subscriber = hub.join(view, meta)
        checked = clock()
        while True:
            data = await subscriber.next(HEARTBEAT)
            # Every heartbeat is checked, bounding a quiet stream's revocation
            # by HEARTBEAT; a busy stream is checked at most once per REVALIDATE,
            # always before the next frame is sent.
            if data is None or clock() - checked >= REVALIDATE:
                if not await sync_to_async(valid)(snapshot):
                    reason = "revoked"
                    return
                checked = clock()
            yield HEARTBEAT_FRAME if data is None else data
    except asyncio.CancelledError:
        raise
    except Exception:
        # No exception details, credentials, raw body or upstream URL in output.
        reason = "error"
        yield ERROR
    finally:
        if subscriber:
            hub.leave(subscriber)
            closed(meta, clock() - started, reason)
        if release:
            release()


def events(request, view):
    if request.method != "GET":
        return not_allowed("GET")
    try:
        if view not in VIEWS or request.GET:
            raise Problem("not_found", 404)
        user = principal(request)
        if view == "pair":
            # Pairing is an Admin's page, as its device list is (views.pairing_items).
            require_admin(request, user)
        adapter = get_adapter()
        token = getattr(request, "identity_token", None)
        snapshot = {"user": user.pk, "revision": user.auth_revision, "token": str(token.pk) if token else None,
                    "session": request.session.session_key if not token else None,
                    "security_revision": user._identity_context["security_revision"],
                    "guest": getattr(user, "is_guest", False)}
        scope = getattr(request, "scope", None)
        # Lifecycle fields for observers (metrics); the journal lines are
        # trail.stream_opened's. A Guest-listener request carries no
        # credential: its principal is a Guest.
        meta = {"view": view, "origin": "guest" if guest_listener(request) else "https",
                "principal": "guest" if snapshot["guest"] else "token" if token else "session",
                "http_version": scope.get("http_version") if isinstance(scope, dict) else None,
                "peer": user._identity_context.get("peer")}
        bucket = peer_bucket(request.META.get("REMOTE_ADDR"))
        release = slots.acquire(bucket)
        if release is None:
            notify("stream_refused", meta)
            raise Problem("too_many_streams", 429, retry_after=HEARTBEAT)
        # One journal line when the stream opens and one when it ends, with
        # its duration, the number of topics its view carries and the peer's
        # and gateway's stream counts. The wrapped release runs once however
        # the response ends.
        from .trail import stream_opened
        release = stream_opened(release, request, user, topics=len(view_topics(adapter.contract)[view]),
                                counts=lambda: slots.counts(bucket))
        content = stream(adapter, view, snapshot, release, meta)
        # A generator that never starts never runs its finally block.
        weakref.finalize(content, release)
        response = EventStream(content, release, content_type="text/event-stream")
        response["Cache-Control"] = "no-store"
        response["X-Accel-Buffering"] = "no"
        return response
    except Problem as exc:
        return problem_response(exc)
