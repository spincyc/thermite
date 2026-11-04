"""Explicit, TLS-only site composition. No service starts on import."""

from contextlib import contextmanager
import asyncio
import ipaddress
import json
import logging
import os
from pathlib import Path
import re
import ssl

import oh_ports

from . import AccessError
from .files import json_private, private_directory, read_private, require_service_account

CONFIG_FIELDS = {"version", "state", "contract_file", "runtime_url", "runtime_token_file", "allowed_hosts",
                 "bind_host", "port", "tls_cert", "tls_key", "timezone"}


def _native_port(runtime_url):
    from oh_identity.addresses import loopback_origin
    return oh_ports.checked("native HTTP", loopback_origin(runtime_url).port or 80)


def validate_listeners(runtime_url, port, *, callback_port=None, host_ports=None):
    """Stage against the site's allocation; runtime loaders check actual endpoints."""
    selected = {"access": port}
    if callback_port is not None:
        selected["callbacks"] = callback_port
    return oh_ports.listeners(selected, upstream=_native_port(runtime_url), allocated=host_ports)


def load_config(path, *, tls=True):
    try:
        data = json.loads(read_private(path, 65536))
        if not isinstance(data, dict) or set(data) != CONFIG_FIELDS or data["version"] != 1:
            raise ValueError()
        for field in ("state", "contract_file", "runtime_token_file", "tls_cert", "tls_key"):
            if not isinstance(data[field], str) or not Path(data[field]).is_absolute():
                raise ValueError()
        hosts = data["allowed_hosts"]
        if not isinstance(hosts, list) or not hosts or any(not isinstance(x, str) or not re.fullmatch(r"[A-Za-z0-9.:[\]-]+", x) or x.startswith(".") or "*" in x for x in hosts):
            raise ValueError()
        ipaddress.ip_address(data["bind_host"])
        # Staging checks the full site allocation. This runtime configuration
        # names actual endpoints; defaults cannot describe relocated services.
        oh_ports.unique({"access": data["port"], "native HTTP": _native_port(data["runtime_url"])})
        from zoneinfo import ZoneInfo
        ZoneInfo(data["timezone"])
        private_directory(data["state"])
        # Reject unsafe custody even for worker/status setup.
        read_private(data["runtime_token_file"], 16384)
        read_private(data["contract_file"])
        if tls:
            read_private(data["tls_key"], 65536)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.load_cert_chain(data["tls_cert"], data["tls_key"])
    except (ValueError, KeyError, TypeError, OSError, ssl.SSLError):
        raise AccessError("Invalid access configuration, TLS material, or private file permissions") from None
    return data


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


def _run_server(application, *, on_shutdown=None, **options):
    import uvicorn

    class Server(uvicorn.Server):
        async def shutdown(self, sockets=None):
            await super().shutdown(sockets)
            # Uvicorn cancels over-budget requests, then re-raises SIGTERM.
            # Django also spawns response/stream tasks outside ServerState.tasks.
            # This loop belongs to this single-worker server: finish its normal
            # runner cancellation here, before SIGTERM can bypass runner cleanup.
            pending = asyncio.all_tasks() - {asyncio.current_task()}
            if pending:
                for task in pending:
                    if not task.cancelling():
                        task.cancel()
                done, unfinished = await asyncio.wait(pending, timeout=5)
                failed = sum(not task.cancelled() and task.exception() is not None for task in done)
                if unfinished or failed:
                    logging.getLogger("uvicorn.error").warning(
                        "Access shutdown cleanup incomplete: %d unfinished task(s), %d failed task(s).",
                        len(unfinished), failed)
            # capture_signals re-raises SIGTERM after shutdown returns, which
            # can bypass serve's outer finally. Drain/close before that point.
            if on_shutdown:
                on_shutdown()

    server = Server(uvicorn.Config(application, **options))
    try:
        server.run()
    except KeyboardInterrupt:
        pass  # Match uvicorn.run's single-process signal handling.
    if not server.started:
        raise SystemExit(uvicorn.config.STARTUP_FAILURE)


def serve(config_path):
    require_service_account()
    config, adapter, _ = configured(config_path)
    cleaned = False

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
        # Direct TLS only. Proxy headers and forwarded client/origin claims are ignored.
        _run_server(application, on_shutdown=cleanup,
                    host=config["bind_host"], port=config["port"], ssl_certfile=config["tls_cert"],
                    ssl_keyfile=config["tls_key"], ssl_version=ssl.PROTOCOL_TLS_SERVER,
                    proxy_headers=False, forwarded_allow_ips="", access_log=False,
                    server_header=False, limit_concurrency=100, timeout_keep_alive=5,
                    timeout_graceful_shutdown=15,
                    log_config=None, workers=1)
    finally:
        cleanup()
