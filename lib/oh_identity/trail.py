"""The command trail: one key=value journal line per command, refusal,
security event, gateway request and event stream.

Every command the gateway or the policy worker sends to openHAB gets a short
ID. Its journal line names who sent it and from where, and openHAB sees the
same ID as the command's event source (X-OpenHAB-Source), so the gateway
line, events.log and the routing rule's own lines can be joined:

    cmd id=1a2b3c4d origin=web client=web/5e6f7a8b user=owner principal=session
        token=- peer=9c0d1e2f req=3a4b5c6d room=den action=source
        item=loc_den_source value=off outcome=accepted ms=41

origin is web, tui, guest or api (a request that named no client), all_off,
schedule or repair (the worker), or local (a trusted local call). Values are
validated command values, never pairing codes (always <private>), bodies,
credentials or addresses: peer is the first 8 characters of the audit's keyed
peer digest. Nothing here is stored; the journal is the trail.

Importing this module loads no framework (oh_access.guest loads none
either): the gateway, the worker and the room-policy engine share it.
"""

from dataclasses import dataclass, replace
import hashlib
import hmac
import json
import logging
import re
import secrets
import threading
import time

from oh_access.guest import SCOPE as GUEST_SCOPE

LOG = logging.getLogger("oh_identity.trail")
#: ASGI scope keys. Request data never reaches a scope's top level, so no
#: client can set them. GUEST_SCOPE is the plain-HTTP Guest listener's mark
#: on its requests (oh_identity.guest.listener reads the same key).
REQUEST_ID = "thermite.request_id"
#: A client's own label: web/<tab id> from the web remote, tui/<host> from
#: the terminal remote. It is a claim, never authority.
CLIENT = re.compile(r"(web|tui)/([A-Za-z0-9][A-Za-z0-9._-]{0,62})")
ORIGINS = frozenset({"web", "tui", "guest", "api", "all_off", "schedule", "repair", "local"})
ID = re.compile(r"[0-9a-f]{8}")
#: openHAB's event source for a command: thermite.<origin>:<id>. openHAB
#: 5.2.3 prefixes it to its own REST source ("<this>=>org.openhab.core.io.rest$user").
SOURCE = re.compile(r"thermite\.([a-z_]{2,16}):([0-9a-f]{8})")
PRIVATE = "<private>"
# Values that print as themselves; anything else is JSON-quoted, so a value
# can never break a line or forge another field.
_PLAIN = re.compile(r"[A-Za-z0-9_.:/@+<>()=,-]{1,120}")
WARN_EVERY = 300


def new_id():
    return secrets.token_hex(4)


def text(value):
    if value is None or value == "":
        return "-"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return format(value, "g")
    value = str(value)
    return value if _PLAIN.fullmatch(value) else json.dumps(value[:120], ensure_ascii=True)


def line(kind, **fields):
    return kind + "".join(" " + key + "=" + text(value) for key, value in fields.items())


def milliseconds(started, clock=time.monotonic):
    return max(0, round((clock() - started) * 1000))


def peer_digest(address):
    """The audit's keyed, non-reversible label for a peer's address bucket
    (one IPv4 address or IPv6 /64). Needs Django settings."""
    from django.conf import settings
    from .addresses import peer_bucket
    bucket = peer_bucket(address)
    return hmac.new(settings.SECRET_KEY.encode(), ("audit-peer\0" + bucket).encode(), hashlib.sha256).hexdigest()[:32]


def short(digest):
    return digest[:8] if isinstance(digest, str) and digest else None


def cause(error):
    """What a failed openHAB call says about itself: its HTTP status, else
    its exception class. Never a message or a body."""
    status = getattr(error, "status", None)
    if status is None:
        status = getattr(getattr(error, "response", None), "status_code", None)
    if type(status) is int and 100 <= status <= 599:
        return "http=" + str(status)
    return "exc=" + type(error).__name__


@dataclass(frozen=True)
class Origin:
    """Where a command came from. client is the client's own label; request
    the gateway request ID; batch an All off batch ID."""
    origin: str = "local"
    client: str | None = None
    request: str | None = None
    batch: str | None = None

    def __post_init__(self):
        if self.origin not in ORIGINS:
            raise ValueError("Unknown command origin")


def origin(request):
    """A gateway request's Origin. The Guest listener's mark (its scope key,
    as oh_identity.guest.listener reads it) wins over any header; such a
    request carries no credential, so its principal is guest. A header is a
    label only and is ignored unless well formed."""
    scope = getattr(request, "scope", None)
    scope = scope if isinstance(scope, dict) else {}
    label = request.headers.get("X-Thermite-Client", "")
    match = CLIENT.fullmatch(label) if isinstance(label, str) else None
    request_id = scope.get(REQUEST_ID)
    request_id = request_id if isinstance(request_id, str) and ID.fullmatch(request_id) else None
    kind = "guest" if scope.get(GUEST_SCOPE) else match.group(1) if match else "api"
    return Origin(kind, match.group(0) if match else None, request_id)


def principal(user):
    """(user, principal, token) for a command's line, from the credential
    the request authenticated with."""
    context = getattr(user, "_identity_context", None) or {}
    if user is None:
        return None, "worker", None
    if getattr(user, "is_guest", False) or context.get("guest"):
        return "guest", "guest", None
    kind = "token" if context.get("token") else "session" if context.get("session") else "local"
    token = str(context["token"])[:8] if context.get("token") else None
    return getattr(user, "username", None), kind, token


class Command:
    """One command's trail: its ID, its openHAB event source and, when it
    ends, one INFO line."""

    def __init__(self, where, user, *, room, action, item, value, private=False, clock=time.monotonic):
        self.id, self.where, self.clock = new_id(), where or Origin(), clock
        self.user = user
        self.room, self.action, self.item = room, action, item
        self.value = PRIVATE if private else value
        self.started = clock()
        self.cause = None
        self.done = False

    @property
    def source(self):
        return "thermite." + self.where.origin + ":" + self.id

    def outcome(self, error):
        if error is None:
            return "accepted", {}
        code = getattr(error, "code", None)
        if code is None:
            return ("runtime_busy" if type(error).__name__ == "Busy" else "error(exc=" + type(error).__name__ + ")"), {}
        if code == "runtime_unavailable" and self.cause:
            return code + "(" + self.cause + ")", {}
        lock = getattr(error, "lock", None)
        if lock:
            return code, {"lock": lock[0], "lock_room": lock[1]}
        return code, {}

    def finish(self, error=None):
        """Log the command's outcome: accepted, a refusal's code, or the
        exception that ended it."""
        self._log(*self.outcome(error))

    def failed(self, error):
        """Log an openHAB call that failed: its HTTP status or exception class."""
        self.cause = cause(error)
        self._log("runtime_unavailable(" + self.cause + ")", {})

    def _log(self, outcome, extra):
        if self.done:
            return
        self.done = True
        username, kind, token = principal(self.user)
        context = getattr(self.user, "_identity_context", None) or {}
        batch = {"batch": self.where.batch} if self.where.batch else {}
        LOG.info(line("cmd", id=self.id, **batch, origin=self.where.origin, client=self.where.client,
                      user=username, principal=kind, token=token, peer=short(context.get("peer")),
                      req=self.where.request, room=self.room, action=self.action, item=self.item, value=self.value,
                      outcome=outcome, **extra, ms=milliseconds(self.started, self.clock)))


class Batch:
    """An All off request: one line for the batch, and each room's command
    its own child ID with origin all_off and this batch's ID."""

    def __init__(self, where, user):
        self.id, self.where, self.user = new_id(), where or Origin(), user
        self.child = replace(self.where, origin="all_off", batch=self.id)
        self.started = time.monotonic()

    def finish(self, *, rooms, accepted, skipped, outcome):
        username, kind, token = principal(self.user)
        context = getattr(self.user, "_identity_context", None) or {}
        LOG.info(line("all_off", id=self.id, origin=self.where.origin, client=self.where.client, user=username,
                      principal=kind, token=token, peer=short(context.get("peer")), req=self.where.request,
                      rooms=rooms, accepted=accepted, skipped=skipped, outcome=outcome,
                      ms=milliseconds(self.started)))


class Limited:
    """At most one warning per key and interval; suppressed ones are counted
    and reported with the next."""

    def __init__(self, every=WARN_EVERY, clock=time.monotonic, limit=256):
        self.every, self.clock, self.limit = every, clock, limit
        self._guard = threading.Lock()
        self._last = {}

    def warning(self, key, kind, **fields):
        now = self.clock()
        with self._guard:
            last, suppressed = self._last.get(key, (None, 0))
            if last is not None and now - last < self.every:
                self._last[key] = (last, suppressed + 1)
                return False
            if key not in self._last and len(self._last) >= self.limit:
                self._last.pop(next(iter(self._last)))
            self._last[key] = (now, 0)
        LOG.warning(line(kind, **fields, suppressed=suppressed))
        return True


warnings = Limited()


def policy_unavailable(where, room, error):
    """A saved policy that could not be evaluated: the room counts as locked."""
    kind = "no_engine" if error is None else type(error).__name__
    warnings.warning(("policy_unavailable", where, room, kind), "policy_unavailable", where=where, room=room,
                     exc=kind)


def audit(row, actor):
    """Mirror one security audit row to the journal."""
    LOG.info(line("audit", action=row.action, outcome=row.outcome, principal=row.principal, actor=row.actor_id,
                  user=getattr(actor, "username", None) if row.actor_id else None, target=row.target,
                  peer=short(row.peer), at=row.at.isoformat() if row.at else None))


def stream_opened(release, request, user, *, topics, counts):
    """Log an event stream's admission now and its end once, when release
    runs. counts() answers (streams of this peer, streams in total).
    Returns the release to use instead."""
    scope = getattr(request, "scope", None)
    request_id = scope.get(REQUEST_ID) if isinstance(scope, dict) else None
    username, kind, token = principal(user)
    context = getattr(user, "_identity_context", None) or {}
    fields = {"id": request_id, "user": username, "principal": kind, "token": token,
              "peer": short(context.get("peer")), "listener": "guest" if isinstance(scope, dict) and scope.get(GUEST_SCOPE) else None}
    started = time.monotonic()
    peer_count, total = counts()
    LOG.info(line("stream open", **fields, topics=topics, streams_peer=peer_count, streams_total=total))
    guard, ended = threading.Lock(), False

    def close():
        nonlocal ended
        with guard:
            if ended:
                return
            ended = True
        try:
            release()
        finally:
            peer_count, total = counts()
            LOG.info(line("stream close", **fields, ms=milliseconds(started), streams_peer=peer_count,
                          streams_total=total))
    return close


class RequestLog:
    """Pure ASGI: every HTTP response gets an X-Request-Id, and every request
    that changes something (not GET or HEAD) or is not answered 2xx gets one
    journal line. Successful reads, including the 5 s lock and policy polls
    and the assets, are not logged; event streams log their own lines
    (stream_opened). The ID is also in the scope for the command trail."""

    def __init__(self, application):
        self.application = application

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.application(scope, receive, send)
        request_id = new_id()
        scope = {**scope, REQUEST_ID: request_id}
        started, status = time.monotonic(), None

        async def respond(message):
            nonlocal status
            if message.get("type") == "http.response.start":
                status = message.get("status")
                message = {**message, "headers": [*message.get("headers", ()), (b"x-request-id", request_id.encode())]}
            await send(message)

        try:
            await self.application(scope, receive, respond)
        finally:
            method = scope.get("method")
            if method not in ("GET", "HEAD") or type(status) is not int or not 200 <= status < 300:
                self.record(scope, request_id, status, started)

    @staticmethod
    def record(scope, request_id, status, started):
        try:
            client = scope.get("client")
            peer = short(peer_digest(client[0])) if client else None
        except Exception:
            peer = None
        LOG.info(line("req", id=request_id, method=scope.get("method"), path=scope.get("path"),
                      status=status, ms=milliseconds(started), peer=peer,
                      listener="guest" if scope.get(GUEST_SCOPE) else None))
