"""Authenticated loopback transport. Responses never become public error text."""

import asyncio
import json
import os
import stat
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx


def native_batches(topics, base="http://127.0.0.1:8080"):
    from .runtime import safe_item
    grouped = {}
    for topic in topics:
        parts = topic.split("/")
        if len(parts) != 4 or parts[:2] != ["openhab", "items"] or not safe_item(parts[2]) or parts[3] not in {"stateupdated", "statechanged", "command"} or parts[3] == "command" and "_pairing" in parts[2]:
            raise ValueError("Invalid native topic")
        prefix, verb = topic.rsplit("/", 1)
        grouped.setdefault(prefix, set()).add(verb)
    compact = []
    for prefix, verbs in sorted(grouped.items()):
        if verbs == {"stateupdated", "statechanged", "command"} and "_pairing" not in prefix:
            compact.append(prefix + "/*")
        else:
            compact.extend(prefix + "/" + verb for verb in sorted(verbs))
    prefix = base + "/rest/events?topics="
    queries, current = [], []
    for topic in compact:
        candidate = quote(",".join([*current, topic]), safe="/,*")
        if len((prefix + candidate).encode()) > 6000:
            if not current:
                raise ValueError("Runtime topic exceeds the native URL budget")
            queries.append(quote(",".join(current), safe="/,*"))
            current = [topic]
            if len((prefix + quote(topic, safe="/,*")).encode()) > 6000:
                raise ValueError("Runtime topic exceeds the native URL budget")
        else:
            current.append(topic)
    if current:
        queries.append(quote(",".join(current), safe="/,*"))
    if len(queries) > 4:
        raise ValueError("Runtime topic batch count exceeds four")
    return queries


def native_topics(topics):
    batches = native_batches(topics)
    if len(batches) != 1:
        raise ValueError("More than one native topic batch required")
    return batches[0]


class LoopbackTransport:
    projects_events = True
    def __init__(self, base, token_file, timeout=5):
        address = urlsplit(base)
        if address.scheme != "http" or address.hostname not in {"127.0.0.1", "::1"} or address.username or address.password or address.path not in {"", "/"} or address.query or address.fragment:
            raise ValueError("Runtime URL must be an explicit loopback HTTP origin")
        path = Path(token_file)
        info = path.lstat()
        if path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise ValueError("Runtime bearer file must be service-owned and mode 0600")
        self.base = base.rstrip("/")
        self.headers = {"Authorization": "Bearer " + path.read_text().strip()}  # lint: allow (scheme only; credential read from private file)
        self.timeout = timeout

    def read(self, item):
        with httpx.Client(trust_env=False, follow_redirects=False, timeout=self.timeout) as client:
            response = client.get(self.base + "/rest/items/" + quote(item, safe="") + "/state", headers=self.headers)
            response.raise_for_status()
            if len(response.content) > 65536:
                raise ValueError("Oversize runtime state")
            return response.text.strip()

    def command(self, item, value):
        with httpx.Client(trust_env=False, follow_redirects=False, timeout=self.timeout) as client:
            response = client.post(self.base + "/rest/items/" + quote(item, safe=""), content=value,
                                   headers={**self.headers, "Content-Type": "text/plain"})
            if response.status_code not in {200, 202}:
                raise ValueError("Runtime command failed")

    def group(self, item):
        with httpx.Client(trust_env=False, follow_redirects=False, timeout=self.timeout) as client:
            response = client.get(self.base + "/rest/items/" + quote(item, safe=""), params={"fields": "members(name,state)"}, headers=self.headers)
            response.raise_for_status()
            if len(response.content) > 262144:
                raise ValueError("Oversize runtime group")
            return response.json()

    def media(self, resource, method, body, maximum):
        with httpx.Client(trust_env=False, follow_redirects=False, timeout=15) as client:
            with client.stream(method, self.base + resource, content=body or None,
                               headers={**self.headers, "Content-Type": "application/sdp"}) as response:
                response.raise_for_status()
                data = bytearray()
                for chunk in response.iter_bytes():
                    if len(data) + len(chunk) > maximum:
                        raise ValueError("Oversize media")
                    data.extend(chunk)
                return response.headers.get("content-type", "").split(";")[0], bytes(data)

    async def _events(self, client, query):
        async with client.stream("GET", self.base + "/rest/events?topics=" + query, headers=self.headers) as response:
            response.raise_for_status()
            if response.headers.get("content-type", "").split(";")[0] != "text/event-stream":
                raise ValueError("Not an event stream")
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

    async def events(self, topics, *, project):
        queries = native_batches(topics, self.base)
        queue = asyncio.Queue(maxsize=64)
        pending = None
        tasks = []
        # Project before buffering: no raw private body enters the fan-in queue.
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False,
                                     timeout=httpx.Timeout(15, read=None)) as client:
            stopping = False

            async def pump(query):
                async for raw in self._events(client, query):
                    value = project(raw)
                    if value is not None:
                        await queue.put(value)
            tasks = [asyncio.create_task(pump(query)) for query in queries]

            def ended(finished):
                if not stopping:
                    for other in tasks:
                        if other is not finished:
                            other.cancel()
            for task in tasks:
                task.add_done_callback(ended)
            try:
                while tasks:
                    pending = asyncio.create_task(queue.get())
                    done, _ = await asyncio.wait([pending, *tasks], return_when=asyncio.FIRST_COMPLETED)
                    if any(task in done for task in tasks):
                        # A failed or closed shard makes the whole subscription
                        # unavailable; never silently serve a partial feed.
                        raise ValueError("Runtime event stream ended")
                    yield pending.result()
                    pending = None
            finally:
                stopping = True
                for task in [*tasks, *([pending] if pending else [])]:
                    task.cancel()
                await asyncio.gather(*tasks, *([pending] if pending else []), return_exceptions=True)
