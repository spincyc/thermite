"""Explicit, TLS-only site composition. No service starts on import."""

from contextlib import contextmanager
import ipaddress
import json
import os
from pathlib import Path
import re
import ssl
from urllib.parse import urlsplit

from . import AccessError
from .files import json_private, private_directory, read_private

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
        native.port  # also validate an explicit port before any service setup
        if type(data["port"]) is not int or not 1024 <= data["port"] <= 65535:
            raise ValueError()
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


def serve(config_path):
    config, _, _ = configured(config_path)
    from oh_identity.models import User
    if not User.objects.filter(is_active=True, role__reserved=True, role__name="Admin").exists():
        raise AccessError("Bootstrap a local admin before serving this site")
    from oh_identity.asgi import application
    import uvicorn
    # Direct TLS only. Proxy headers and forwarded client/origin claims are ignored.
    uvicorn.run(application, host=config["bind_host"], port=config["port"], ssl_certfile=config["tls_cert"],
                ssl_keyfile=config["tls_key"], ssl_version=ssl.PROTOCOL_TLS_SERVER,
                proxy_headers=False, forwarded_allow_ips="", access_log=False,
                server_header=False, limit_concurrency=100, timeout_keep_alive=5,
                log_config=None, workers=1)
