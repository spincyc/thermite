"""Build a private persistent release and owner-installable units; never activate."""

import os
import json
from pathlib import Path
import shutil
import stat
import subprocess
import venv
from urllib.parse import urlsplit

import oh_ports
from oh_service import ACCOUNT, GROUP

from . import AccessError
from .files import (atomic_private, json_private, private_directory, read_private,
                    require_service_account, service_identity)


def systemd(value):
    text = str(value)
    if any(ord(char) < 32 for char in text):
        raise AccessError("Unit paths must not contain control characters")
    return '"' + text.replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"') + '"'


def units(destination, user=ACCOUNT, *, state, callbacks=False):
    root = Path(destination).absolute()
    state = _absolute(state)
    if user != ACCOUNT:
        raise AccessError("Access services require user thermite and group thermite")
    command = [root / "env/bin/python", root / "program/bin/thermite", "access"]
    result = {}
    for name in (("serve", "worker", "callbacks") if callbacks else ("serve", "worker")):
        suffix = "" if name == "serve" else "-" + name
        config_name = "callbacks.json" if name == "callbacks" else "config.json"
        writable = "" if name == "callbacks" else "\nReadWritePaths=" + systemd(state)
        result["thermite-access" + suffix + ".service"] = (
            "[Unit]\nDescription=thermite " + {"worker": "room policy worker", "serve": "authenticated site", "callbacks": "verified UPnP callback ingress"}[name] +
            "\nAfter=network-online.target\nWants=network-online.target\n\n[Service]\nType=simple\nUser=" + user +
            "\nGroup=" + GROUP + "\nUMask=0077\nWorkingDirectory=" + systemd(root) +
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
        try:
            if (ancestor / "stage.json").exists() or ((ancestor / "program").is_dir() and (ancestor / "config.json").exists()):
                raise AccessError("Persistent state must not be inside an existing release")
        except PermissionError:
            # A correctly private service-owned state root is opaque to the
            # staging operator. Its mode and owner are checked separately.
            if ancestor != state:
                raise AccessError("Cannot inspect persistent state parents") from None


def _no_symlinks(path):
    for parent in [*reversed(path.parents), path]:
        if parent.is_symlink():
            raise AccessError("Service paths must not contain symlinks")


def _service_parents(target, state, uid, gid):
    """Refuse releases hidden behind an operator's private HOME or other parent."""
    for path in {target.parent, state.parent}:
        _no_symlinks(path)
        if not path.is_dir():
            raise AccessError("Prepare existing service-accessible release and state parents before staging")
        for parent in [path, *path.parents]:
            info = parent.stat()
            bit = stat.S_IXUSR if info.st_uid == uid else stat.S_IXGRP if info.st_gid == gid else stat.S_IXOTH
            if not info.st_mode & bit:
                raise AccessError("Service parents must permit thermite traversal; use a prepared persistent location such as /srv/thermite-access")


def _state_directory(state, uid, gid):
    """Inspect only the state root: an upgrade must not open private authority."""
    _no_symlinks(state)
    if not state.exists():
        private_directory(state)
        return
    info = state.stat()
    if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700 or info.st_uid not in {os.geteuid(), uid}:
        raise AccessError("Persistent state must be mode 0700 and owned by the operator or thermite")
    if info.st_uid == uid and info.st_gid != gid:
        raise AccessError("Existing thermite state must also belong to group thermite")


def _inventory(root, *, release=False):
    """Finite custody inventory; never follow links or walk existing service state."""
    result = []
    pending = [root]
    device = root.lstat().st_dev
    while pending:
        path = pending.pop()
        info = path.lstat()
        if info.st_dev != device:
            raise AccessError("Private service trees must not cross filesystem mounts")
        if stat.S_ISDIR(info.st_mode):
            result.append((path, 0o700))
            pending.extend(sorted(path.iterdir(), reverse=True))
        elif stat.S_ISREG(info.st_mode):
            if info.st_nlink != 1:
                raise AccessError("Service custody must not change files linked outside their private tree")
            executable = release and path.relative_to(root).parts[0] in {"program", "env"} and info.st_mode & 0o111
            result.append((path, 0o700 if executable else 0o600))
        elif stat.S_ISLNK(info.st_mode) and release and root / "env" in path.parents and (root / "env") in path.resolve().parents:
            # venv's lib64 -> lib is the only usual link. It may not escape env.
            result.append((path, None))
        else:
            raise AccessError("Private service trees must contain only regular files, directories and internal environment links")
    return result


def _batches(paths, maximum=16000):
    batch, length = [], 0
    for path in paths:
        text = str(path)
        if batch and length + len(text) > maximum:
            yield batch
            batch, length = [], 0
        batch.append(text)
        length += len(text) + 1
    if batch:
        yield batch


def _handoff(inventory):
    commands = []
    for mode in (0o600, 0o700):
        for batch in _batches(path for path, wanted in inventory if wanted == mode):
            commands.append(["sudo", "chmod", format(mode, "04o"), "--", *batch])
    # The root is handed over last; no command relies on operator traversal later.
    for batch in _batches(path for path, _ in reversed(inventory)):
        commands.append(["sudo", "chown", "--no-dereference", ACCOUNT + ":" + GROUP, "--", *batch])
    return commands


def verify(config_path):
    """Read-only commissioning check, run under the same identity as the units."""
    from .service import load_config
    uid, gid = require_service_account()
    config_path = _absolute(config_path)
    try:
        state = Path(json.loads(read_private(config_path, 65536))["state"])
        if not state.is_absolute() or not state.is_dir():
            raise ValueError()
    except (ValueError, KeyError, TypeError):
        raise AccessError("Custody verification requires an existing persistent state directory") from None
    config = load_config(config_path)
    target, state = config_path.parent, Path(config["state"])
    _service_parents(target, state, uid, gid)
    count = 0
    for inventory in (_inventory(target, release=True), _inventory(state)):
        for path, mode in inventory:
            info = path.lstat()
            if info.st_uid != uid or info.st_gid != gid or mode is not None and stat.S_IMODE(info.st_mode) != mode:
                raise AccessError("Access custody verification failed: every private path must belong to thermite:thermite with private modes")
            count += 1
    return {"verified": True, "user": ACCOUNT, "group": GROUP, "paths": count,
            "destination": str(target), "persistent_state": str(state)}


def stage(destination, *, state, house, assets, runtime_url, runtime_token_file, tls_cert, tls_key,
          allowed_hosts, bind_host, port, timezone, user=None, wheelhouse=None, area=None,
          install_environment=True, callbacks=None):
    """A new release with explicit reusable site state. No database is migrated.

    Owner installs the units only AFTER resolving the activation checklist. The
    program and assets are copied, so no service points into a disposable checkout.
    """
    from .service import compile_contract, load_config
    from oh_paths import PROGRAM, SITE, SITE_FILE
    area = _absolute(area or SITE)
    resources = PROGRAM if (area / SITE_FILE).is_file() else area
    target = _absolute(destination)
    state = _absolute(state)
    if target.exists():
        raise AccessError("Stage into a new persistent release directory")
    if target == area or area in target.parents:
        raise AccessError("Release directory must be outside the source checkout")
    if _disposable(target):
        raise AccessError("Services must not point into a disposable workspace")
    _state_location(state, target, area)
    user = user or ACCOUNT
    if user != ACCOUNT:
        raise AccessError("Access services require user thermite and group thermite")
    uid, gid = service_identity()
    _service_parents(target, state, uid, gid)
    callback_config = None
    if callbacks is not None:
        from .callback_service import load_config as load_callbacks
        callback_config = load_callbacks(callbacks, runtime_url=runtime_url)
    if callback_config is not None:
        try:
            oh_ports.unique({"access": port, "callbacks": callback_config["port"],
                             "native HTTP": urlsplit(runtime_url).port or 80})
        except ValueError as exc:
            raise AccessError(str(exc)) from None
    generated_units = units(target, user, state=state, callbacks=callback_config is not None)
    # Validate/create only the owned directory. Staging never initializes keys,
    # changes site identity, opens SQLite, migrates schemas or takes worker locks.
    _state_directory(state, uid, gid)
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
    shutil.copytree(resources / "lib", program / "lib", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (program / "bin").mkdir(mode=0o700)
    shutil.copy2(resources / "bin/thermite", program / "bin/thermite")
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
    # Runtime loading deliberately requires current-user custody. Validate this
    # release's inputs against an empty operator-owned temporary root, so staging
    # an upgrade never reads or changes the service-owned persistent state.
    validation_state = private_directory(target / ".validation-state")
    validation_config = target / ".validation-config.json"
    try:
        json_private(validation_config, {**config, "state": str(validation_state)})
        load_config(validation_config)
    finally:
        validation_config.unlink(missing_ok=True)
        validation_state.rmdir()
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
        "Complete and verify the explicit thermite:thermite ownership handoff before bootstrap or activation. Bootstrap as thermite only for a genuinely new site; reuse persistent state for upgrades and never bootstrap an existing site again.",
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
               "service_user": ACCOUNT, "service_group": GROUP}
    command = ["sudo", "-u", ACCOUNT, "-g", GROUP, str(target / "env/bin/python"),
               str(target / "program/bin/thermite"), "access"]
    receipt["verification_command"] = [*command, "verify", "--config", str(target / "config.json")]
    receipt["bootstrap_command"] = [*command, "bootstrap", "--config", str(target / "config.json"), "--user", "<admin-user>"]
    receipt["activation_commands"] = [["sudo", "install", "-o", "root", "-g", "root", "-m", "0644", str(target / "units" / name), "/etc/systemd/system/" + name] for name in generated_units] + [
        ["sudo", "systemctl", "daemon-reload"], ["sudo", "systemctl", "enable", "--now", *generated_units]]
    json_private(target / "stage.json", {})
    release_inventory = _inventory(target, release=True)
    for path, mode in release_inventory:
        if mode is not None:
            path.chmod(mode)
    state_inventory = _inventory(state) if state.stat().st_uid == os.geteuid() else []
    receipt["ownership_commands"] = [["getent", "passwd", ACCOUNT], ["getent", "group", GROUP], ["id", ACCOUNT],
                                     *_handoff([*release_inventory, *state_inventory])]
    receipt["owner_commands"] = [*receipt["ownership_commands"], receipt["verification_command"], *receipt["activation_commands"]]
    json_private(target / "stage.json", receipt)
    return receipt
