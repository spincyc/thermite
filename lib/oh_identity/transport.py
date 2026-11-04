"""Authenticated loopback transport. Responses never become public error text."""

import json
import os
import re
import stat
import threading
import weakref
from pathlib import Path
from urllib.parse import quote

import httpx

from oh_ports import NATIVE_URL

from .addresses import loopback_origin
from .trail import SOURCE


class CommandRefused(ValueError):
    """openHAB answered a command with another status than 200 or 202. Only
    the status is kept: the command's trail names it, never the body."""

    def __init__(self, status):
        super().__init__("Runtime command failed")
        self.status = status

#: An upstream topic filter: one verb of one exact Item, or of every Item.
FILTER = re.compile(r"openhab/items/(\*|[A-Za-z_][A-Za-z0-9_]*)/(stateupdated|statechanged|command)")
#: Bytes of one upstream request line, below Jetty's limit.
URL_BYTES = 6000
#: Bytes of one read of every Item's state.
STATES_BYTES = 8 * 1024 * 1024
#: Seconds an upstream subscription may go without a byte before it is
#: treated as dead and reconnected. A wildcard filter carries every Item's
#: updates and openHAB sends periodic ALIVE events, so a live connection is
#: rarely this quiet; a silently dropped one would otherwise freeze every
#: client stream the hub serves.
QUIET_SECONDS = 60


def native_query(filters, base=NATIVE_URL):
    """The topics query of one upstream subscription. A filter is an exact
    public topic, never a pairing command, or one verb of every Item; a
    wildcard reaches private Items too, so its consumer's projection must
    discard every topic it does not allow before reading a payload."""
    from .runtime import safe_item
    if not filters:
        raise ValueError("No runtime topics")
    for topic in filters:
        match = FILTER.fullmatch(topic) if isinstance(topic, str) else None
        if not match or match[1] != "*" and (not safe_item(match[1]) or match[2] == "command" and "_pairing" in match[1]):
            raise ValueError("Invalid native topic")
    query = quote(",".join(filters), safe="/,*")
    if len((base + "/rest/events?topics=" + query).encode()) > URL_BYTES:
        raise ValueError("Runtime topics exceed the native URL budget")
    return query


class LoopbackTransport:
    def __init__(self, base, token_file, timeout=5):
        try:
            loopback_origin(base)
        except ValueError:
            raise ValueError("Runtime URL must be an explicit loopback HTTP origin") from None
        path = Path(token_file)
        info = path.lstat()
        if path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise ValueError("Runtime bearer file must be service-owned and mode 0600")
        self.base = base.rstrip("/")
        self.headers = {"Authorization": "Bearer " + path.read_text().strip()}  # lint: allow (scheme only; credential read from private file)
        self.timeout = timeout
        self._guard = threading.Lock()
        self._clients = {}
        self._closed = False
        # Services close explicitly after their requests/tasks stop. This also
        # closes short-lived CLI transports and a normally exiting process.
        self._finalizer = weakref.finalize(self, self._close_clients, self._clients)

    @staticmethod
    def _close_clients(clients):
        for client in clients.values():
            client.close()

    def close(self):
        with self._guard:
            self._closed = True
            self._finalizer()

    def _client(self, kind):
        with self._guard:
            if self._closed:
                raise RuntimeError("Runtime transport is closed")
            if kind not in self._clients:
                # Snapshot/media traffic must not consume the connections
                # reserved for interactive commands. httpx.Client is shared
                # safely by Django's request threads and reuses keepalives.
                capacity = {"read": 8, "command": 4, "media": 2}[kind]
                self._clients[kind] = httpx.Client(
                    trust_env=False, follow_redirects=False, timeout=15 if kind == "media" else self.timeout,
                    limits=httpx.Limits(max_connections=capacity, max_keepalive_connections=capacity))
            return self._clients[kind]

    def read(self, item):
        response = self._client("read").get(self.base + "/rest/items/" + quote(item, safe="") + "/state", headers=self.headers)
        response.raise_for_status()
        if len(response.content) > 65536:
            raise ValueError("Oversize runtime state")
        return response.text.strip()

    def command(self, item, value, source=None):
        """source (thermite.<origin>:<id>, the command's trail ID) becomes
        the event source openHAB 5.2.3 records for the command: its REST
        ItemResource takes X-OpenHAB-Source and prefixes it to its own
        ("<source>=>org.openhab.core.io.rest$<user>"), which events.log and
        the routing rule's trigger event both show. Every write shares one
        service token, so this is how openHAB tells them apart. It is a
        label, never authority."""
        headers = {**self.headers, "Content-Type": "text/plain"}
        if source is not None:
            if not isinstance(source, str) or not SOURCE.fullmatch(source):
                raise ValueError("Invalid command source")
            headers["X-OpenHAB-Source"] = source
        response = self._client("command").post(self.base + "/rest/items/" + quote(item, safe=""), content=value,
                                               headers=headers)
        if response.status_code not in {200, 202}:
            raise CommandRefused(response.status_code)

    def states(self, names):
        """The raw states of these Items, read together in one request. openHAB
        lists every Item; only the named ones are kept, the rest never leave
        this method."""
        wanted = set(names)
        response = self._client("read").get(self.base + "/rest/items", params={"fields": "name,state"},
                                            headers=self.headers, timeout=15)
        response.raise_for_status()
        if len(response.content) > STATES_BYTES:
            raise ValueError("Oversize runtime states")
        rows = response.json()
        if not isinstance(rows, list):
            raise ValueError("Invalid runtime states")
        return {row["name"]: row["state"] for row in rows
                if isinstance(row, dict) and row.get("name") in wanted and isinstance(row.get("state"), str)}

    def group(self, item):
        response = self._client("read").get(self.base + "/rest/items/" + quote(item, safe=""), params={"fields": "members(name,state)"}, headers=self.headers)
        response.raise_for_status()
        if len(response.content) > 262144:
            raise ValueError("Oversize runtime group")
        return response.json()

    def media(self, resource, method, body, maximum):
        with self._client("media").stream(method, self.base + resource, content=body or None,
                                          headers={**self.headers, "Content-Type": "application/sdp"}) as response:
            response.raise_for_status()
            data = bytearray()
            for chunk in response.iter_bytes():
                if len(data) + len(chunk) > maximum:
                    raise ValueError("Oversize media")
                data.extend(chunk)
            return response.headers.get("content-type", "").split(";")[0], bytes(data)

    async def _events(self, client, query, opened=None):
        async with client.stream("GET", self.base + "/rest/events?topics=" + query, headers=self.headers) as response:
            response.raise_for_status()
            if response.headers.get("content-type", "").split(";")[0] != "text/event-stream":
                raise ValueError("Not an event stream")
            if opened:
                opened()
            data, buffer, size = [], bytearray(), 0
            # Yield small updates as they arrive. A fixed chunk size makes
            # httpx wait for that many bytes before exposing a quiet SSE frame.
            async for chunk in response.aiter_bytes():
                buffer.extend(chunk)
                while b"\n" in buffer:
                    raw, _, rest = buffer.partition(b"\n")
                    buffer = bytearray(rest)
                    size += len(raw)
                    if size > 65536:
                        raise ValueError("Oversize runtime event")
                    line = raw.rstrip(b"\r").decode("utf-8")
                    if not line:
                        if data:
                            yield json.loads("\n".join(data))
                        data, size = [], 0
                    elif line.startswith("data:"):
                        data.append(line[5:].lstrip(" "))
                if len(buffer) + size > 65536:
                    raise ValueError("Oversize runtime event")

    async def subscribe(self, filters, *, project, opened=None):
        """One upstream SSE connection for these topic filters (native_query).
        Yields project(event) for each event it keeps (not None), projected
        before anything is buffered. `opened` is called once openHAB has
        answered with an event stream: its subscription is then registered,
        so a state read after it cannot miss a later event."""
        query = native_query(filters, self.base)
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False,
                                     timeout=httpx.Timeout(15, read=QUIET_SECONDS)) as client:
            async for raw in self._events(client, query, opened):
                value = project(raw)
                if value is not None:
                    yield value
