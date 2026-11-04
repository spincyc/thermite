"""Credential-free SSE projection and bounded authorization revalidation."""

import asyncio
import fnmatch
import json
import threading
import time
import weakref

from asgiref.sync import sync_to_async
from django.contrib.sessions.models import Session
from django.http import StreamingHttpResponse
from django.utils import timezone

from .addresses import peer_bucket
from .auth import not_allowed, principal, problem_response
from .models import PersonalToken, User
from .runtime import get_adapter, project
from .services import Problem

HEARTBEAT = 5
# A busy stream rechecks its credential at most this often; a quiet one at
# every heartbeat.
REVALIDATE = 1.0
clock = time.monotonic
MAX_PATTERNS = 64
MAX_TOPICS = 1000
# Streams are unauthenticated for Guests and each holds upstream connections.
# One web remote opens up to three (web/remote/backend.js EVENT_STREAMS), so a
# client with a few pages and a terminal fits; the total leaves most of the
# service's 100 connections (oh_access/service.py) for ordinary requests.
PEER_STREAMS = 12
TOTAL_STREAMS = 64
TYPES = {"stateupdated": "ItemStateUpdatedEvent", "statechanged": "ItemStateChangedEvent", "command": "ItemCommandEvent"}
_TOPICS = weakref.WeakKeyDictionary()


def available(contract):
    """Every public topic of this contract, computed once per contract."""
    topics = _TOPICS.get(contract)
    if topics is None:
        topics = _TOPICS[contract] = frozenset(
            "openhab/items/" + item + "/" + verb for item, spec in contract.states.items() for verb in TYPES
            if verb != "command" or spec.get("commands") is True and "_pairing" not in item)
    return topics


def topics_for(contract, query):
    if not isinstance(query, str) or len(query) > 12000:
        raise Problem("invalid_topics")
    patterns = set(query.split(","))
    if len(patterns) > MAX_PATTERNS:
        raise Problem("invalid_topics")
    public = available(contract)
    out = set()
    for pattern in sorted(patterns):
        parts = pattern.split("/")
        if len(parts) != 4 or parts[:2] != ["openhab", "items"] or parts[3] not in {*TYPES, "*"}:
            raise Problem("invalid_topics")
        # A credential command family is forbidden before wildcard expansion.
        if "_pairing" in parts[2] and not parts[2].endswith(("_pairing_status", "_pairing_result")):
            raise Problem("invalid_topics")
        if not any(mark in pattern for mark in "*?["):
            found = {pattern} & public
        else:
            found = {topic for topic in public if fnmatch.fnmatchcase(topic, pattern)}
        if not found:
            raise Problem("invalid_topics")
        out |= found
        if len(out) > MAX_TOPICS:
            raise Problem("invalid_topics")
    if not out:
        raise Problem("invalid_topics")
    return sorted(out)


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


def frame(contract, allowed, event):
    if not isinstance(event, dict) or event.get("topic") not in allowed:
        return None
    _, _, item, verb = event["topic"].split("/")
    if verb == "command" and (not contract.states[item].get("commands") or "_pairing" in item):
        return None
    if event.get("type") != TYPES[verb]:
        return None
    raw = event.get("payload")
    try:
        payload = json.loads(raw) if isinstance(raw, str) else None
    except ValueError:
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("value"), str):
        return None
    clean = {"type": payload.get("type") if payload.get("type") in {"String", "OnOff", "Decimal", "Percent", "UnDef"} else "String",
             "value": project(item, payload["value"], contract.states[item])}
    if verb == "statechanged" and isinstance(payload.get("oldValue"), str):
        clean["oldValue"] = project(item, payload["oldValue"], contract.states[item])
    return {"topic": event["topic"], "type": TYPES[verb], "payload": json.dumps(clean, separators=(",", ":"))}


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


async def stream(adapter, topics, snapshot, release=None):
    projected = getattr(adapter.transport, "projects_events", False)
    iterable = adapter.transport.events(topics, project=lambda event: frame(adapter.contract, topics, event)) if projected else adapter.transport.events(topics)
    iterator = iterable.__aiter__()
    pending = None
    try:
        if not await sync_to_async(valid)(snapshot):
            return
        checked = clock()
        while True:
            if pending is None:
                pending = asyncio.create_task(anext(iterator))
            done, _ = await asyncio.wait([pending], timeout=HEARTBEAT)
            # Every heartbeat is checked, bounding a quiet stream's revocation
            # by HEARTBEAT; a busy stream is checked at most once per REVALIDATE.
            if not done or clock() - checked >= REVALIDATE:
                if not await sync_to_async(valid)(snapshot):
                    return
                checked = clock()
            if not done:
                yield b": heartbeat\n\n"
                continue
            try:
                event = pending.result()
            except StopAsyncIteration:
                return
            pending = None
            value = event if projected else frame(adapter.contract, topics, event)
            if value:
                yield ("data: " + json.dumps(value, separators=(",", ":")) + "\n\n").encode()
    except asyncio.CancelledError:
        raise
    except Exception:
        # No exception details, credentials, raw body or upstream URL in output.
        yield b"event: unavailable\ndata: {\"error\":\"runtime_unavailable\"}\n\n"
    finally:
        if pending:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        close = getattr(iterator, "aclose", None)
        if close:
            await close()
        if release:
            release()


def events(request):
    if request.method != "GET":
        return not_allowed("GET")
    try:
        user = principal(request)
        adapter = get_adapter()
        topics = topics_for(adapter.contract, request.GET.get("topics", ""))
        token = getattr(request, "identity_token", None)
        snapshot = {"user": user.pk, "revision": user.auth_revision, "token": str(token.pk) if token else None,
                    "session": request.session.session_key if not token else None,
                    "security_revision": user._identity_context["security_revision"],
                    "guest": getattr(user, "is_guest", False)}
        release = slots.acquire(peer_bucket(request.META.get("REMOTE_ADDR")))
        if release is None:
            raise Problem("too_many_streams", 429, retry_after=HEARTBEAT)
        content = stream(adapter, topics, snapshot, release)
        # A generator that never starts never runs its finally block.
        weakref.finalize(content, release)
        response = EventStream(content, release, content_type="text/event-stream")
        response["Cache-Control"] = "no-store"
        response["X-Accel-Buffering"] = "no"
        return response
    except Problem as exc:
        return problem_response(exc)
