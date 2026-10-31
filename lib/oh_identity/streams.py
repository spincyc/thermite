"""Credential-free SSE projection and bounded authorization revalidation."""

import asyncio
import fnmatch
import json

from asgiref.sync import sync_to_async
from django.contrib.sessions.models import Session
from django.http import StreamingHttpResponse
from django.utils import timezone

from .auth import failure, principal
from .models import PersonalToken, User
from .runtime import get_adapter, project
from .services import Problem, permissions

HEARTBEAT = 5
TYPES = {"stateupdated": "ItemStateUpdatedEvent", "statechanged": "ItemStateChangedEvent", "command": "ItemCommandEvent"}


def topics_for(contract, query):
    if not isinstance(query, str) or len(query) > 12000:
        raise Problem("invalid_topics")
    available = {"openhab/items/" + item + "/" + verb for item, spec in contract.states.items() for verb in TYPES
                 if verb != "command" or spec.get("commands") is True and "_pairing" not in item}
    out = set()
    for pattern in query.split(","):
        parts = pattern.split("/")
        if len(parts) != 4 or parts[:2] != ["openhab", "items"] or parts[3] not in {*TYPES, "*"}:
            raise Problem("invalid_topics")
        # A credential command family is forbidden before wildcard expansion.
        if "_pairing" in parts[2] and not parts[2].endswith(("_pairing_status", "_pairing_result")):
            raise Problem("invalid_topics")
        found = {topic for topic in available if fnmatch.fnmatchcase(topic, pattern)}
        if not found:
            raise Problem("invalid_topics")
        out.update(found)
    if not out or len(out) > 1000:
        raise Problem("invalid_topics")
    return sorted(out)


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
    user = User.objects.select_related("role").filter(pk=snapshot["user"], is_active=True, auth_revision=snapshot["revision"]).first()
    if not user or "site.read" not in permissions(user):
        return False
    if snapshot.get("token"):
        return PersonalToken.objects.filter(pk=snapshot["token"], revoked=False, expires_at__gt=timezone.now(), auth_revision=user.auth_revision).exists()
    return Session.objects.filter(session_key=snapshot.get("session"), expire_date__gt=timezone.now()).exists()


async def stream(adapter, topics, snapshot):
    projected = getattr(adapter.transport, "projects_events", False)
    iterable = adapter.transport.events(topics, project=lambda event: frame(adapter.contract, topics, event)) if projected else adapter.transport.events(topics)
    iterator = iterable.__aiter__()
    pending = None
    try:
        while await sync_to_async(valid)(snapshot):
            if pending is None:
                pending = asyncio.create_task(anext(iterator))
            done, _ = await asyncio.wait([pending], timeout=HEARTBEAT)
            if not done:
                yield b": heartbeat\n\n"
                continue
            try:
                event = pending.result()
            except StopAsyncIteration:
                return
            pending = None
            # Recheck after a quiet upstream wakes, not only before awaiting it.
            if not await sync_to_async(valid)(snapshot):
                return
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


def events(request):
    if request.method != "GET":
        return failure("method_not_allowed", 405)
    try:
        user = principal(request)
        if "site.read" not in permissions(user):
            raise Problem("forbidden", 403)
        adapter = get_adapter()
        topics = topics_for(adapter.contract, request.GET.get("topics", ""))
        token = getattr(request, "identity_token", None)
        snapshot = {"user": user.pk, "revision": user.auth_revision, "token": str(token.pk) if token else None,
                    "session": request.session.session_key if not token else None}
        response = StreamingHttpResponse(stream(adapter, topics, snapshot), content_type="text/event-stream")
        response["Cache-Control"] = "no-store"
        response["X-Accel-Buffering"] = "no"
        return response
    except Problem as exc:
        return failure(exc.code, exc.status)
