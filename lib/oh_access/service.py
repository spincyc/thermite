"""Explicit site composition: TLS, plus the opt-in plain-HTTP Guest listener
(oh_access.guest). No service starts on import."""

from contextlib import contextmanager
import asyncio
import copy
import ipaddress
import json
import logging
import os
from pathlib import Path
import re
import signal
import ssl
import sys

import oh_ports

from . import AccessError, guest, metrics
from .files import json_private, private_directory, read_private, require_service_account

CONFIG_FIELDS = {"version", "state", "contract_file", "runtime_url", "runtime_token_file", "allowed_hosts",
                 "bind_host", "port", "tls_cert", "tls_key", "timezone", "guest_http"}
#: Version 2 added guest_http: the optional plain-HTTP Guest listener, or null.
CONFIG_VERSION = 2
# Uvicorn's exit status for a server that never started; systemd sees a failure.
STARTUP_FAILURE = 3
# In-flight HTTP requests across the gateway; every HTTP/2 stream counts.
MAX_REQUESTS = 100
# After the shutdown deadline tells the requests still running that their
# client has gone, they and their connections get this long to end. Requests
# that ignore it are then cancelled, and their connections get as long again.
CLOSE_SECONDS = 2
# Once the listeners have drained, leftover tasks get this long to cancel.
CANCEL_SECONDS = 5


def _native_port(runtime_url):
    from oh_identity.addresses import loopback_origin
    return oh_ports.checked("native HTTP", loopback_origin(runtime_url).port or 80)


def validate_listeners(runtime_url, port, *, callback_port=None, guest_port=None, host_ports=None):
    """Stage against the site's allocation; runtime loaders check actual endpoints."""
    selected = {"access": port}
    if callback_port is not None:
        selected["callbacks"] = callback_port
    if guest_port is not None:
        selected["guest_http"] = guest_port
    return oh_ports.listeners(selected, upstream=_native_port(runtime_url), allocated=host_ports)


def load_config(path, *, tls=True):
    try:
        data = json.loads(read_private(path, 65536))
        if not isinstance(data, dict) or set(data) != CONFIG_FIELDS or data["version"] != CONFIG_VERSION:
            raise ValueError()
        for field in ("state", "contract_file", "runtime_token_file", "tls_cert", "tls_key"):
            if not isinstance(data[field], str) or not Path(data[field]).is_absolute():
                raise ValueError()
        hosts = data["allowed_hosts"]
        if not isinstance(hosts, list) or not hosts or any(not isinstance(x, str) or not re.fullmatch(r"[A-Za-z0-9.:[\]-]+", x) or x.startswith(".") or "*" in x for x in hosts):
            raise ValueError()
        ipaddress.ip_address(data["bind_host"])
        guest_http = data["guest_http"]
        if guest_http is not None and guest.settings(guest_http) != guest_http:
            raise ValueError()
        # Staging checks the full site allocation. This runtime configuration
        # names actual endpoints; defaults cannot describe relocated services.
        oh_ports.unique({"access": data["port"], "native HTTP": _native_port(data["runtime_url"]),
                         **({"guest HTTP": guest_http["port"]} if guest_http else {})})
        from zoneinfo import ZoneInfo
        ZoneInfo(data["timezone"])
        private_directory(data["state"])
        # Reject unsafe custody even for worker/status setup.
        read_private(data["runtime_token_file"], 16384)
        read_private(data["contract_file"])
        if tls:
            read_private(data["tls_key"], 65536)
            tls_context(data["tls_cert"], data["tls_key"])
    except (ValueError, KeyError, TypeError, OSError, ssl.SSLError):
        raise AccessError("Invalid access configuration, TLS material, or private file permissions") from None
    return data


def tls_context(cert, key):
    """The gateway's TLS: version 1.2 or later, offering HTTP/2 before HTTP/1.1."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    # RFC 9113 section 9.2: HTTP/2 over TLS 1.2 forbids compression and
    # renegotiation and needs an ephemeral AEAD suite. TLS 1.3 is unaffected.
    context.options |= ssl.OP_NO_COMPRESSION | ssl.OP_NO_RENEGOTIATION
    context.set_ciphers("ECDHE+AESGCM:ECDHE+CHACHA20")
    context.set_alpn_protocols(["h2", "http/1.1"])
    context.load_cert_chain(cert, key)
    return context


def setup(config, *, transport=None):
    """One database/contract/lock authority per process; injectable runtime I/O."""
    from oh_identity import composition
    from django.conf import settings
    if settings.configured and settings.SETTINGS_MODULE != "oh_identity.settings":
        raise AccessError("A site service cannot reuse another Django settings module")
    os.environ["DJANGO_SETTINGS_MODULE"] = "oh_identity.settings"
    composition.setup(config["state"], config["allowed_hosts"])
    if settings.TIME_ZONE != config["timezone"]:
        # setup imports settings, so timezone must be explicit before its first use.
        raise AccessError("Service timezone differs from the configured site")
    from oh_identity.runtime import RuntimeAdapter, RuntimeContract, install_adapter
    from oh_identity.transport import LoopbackTransport
    from oh_room_policy import FileLocks
    import oh_room_policy
    try:
        data = json.loads(read_private(config["contract_file"]))
        contract = RuntimeContract(**data)
    except (ValueError, TypeError, KeyError):
        raise AccessError("Invalid private runtime contract") from None
    transport = transport or LoopbackTransport(config["runtime_url"], config["runtime_token_file"])
    locks = FileLocks(Path(config["state"]) / "locks")

    @contextmanager
    def command_lock(rooms, resources):
        with locks.hold([*("room:" + room for room in rooms), *resources]):
            yield

    adapter = RuntimeAdapter(contract, transport, policy=oh_room_policy, command_lock=command_lock)
    install_adapter(adapter)
    return adapter, locks


def configured(path, *, tls=True, transport=None):
    config = load_config(path, tls=tls)
    os.environ["THERMITE_TIME_ZONE"] = config["timezone"]
    adapter, locks = setup(config, transport=transport)
    return config, adapter, locks


def compile_contract(house_file, asset_root, destination):
    """Owner-only immutable server authority from validated inventory/providers."""
    from oh_conf import House
    import oh_inventory
    import oh_render
    import oh_site_contract
    data = oh_inventory.load(Path(house_file))
    if oh_render.validate(data):
        raise AccessError("House configuration is invalid")
    root = Path(asset_root).absolute()
    if not root.is_dir() or root.is_symlink():
        raise AccessError("Private asset root must be an existing directory")
    contract = oh_site_contract.compile(House(data), oh_render.build(data), asset_root=root)
    json_private(destination, contract)
    return contract


def bootstrap(config_path, username, secret):
    """Only a trusted local CLI calls this with a hidden terminal password."""
    config = load_config(config_path, tls=False)
    os.environ["THERMITE_TIME_ZONE"] = config["timezone"]
    from oh_identity import composition
    adapter, _ = setup(config)
    with adapter.command_lock(tuple(sorted(adapter.contract.rooms)), ()):
        user = composition.initialize(config["state"], username, secret, config["allowed_hosts"])
    return {"initialized": True, "username": user.username, "state": config["state"]}


def rebase_timezone(adapter, zone):
    """Move stored policies to the configured site timezone, under every room
    lock, before either service evaluates a schedule."""
    from oh_identity.policy_store import rebase_timezone as rebase
    with adapter.command_lock(tuple(sorted(adapter.contract.rooms)), ()):
        return rebase(zone)


def prune_policies(config_path):
    """Owner operation: delete policies for rooms the compiled house no longer has."""
    require_service_account()
    _, adapter, _ = configured(config_path, tls=False)
    check_schema()
    from oh_identity.policy_store import prune_unknown_rooms
    return {"pruned": prune_unknown_rooms(tuple(adapter.contract.rooms))}


def migrate(config_path):
    """Explicit owner operation, after backing up state and stopping access services."""
    require_service_account()
    config, _, _ = configured(config_path, tls=False)
    from django.core.management import call_command
    call_command("migrate", interactive=False, verbosity=0)
    return {"migrated": True, "state": config["state"]}


def check_schema():
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor
    executor = MigrationExecutor(connection)
    if executor.migration_plan(executor.loader.graph.leaf_nodes()):
        raise AccessError("Back up identity state and run thermite access migrate before serving this site")


def prepare_setup(config_path):
    """Arm the first web administrator setup; the caller owns terminal-only output."""
    require_service_account()
    config = load_config(config_path, tls=False)
    # A fresh site needs its schema before the gateway can serve the setup page.
    # Existing databases are upgraded only by the explicit migrate command.
    fresh = not (Path(config["state"]) / "identity.sqlite3").exists()
    configured(config_path, tls=False)
    if fresh:
        from django.core.management import call_command
        call_command("migrate", interactive=False, verbosity=0)
    check_schema()
    from oh_identity.security import arm_setup
    return arm_setup()


class Admission:
    """The ASGI boundary in front of the site. Only HTTP reaches the
    application: lifespan is answered here and a WebSocket handshake is
    refused with 403. At most `limit` HTTP requests run at once, each HTTP/2
    stream counting alone, so streams reset by their client still count until
    their handler returns. A request over the limit is answered 503 at once,
    as uvicorn's limit_concurrency answered it. Listeners that share one
    instance share one budget."""

    UNAVAILABLE = b"Service Unavailable"

    def __init__(self, application, limit=MAX_REQUESTS):
        self.application, self.limit, self.active = application, limit, 0

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            if self.active >= self.limit:
                # One rate-limited warning; the request never reaches the
                # trail's request line.
                from oh_identity.trail import warnings
                warnings.warning(("gateway_busy",), "gateway_busy", status=503, active=self.active, limit=self.limit)
                await send({"type": "http.response.start", "status": 503, "headers": [
                    (b"content-type", b"text/plain; charset=utf-8"),
                    (b"content-length", b"%d" % len(self.UNAVAILABLE)), (b"connection", b"close")]})
                await send({"type": "http.response.body", "body": self.UNAVAILABLE})
                return
            self.active += 1
            try:
                await self.application(scope, receive, send)
            finally:
                self.active -= 1
        elif scope["type"] == "lifespan":
            while (await receive())["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            await send({"type": "lifespan.shutdown.complete"})
        else:
            # Hypercorn's only other scope. Closing a WebSocket before
            # accepting it answers HTTP 403.
            await send({"type": "websocket.close"})


def listener(host, port, tls=None, clients=None):
    """Hypercorn settings for one gateway listener; tls None serves plain HTTP.
    clients (oh_access.guest.Clients) closes connections from elsewhere."""
    from hypercorn.config import Config

    class Listener(Config):
        # Serve the context tls_context() built, not one from certificate paths.
        ssl_enabled = tls is not None

        def create_ssl_context(self):
            return tls

        def create_sockets(self):
            sockets = super().create_sockets()
            if clients is not None:
                sockets.insecure_sockets[:] = map(clients.only, sockets.insecure_sockets)
            return sockets

    config = Listener()
    config.bind = [f"[{host}]:{port}" if ipaddress.ip_address(host).version == 6 else f"{host}:{port}"]
    # No access log and no Server header. Errors use the logger Django's
    # LOGGING sends to the journal; hypercorn configures no logging itself.
    config.accesslog = None
    config.errorlog = logging.getLogger("hypercorn.error")
    config.include_server_header = False
    config.keep_alive_timeout = 5
    config.graceful_timeout = 15
    # Hypercorn ends a connection after this many requests, and over HTTP/2
    # that drops every event stream it carries. A browser shares one
    # connection between its pages, each polling policy every 5 s.
    config.keep_alive_max_requests = sys.maxsize
    config.h11_max_incomplete_size = 16 * 1024
    # One browser holds at most 8 event streams (oh_identity/streams.py
    # PEER_STREAMS), which leaves 24 streams for reads, commands and assets;
    # a browser queues the rest. Header lists are bounded as HTTP/1.1 heads
    # are, and inbound frames to the protocol's minimum size.
    config.h2_max_concurrent_streams = 32
    config.h2_max_header_list_size = 16 * 1024
    config.h2_max_inbound_frame_size = 2 ** 14
    return config


def _run_server(listeners, *, on_shutdown=None):
    """Serve (ASGI application, listener config) pairs in one event loop until
    SIGTERM or SIGINT, then drain them together."""
    try:
        bound = [(application, config, config.create_sockets()) for application, config in listeners]
    except OSError as error:
        logging.getLogger("hypercorn.error").error("Access server could not bind: %s", error)
        raise SystemExit(STARTUP_FAILURE) from None
    asyncio.run(_serve(bound, on_shutdown=on_shutdown))


async def _serve(bound, *, on_shutdown=None, stop=None):
    # hypercorn.asyncio.serve cannot take sockets bound before the loop starts.
    from hypercorn.app_wrappers import ASGIWrapper
    from hypercorn.asyncio.run import worker_serve

    if stop is None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(signum, stop.set)

    requests = set()
    # Done at the shutdown deadline.
    gone = asyncio.get_running_loop().create_future()

    def tracked(application):
        async def handle(scope, receive, send):
            if scope["type"] == "lifespan":
                return await application(scope, receive, send)

            async def until_gone():
                # From the deadline a request hears that its client has gone,
                # as ASGI tells it of a closed connection. Django then ends
                # the request and the tasks it started for it itself.
                if not gone.done():
                    message = asyncio.ensure_future(receive())
                    try:
                        await asyncio.wait((message, gone), return_when=asyncio.FIRST_COMPLETED)
                    finally:
                        if not message.done():
                            message.cancel()
                    if message.done():
                        return message.result()
                return {"type": "http.disconnect"}

            # Hypercorn runs each request in its own task, inside its
            # connection's task group.
            requests.add(task := asyncio.current_task())
            try:
                await application(scope, until_gone, send)
            finally:
                requests.discard(task)
        return handle

    async def listen(application, config, sockets):
        # Hypercorn waits for its connections with no deadline of its own;
        # the one config.graceful_timeout sets is kept below.
        config = copy.copy(config)
        config.graceful_timeout = None
        try:
            await worker_serve(ASGIWrapper(tracked(application)), config, sockets=sockets, shutdown_trigger=stop.wait)
        finally:
            stop.set()  # One listener ending ends them all.

    listeners = [asyncio.create_task(listen(*each)) for each in bound]
    try:
        await stop.wait()
        # Keep the deadline here, the same on every Python. Hypercorn's own
        # graceful_timeout starts once asyncio.Server.wait_closed() returns:
        # from Python 3.12.1 that waits for every connection to end, so one
        # held event stream would hold shutdown until systemd kills the unit.
        # On 3.11 it returns at once, and Hypercorn's timeout would cancel
        # each connection with its requests still running: the failure
        # described below.
        grace = max(config.graceful_timeout for _, config, _ in bound)
        _, overdue = await asyncio.wait(listeners, timeout=grace)
        if overdue:
            # Tell the requests still running that their client has gone.
            gone.set_result(None)
            _, overdue = await asyncio.wait(overdue, timeout=CLOSE_SECONDS)
        if overdue and requests:
            # Cancel the requests that ignored it, not their connections. A
            # request's last act closes its HTTP/2 stream, which restarts the
            # connection's idle timer in the connection's task group; were
            # that group already cancelled, the request would fail with
            # "TaskGroup is shutting down" and the connection with it.
            # Without requests, each terminated connection closes itself.
            for task in requests:
                task.cancel()
            _, overdue = await asyncio.wait(overdue, timeout=CLOSE_SECONDS)
        for task in overdue:
            task.cancel()
        ended = await asyncio.gather(*listeners, return_exceptions=True)
    finally:
        # Cancel what is left: connections that did not close in time, and
        # tasks that cancelled requests left behind, as Django's would be.
        # Then drain and close before the loop and the process end.
        pending = asyncio.all_tasks() - {asyncio.current_task()}
        if pending:
            for task in pending:
                if not task.cancelling():
                    task.cancel()
            done, unfinished = await asyncio.wait(pending, timeout=CANCEL_SECONDS)
            failed = sum(not task.cancelled() and task.exception() is not None for task in done)
            if unfinished or failed:
                logging.getLogger("hypercorn.error").warning(
                    "Access shutdown cleanup incomplete: %d unfinished task(s), %d failed task(s).",
                    len(unfinished), failed)
        if on_shutdown:
            on_shutdown()
    for result in ended:
        if isinstance(result, Exception):  # A listener cancelled at the deadline is no failure.
            raise result


def guest_listeners(config, admission):
    """(application, listener) pairs for the opt-in Guest listener, one per
    address. They share admission, so its request budget, with HTTPS; each
    request carries the listener in its ASGI scope (guest.mark)."""
    settings = config["guest_http"]
    if settings is None:
        return []
    secure = guest.origin("https", config["allowed_hosts"][0].strip("[]"), config["port"])
    application = guest.mark(admission, guest.Listener(frozenset(guest.origins(settings)), secure))
    clients = guest.Clients(settings["clients"])
    return [(application, listener(address, settings["port"], clients=clients)) for address in settings["listen"]]


def serve(config_path):
    require_service_account()
    config, adapter, _ = configured(config_path)
    cleaned, textfile = False, None

    def cleanup():
        nonlocal cleaned
        if cleaned:
            return
        cleaned = True
        from oh_identity.batches import commands
        # Drain admitted batch jobs before closing the pools they may use.
        commands.shutdown()
        close = getattr(adapter.transport, "close", None)
        if close:
            close()

    try:
        check_schema()
        from oh_identity.models import User
        from oh_identity.security import get_state
        if get_state().enabled and not User.objects.filter(is_active=True, admin=True).exists():
            raise AccessError("An authenticated site requires an active administrator")
        rebase_timezone(adapter, config["timezone"])
        from oh_identity.asgi import application
        # Direct TLS, and plain HTTP only on the opt-in Guest listener.
        # Hypercorn trusts no proxy or forwarded header: the ASGI client is
        # always the socket peer.
        tls = tls_context(config["tls_cert"], config["tls_key"])
        # Both listeners share one metered admission: Metered sees its 503s
        # and labels each request with its listener (guest.mark sets it).
        # Metrics go to a textfile (oh_access.metrics), never to a listener.
        admission = metrics.Metered(Admission(application))
        metrics.watch_streams()
        textfile = metrics.start(metrics.GATEWAY, metrics.GATEWAY_FILE, refresh=metrics.refresh_streams)
        _run_server([(admission, listener(config["bind_host"], config["port"], tls)),
                     *guest_listeners(config, admission)], on_shutdown=cleanup)
    finally:
        cleanup()
        if textfile:
            textfile.stop()
