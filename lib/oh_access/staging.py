"""Build a private persistent release and owner-installable units; never activate."""

import getpass
import json
import os
from pathlib import Path
import re
import pwd
import shutil
import subprocess
import sys
import venv

from . import AccessError
from .files import atomic_private, json_private, private_directory, read_private


def systemd(value):
    text = str(value)
    if any(ord(char) < 32 for char in text):
        raise AccessError("Unit paths must not contain control characters")
    return '"' + text.replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"') + '"'


def units(destination, user, *, state, callbacks=False):
    root = Path(destination).absolute()
    state = _absolute(state)
    if not re.fullmatch(r"[a-z_][a-z0-9_-]*", user) or user == "root":
        raise AccessError("Choose an existing unprivileged service user")
    command = [root / "env/bin/python", root / "program/bin/thermite", "access"]
    result = {}
    for name in (("serve", "worker", "callbacks") if callbacks else ("serve", "worker")):
        suffix = "" if name == "serve" else "-" + name
        config_name = "callbacks.json" if name == "callbacks" else "config.json"
        writable = "" if name == "callbacks" else "\nReadWritePaths=" + systemd(state)
        result["thermite-access" + suffix + ".service"] = (
            "[Unit]\nDescription=thermite " + {"worker": "room policy worker", "serve": "authenticated site", "callbacks": "verified UPnP callback ingress"}[name] +
            "\nAfter=network-online.target\nWants=network-online.target\n\n[Service]\nType=simple\nUser=" + user +
            "\nUMask=0077\nWorkingDirectory=" + systemd(root) +
            "\nExecStart=" + " ".join(systemd(x) for x in [*command, name, "--config", root / config_name]) +
            "\nRestart=on-failure\nRestartSec=5\nTimeoutStopSec=30\nNoNewPrivileges=yes\nPrivateTmp=yes\nProtectSystem=strict\nProtectHome=read-only" + writable +
            "\nRestrictAddressFamilies=AF_UNIX AF_INET AF_INET6\n\n[Install]\nWantedBy=multi-user.target\n")
    return result


def _absolute(value):
    return Path(os.path.abspath(Path(value).expanduser()))


def _disposable(path):
    return ".scratch" in path.parts or "worktrees" in path.parts


def _overlap(first, second):
    return first == second or first in second.parents or second in first.parents


def _state_location(state, target, area):
    """Keep upgrade authority outside code, every recognized release and workspaces."""
    roots = [area]
    for ancestor in (area, *area.parents):
        if (ancestor / ".git").exists():
            roots.append(ancestor)
            break
    if _disposable(state) or any(_overlap(state, root) for root in (target, *roots)):
        raise AccessError("Persistent state must be separate from releases, source checkouts and disposable workspaces")
    for ancestor in (state, *state.parents):
        if (ancestor / "stage.json").exists() or ((ancestor / "program").is_dir() and (ancestor / "config.json").exists()):
            raise AccessError("Persistent state must not be inside an existing release")


def stage(destination, *, state, house, assets, runtime_url, runtime_token_file, tls_cert, tls_key,
          allowed_hosts, bind_host, port, timezone, user=None, wheelhouse=None, area=None,
          install_environment=True, callbacks=None):
    """A new release with explicit reusable site state. No database is migrated.

    Owner installs the units only AFTER resolving the activation checklist. The
    program and assets are copied, so no service points into a disposable checkout.
    """
    from .service import compile_contract, load_config
    area = _absolute(area or Path(__file__).resolve().parents[2])
    target = _absolute(destination)
    state = _absolute(state)
    if target.exists():
        raise AccessError("Stage into a new persistent release directory")
    if target == area or area in target.parents:
        raise AccessError("Release directory must be outside the source checkout")
    if _disposable(target):
        raise AccessError("Services must not point into a disposable workspace")
    _state_location(state, target, area)
    current_user = pwd.getpwuid(os.geteuid()).pw_name
    user = user or current_user
    if user != current_user:
        raise AccessError("Stage as the unprivileged unit user so private file ownership is correct")
    callback_config = None
    if callbacks is not None:
        from .callback_service import load_config as load_callbacks
        callback_config = load_callbacks(callbacks, runtime_url=runtime_url)
    generated_units = units(target, user, state=state, callbacks=callback_config is not None)
    # Validate/create only the owned directory. Staging never initializes keys,
    # changes site identity, opens SQLite, migrates schemas or takes worker locks.
    private_directory(state)
    private_directory(target)
    private_directory(target / "secrets")
    private_directory(target / "units")
    if callback_config is not None:
        json_private(target / "callbacks.json", callback_config)
    # The explicitly supplied files are local custody inputs, not configuration
    # read from Git. Their contents never enter stdout, receipts, or unit text.
    for source, name in ((runtime_token_file, "runtime-bearer"), (tls_key, "tls-key.pem")):
        atomic_private(target / "secrets" / name, read_private(source, 65536))
    certificate = Path(tls_cert).read_bytes()
    atomic_private(target / "secrets/tls-cert.pem", certificate)
    program = target / "program"
    program.mkdir(mode=0o700)
    shutil.copytree(area / "lib", program / "lib", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (program / "bin").mkdir(mode=0o700)
    shutil.copy2(area / "bin/thermite", program / "bin/thermite")
    private_assets = target / "private-assets"
    private_directory(private_assets)
    from oh_features.remote import FILES
    for name in FILES:
        source = Path(assets) / name
        if source.is_symlink() or not source.is_file():
            raise AccessError("Private asset inventory is incomplete")
        atomic_private(private_assets / name, source.read_bytes())
    compile_contract(house, private_assets, target / "contract.json")
    config = {"version": 1, "state": str(state), "contract_file": str(target / "contract.json"),
              "runtime_url": runtime_url, "runtime_token_file": str(target / "secrets/runtime-bearer"),
              "allowed_hosts": allowed_hosts, "bind_host": bind_host, "port": port, "timezone": timezone,
              "tls_cert": str(target / "secrets/tls-cert.pem"), "tls_key": str(target / "secrets/tls-key.pem")}
    json_private(target / "config.json", config)
    load_config(target / "config.json")
    if install_environment:
        venv.EnvBuilder(with_pip=True).create(target / "env")
        argv = [str(target / "env/bin/python"), "-m", "pip", "install", "--require-hashes",
                "-r", str(program / "lib/requirements-site.txt")]
        if wheelhouse:
            argv.extend(["--no-index", "--find-links", str(Path(wheelhouse).absolute())])
        completed = subprocess.run(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        if completed.returncode:
            raise AccessError("Pinned service environment installation failed")
    for name, source in generated_units.items():
        atomic_private(target / "units" / name, source.encode())
    requirements = [
        "Bootstrap the first admin locally only for a genuinely new site; reuse this persistent state for upgrades and never bootstrap an existing site again.",
        "Install and trust an owner-managed TLS certificate for each client; never disable certificate validation.",
        "Seal every native HTTP/HTTPS/websocket/alternate-UI listener before exposing this as the security boundary.",
        "Disable implicit native user roles and trusted-network authentication grants.",
        "Preserve and verify native Sonos/UPnP callbacks before loopback listener cutover; no callback relay is enabled by this bundle.",
        "Confirm one worker, shared state/locks, native bearer permissions, and service user ownership.",
        "Only then may the owner install these unit files, daemon-reload, and enable/start the services."]
    if callback_config is not None:
        requirements[4] = "The optional callback listener is staged, not activated: verify exact native SUBSCRIBE advertised host/port/path and publishing peer, accepted NOTIFY to Item-state readback, renewal and native listener closure before cutover."
    receipt = {"version": 1, "staged": True, "activated": False, "security_boundary_active": False,
               "destination": str(target), "persistent_state": str(state), "config": str(target / "config.json"), "units": list(generated_units),
               "environment_installed": install_environment, "activation_requirements": requirements,
               "callbacks_staged": callback_config is not None, "callbacks_activated": False,
               "owner_commands": [["sudo", "install", "-m", "0644", str(target / "units" / name), "/etc/systemd/system/" + name] for name in generated_units] +
                                 [["sudo", "systemctl", "daemon-reload"],
                                  ["sudo", "systemctl", "enable", "--now", *generated_units]]}
    json_private(target / "stage.json", receipt)
    return receipt
