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
from urllib.parse import urlsplit

import oh_ports

from . import AccessError
from .files import json_private, private_directory, read_private, require_service_account

CONFIG_FIELDS = {"version", "state", "contract_file", "runtime_url", "runtime_token_file", "allowed_hosts",
                 "bind_host", "port", "tls_cert", "tls_key", "timezone"}


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
        native = urlsplit(data["runtime_url"])
        if native.scheme != "http" or native.hostname not in {"127.0.0.1", "::1"} or native.username or native.password or native.path not in ("", "/") or native.query or native.fragment:
            raise ValueError()
        oh_ports.listener("access", data["port"], upstream=native.port or 80)
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
    setup(config)
    user = composition.initialize(config["state"], username, secret, config["allowed_hosts"])
    return {"initialized": True, "username": user.username, "state": config["state"]}


def _run_server(application, **options):
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

    server = Server(uvicorn.Config(application, **options))
    try:
        server.run()
    except KeyboardInterrupt:
        pass  # Match uvicorn.run's single-process signal handling.
    if not server.started:
        raise SystemExit(uvicorn.config.STARTUP_FAILURE)


def serve(config_path):
    require_service_account()
    config, _, _ = configured(config_path)
    from oh_identity.models import User
    if not User.objects.filter(is_active=True, role__reserved=True, role__name="Admin").exists():
        raise AccessError("Bootstrap a local admin before serving this site")
    from oh_identity.asgi import application
    # Direct TLS only. Proxy headers and forwarded client/origin claims are ignored.
    _run_server(application, host=config["bind_host"], port=config["port"], ssl_certfile=config["tls_cert"],
                ssl_keyfile=config["tls_key"], ssl_version=ssl.PROTOCOL_TLS_SERVER,
                proxy_headers=False, forwarded_allow_ips="", access_log=False,
                server_header=False, limit_concurrency=100, timeout_keep_alive=5,
                # Cancel long-lived SSE requests before systemd's 30s stop limit.
                timeout_graceful_shutdown=15,
                log_config=None, workers=1)
