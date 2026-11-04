"""Build a private persistent release and owner-installable units; never activate."""

import os
import json
from contextlib import ExitStack, contextmanager
from pathlib import Path
import shlex
import shutil
import stat
import subprocess
import venv
import oh_units
from oh_service import ACCOUNT, GROUP

from . import AccessError
from .files import (atomic_private, json_private, private_directory, read_private,
                    require_service_account, service_identity)

ACCESS_UNITS = ("thermite-access.service", "thermite-access-worker.service", "thermite-access-callbacks.service")


class _EnvironmentInstallError(AccessError):
    """Keep the operator's diagnostic directory when environment setup fails."""


def _systemd_text(value):
    text = str(value)
    if any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise AccessError("Unit paths must not contain control characters")
    return text.replace("%", "%%")


def systemd(value):
    """One quoted list/command argument, not a scalar path directive."""
    return '"' + _systemd_text(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _working_directory(value):
    # This scalar does not unquote or unescape: quotes and backslashes are
    # literal filename characters. Only specifiers need escaping. A terminal
    # '/.' preserves trailing whitespace/backslashes through the INI reader.
    text = _systemd_text(value)
    return text + "/." if text.endswith((" ", "\\")) else text


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
        unit = "thermite-access" + suffix
        result[unit + ".service"] = (
            "[Unit]\nDescription=thermite " + {"worker": "room policy worker", "serve": "authenticated site", "callbacks": "verified UPnP callback ingress"}[name] +
            f"\nAfter=network-online.target\nWants=network-online.target\nPartOf={oh_units.TARGET}\n\n[Service]\nType=simple\nUser=" + user +
            "\nGroup=" + GROUP + f"\nSlice={oh_units.SLICE}\nUMask=0077\nWorkingDirectory=" + _working_directory(root) +
            # env directly execs the staged interpreter; systemd restricts the
            # executable token itself from containing quotes/backslashes. ':'
            # disables its environment substitution so every '$' stays literal.
            "\nExecStart=:/usr/bin/env -- " + " ".join(systemd(x) for x in [*command, name, "--config", root / config_name]) +
            # Without it the journal would name every one of them env.
            "\nSyslogIdentifier=" + unit +
            "\nRestart=on-failure\nRestartSec=5\nTimeoutStopSec=30\nNoNewPrivileges=yes\nPrivateTmp=yes\nProtectSystem=strict\nProtectHome=read-only" + writable +
            f"\nRestrictAddressFamilies=AF_UNIX AF_INET AF_INET6\n\n[Install]\nWantedBy={oh_units.TARGET}\n")
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
                raise AccessError("Service parents must permit thermite traversal; use a prepared persistent location such as /srv/thermite/access")


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


def _same_release(parent, name, descriptor):
    try:
        current = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return stat.S_ISDIR(current.st_mode) and os.path.samestat(current, os.fstat(descriptor))


@contextmanager
def _directory_descriptor(path):
    flags = os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open(path.anchor, flags)
    try:
        for part in path.parts[1:]:
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


def _private_plan_parent(descriptor):
    info = os.fstat(descriptor)
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise AccessError("Plan parent must already be owned by this operator and mode 0700")


def _trusted_ancestors(path, purpose):
    # A private leaf is insufficient below a service-writable ancestor:
    # thermite could rename it and substitute an interpreter or root helper.
    for ancestor in (path, *path.parents):
        with _directory_descriptor(ancestor) as descriptor:
            info = os.fstat(descriptor)
            writable = stat.S_IMODE(info.st_mode) & 0o022
            sticky_root = info.st_uid == 0 and info.st_mode & stat.S_ISVTX
            if info.st_uid not in {0, os.geteuid()} or writable and not sticky_root:
                raise AccessError(purpose + " ancestors must be owned by root or the operator and protected from replacement")


def _plan_location(plan, target, state):
    _systemd_text(plan)  # Also reject line/control characters in script metadata.
    if any(_overlap(plan, root) for root in (target, state)):
        raise AccessError("Plan directory must be separate from releases and persistent state")
    _no_symlinks(plan)
    if plan.exists():
        raise AccessError("Stage into a new private plan directory")
    try:
        with _directory_descriptor(plan.parent) as descriptor:
            _private_plan_parent(descriptor)
        _trusted_ancestors(plan.parent, "Plan")
    except OSError:
        raise AccessError("Plan parent must already exist without symlinks and be private to this operator") from None
    for ancestor in plan.parents:
        # An operator may already keep a saved stage.json under var/HOST_ID;
        # the program/config pair distinguishes an actual service release.
        if (ancestor / "program").is_dir() and (ancestor / "config.json").exists():
            raise AccessError("Plan directory must not be inside an existing release")


@contextmanager
def _new_release(target, *, purpose="Release", private_parent=False):
    """Build at the final path, removing only this attempt's directory on failure."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    with _directory_descriptor(target.parent) as parent:
        if private_parent:
            _private_plan_parent(parent)
        try:
            os.mkdir(target.name, mode=0o700, dir_fd=parent)
        except FileExistsError:
            message = "Stage into a new private plan directory" if private_parent else "Stage into a new persistent release directory"
            raise AccessError(message) from None
        descriptor = os.open(target.name, flags, dir_fd=parent)
        try:
            try:
                # A releases parent prepared by hand may be SGID. Clear any
                # inherited bit on this exclusively created directory, through its fd.
                os.fchmod(descriptor, 0o700)
                yield descriptor
            except BaseException as error:
                if not _same_release(parent, target.name, descriptor):
                    raise AccessError(purpose + " path changed during staging; refusing to remove its replacement") from None
                if private_parent and isinstance(error, _EnvironmentInstallError):
                    # At this point the plan holds only a private diagnostic
                    # log, never a runnable partial handoff or activation plan.
                    raise
                try:
                    # Anchor deletion to the directory created above. rmtree's
                    # fd-based walk never follows links into other releases or
                    # persistent state, even if the release name is replaced.
                    cleanup = os.open(".", flags, dir_fd=descriptor)
                    try:
                        for name in os.listdir(cleanup):
                            info = os.stat(name, dir_fd=cleanup, follow_symlinks=False)
                            if stat.S_ISDIR(info.st_mode):
                                shutil.rmtree(name, dir_fd=cleanup)
                            else:
                                os.unlink(name, dir_fd=cleanup)
                    finally:
                        os.close(cleanup)
                    if not _same_release(parent, target.name, descriptor):
                        raise AccessError(purpose + " path changed during cleanup; refusing to remove its replacement")
                    os.rmdir(target.name, dir_fd=parent)
                except OSError:
                    raise AccessError("Staging failed and its partial " + purpose.lower() + " could not be removed safely") from None
                raise
            if not _same_release(parent, target.name, descriptor):
                raise AccessError(purpose + " path changed during staging")
        finally:
            os.close(descriptor)


def _handoff_script(receipt):
    """Render this stage's in-memory argv plan, never commands from a JSON input."""
    return ("#!/bin/sh\nset -eu\n"
            "# Run as the operator after reviewing this exact plan, never sudo sh.\n"
            "# This script does not bootstrap accounts or activate services.\n"
            "trap 'handoff_status=$?; if [ \"$handoff_status\" -ne 0 ]; then "
            "printf \"%s\\n\" \"Handoff stopped. Fix the failed command and run this same handoff.sh again. Bootstrap and activation were not run.\" >&2; fi; exit \"$handoff_status\"' 0\n"
            "trap 'exit 130' INT\ntrap 'exit 143' TERM\n"
            "\n# 1. Account checks, then validated descriptor-anchored custody.\n"
            "printf '%s\\n' 'Applying the reviewed thermite:thermite ownership plan.'\n" +
            "\n".join(shlex.join(command) for command in receipt["ownership_commands"]) +
            "\n\n# 2. Read-only verification as the thermite service account.\n"
            "printf '%s\\n' 'Verifying private service custody.'\n" +
            shlex.join(receipt["verification_command"]) +
            "\nprintf '%s\\n' 'Custody verified. Bootstrap only a new site; complete the activation checklist before installing or starting units.'\n")


def _activation_script(receipt):
    """A reviewed cutover, using only commands generated for this release."""
    target = receipt["destination"]
    installed = receipt["units"]
    retired = [name for name in ACCESS_UNITS if name not in installed]
    lines = ["#!/bin/sh", "set -eu", "umask 077",
             "# Run as the operator only after reviewing this script and completing",
             "# the receipt's TLS, native-listener and callback requirements.",
             "# This script never bootstraps or migrates site authority.",
             "trap 'activation_status=$?; if [ \"$activation_status\" -ne 0 ]; then printf \"%s\\n\" \"Activation stopped; services may be stopped. Fix the failed command and rerun this script. See the guide before rolling back with an older plan.\" >&2; fi; exit \"$activation_status\"' 0",
             "trap 'exit 130' INT", "trap 'exit 143' TERM",
             "expected_release=" + shlex.quote(target),
             "running_release() {",
             '  unit=$1',
             '  systemctl is-active --quiet "$unit" || return 1',
             '  pid=$(systemctl show --property=MainPID --value "$unit") || return 1',
             '  case "$pid" in ""|0|*[!0-9]*) return 1 ;; esac',
             '  current=$(sudo readlink -- "/proc/$pid/cwd") || return 1',
             '  [ "$current" = "$expected_release" ] || return 1',
             '  [ "$(systemctl show --property=MainPID --value "$unit")" = "$pid" ]',
             "}",
             "# Verify the new release before stopping any existing service.",
             shlex.join(receipt["verification_command"]),
             "same_release=true"]
    for command in receipt["unit_install_commands"]:
        source, destination = command[-2:]
        lines.append("cmp -s " + shlex.join([source, destination]) + " || same_release=false")
    for name in installed:
        lines.extend(["running_release " + shlex.quote(name) + " || same_release=false",
                      "systemctl is-enabled --quiet " + shlex.quote(name) + " || same_release=false"])
    for name in retired:
        lines.append("if systemctl is-active --quiet " + shlex.quote(name) + " || systemctl is-enabled --quiet " + shlex.quote(name) + "; then same_release=false; fi")
    lines.extend(['if [ "$same_release" = true ]; then',
                  "  printf '%s\\n' 'This release is already active; no services restarted.'", "  exit 0", "fi",
                  "# Stop installed access services before replacing their units."])
    for name in ACCESS_UNITS:
        lines.append("if systemctl cat " + shlex.quote(name) + " >/dev/null 2>&1; then " +
                     shlex.join(["sudo", "systemctl", "stop", name]) + "; fi")
    for name in retired:
        lines.append("if systemctl cat " + shlex.quote(name) + " >/dev/null 2>&1; then " +
                     shlex.join(["sudo", "systemctl", "disable", name]) + "; fi")
    lines.extend(shlex.join(command) for command in receipt["unit_install_commands"])
    lines.extend(["sudo systemctl daemon-reload",
                  shlex.join(["sudo", "systemctl", "enable", "--now", *installed])])
    for name in installed:
        lines.extend(["for attempt in 1 2 3 4 5 6 7 8 9 10; do",
                      "  if running_release " + shlex.quote(name) + "; then break; fi",
                      '  if [ "$attempt" = 10 ]; then printf \'%s\\n\' ' +
                      shlex.quote("Running release verification failed: " + name) + " >&2; exit 1; fi",
                      "  sleep 1", "done"])
    lines.append("printf '%s\\n' 'Running release verified. Check HTTPS login, room controls and any callbacks.'")
    return "\n".join(lines) + "\n"


def _install_environment(target, program, plan, descriptor, wheelhouse):
    log = plan / "environment-install.log"
    fd = os.open(log.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=descriptor)
    with os.fdopen(fd, "wb") as output:
        os.fchmod(output.fileno(), 0o600)
        try:
            output.write(b"Creating the private Python environment.\n")
            output.flush()
            venv.EnvBuilder(with_pip=True).create(target / "env")
            argv = [str(target / "env/bin/python"), "-m", "pip", "install", "--require-hashes",
                    "-r", str(program / "lib/requirements-site.txt")]
            if wheelhouse:
                argv.extend(["--no-index", "--find-links", str(Path(wheelhouse).absolute())])
            completed = subprocess.run(argv, stdout=output, stderr=subprocess.STDOUT, check=False)
            if completed.returncode:
                raise RuntimeError("Pinned dependency installation exited " + str(completed.returncode))
        except Exception as error:
            output.write(("\n" + type(error).__name__ + ": " + str(error) + "\n").encode("utf-8", "replace"))
            for captured in (getattr(error, "output", None), getattr(error, "stderr", None)):
                if captured:
                    output.write(captured if isinstance(captured, bytes) else captured.encode("utf-8", "replace"))
            output.flush()
            os.fsync(output.fileno())
            raise _EnvironmentInstallError("Service environment installation failed; review the private diagnostic log at " +
                                           str(log) + ". The partial release was removed; use a new plan directory when retrying.") from None


def _export_plan(descriptor, receipt, custody, unit_sources=None):
    files = {"stage.json": json.dumps(receipt, indent=2, allow_nan=False) + "\n",
             "handoff.sh": _handoff_script(receipt),
             "activate.sh": _activation_script(receipt),
             "custody.json": json.dumps(custody, separators=(",", ":"), allow_nan=False) + "\n",
             "custody.py": Path(__file__).with_name("custody.py").read_text()}
    def write(parent, name, content):
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb", closefd=False) as stream:
                stream.write(content.encode())
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            os.close(fd)
    for name, content in files.items():
        write(descriptor, name, content)
    # Activation is privileged too: never install root units from the release
    # after its service account has acquired permission to replace their bytes.
    os.mkdir("units", mode=0o700, dir_fd=descriptor)
    units_descriptor = os.open("units", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
    try:
        os.fchmod(units_descriptor, 0o700)
        for name, content in (unit_sources or {}).items():
            write(units_descriptor, name, content)
    finally:
        os.close(units_descriptor)


def stage(destination, *, state, house, assets, runtime_url, runtime_token_file, tls_cert, tls_key,
          allowed_hosts, bind_host, port, timezone, user=None, wheelhouse=None, area=None,
          install_environment=True, callbacks=None, plan_directory=None, host_ports=None, guest_http=None):
    """A new release with explicit reusable site state. No database is migrated.

    guest_http is host.yaml's optional Guest listener mapping (oh_access.guest).

    Owner installs the units only AFTER resolving the activation checklist. The
    program and assets are copied, so no service points into a disposable checkout.
    """
    from . import guest
    from .service import CONFIG_VERSION, compile_contract, load_config, validate_listeners
    from oh_paths import PROGRAM, SITE, SITE_FILE
    if plan_directory is None:
        raise AccessError("An operator-owned --plan-directory is required for a safe, replayable custody handoff")
    area = _absolute(area or SITE)
    resources = PROGRAM if (area / SITE_FILE).is_file() else area
    target = _absolute(destination)
    state = _absolute(state)
    plan = _absolute(plan_directory)
    _plan_location(plan, target, state)
    if target.exists() or target.is_symlink():
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
    if os.geteuid() == uid:
        raise AccessError("Stage as the operator, not the thermite service account")
    for parent in {target.parent, state.parent}:
        _trusted_ancestors(parent, "Release and state")
    _service_parents(target, state, uid, gid)
    callback_config = None
    if callbacks is not None:
        from .callback_service import load_config as load_callbacks
        callback_config = load_callbacks(callbacks, runtime_url=runtime_url)
    try:
        guest_http = guest.settings(guest_http)
        validate_listeners(runtime_url, port,
                           callback_port=callback_config["port"] if callback_config is not None else None,
                           guest_port=guest_http["port"] if guest_http else None, host_ports=host_ports)
    except (ValueError, TypeError) as exc:
        raise AccessError(str(exc)) from None
    generated_units = units(target, user, state=state, callbacks=callback_config is not None)
    # Validate/create only the owned directory. Staging never initializes keys,
    # changes site identity, opens SQLite, migrates schemas or takes worker locks.
    _state_directory(state, uid, gid)
    with ExitStack() as attempt:
        release_descriptor = attempt.enter_context(_new_release(target))
        plan_descriptor = attempt.enter_context(_new_release(plan, purpose="Plan", private_parent=True))
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
        config = {"version": CONFIG_VERSION, "state": str(state), "contract_file": str(target / "contract.json"),
                  "runtime_url": runtime_url, "runtime_token_file": str(target / "secrets/runtime-bearer"),
                  "allowed_hosts": allowed_hosts, "bind_host": bind_host, "port": port, "timezone": timezone,
                  "guest_http": guest_http,
                  "tls_cert": str(target / "secrets/tls-cert.pem"), "tls_key": str(target / "secrets/tls-key.pem")}
        json_private(target / "config.json", config)
        # Runtime loading deliberately requires current-user custody. Validate this
        # release's inputs against an empty operator-owned temporary root, so staging
        # an upgrade never reads or changes the service-owned persistent state.
        validation_state = private_directory(target / ".validation-state")
        validation_config = target / ".validation-config.json"
        json_private(validation_config, {**config, "state": str(validation_state)})
        load_config(validation_config)
        os.unlink(validation_config.name, dir_fd=release_descriptor)
        os.rmdir(validation_state.name, dir_fd=release_descriptor)
        if install_environment:
            _install_environment(target, program, plan, plan_descriptor, wheelhouse)
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
        receipt["unit_install_commands"] = [["sudo", "install", "-o", "root", "-g", "root", "-m", "0644", str(plan / "units" / name), "/etc/systemd/system/" + name] for name in generated_units]
        receipt["activation_commands"] = [["sh", str(plan / "activate.sh")]]
        json_private(target / "stage.json", {})
        release_inventory = _inventory(target, release=True)
        for path, mode in release_inventory:
            if mode is not None:
                path.chmod(mode)
        state_inventory = _inventory(state) if state.stat().st_uid == os.geteuid() else []
        receipt["custody_paths"] = len(release_inventory) + len(state_inventory)
        receipt["custody_manifest"] = str(plan / "custody.json")
        receipt["ownership_commands"] = [["getent", "passwd", ACCOUNT], ["getent", "group", GROUP], ["id", ACCOUNT],
                                         ["sudo", "/usr/bin/python3", "-I", "-S", str(plan / "custody.py"), str(plan / "custody.json")]]
        receipt["owner_commands"] = [*receipt["ownership_commands"], receipt["verification_command"], *receipt["activation_commands"]]
        json_private(target / "stage.json", receipt)
        from .custody import manifest
        _export_plan(plan_descriptor, receipt, manifest([release_inventory, state_inventory], uid, gid), generated_units)
        # Check while both contexts are open, so either replacement fails
        # the entire attempt without deleting the replacing directory.
        for path, descriptor, purpose in ((plan, plan_descriptor, "Plan"), (target, release_descriptor, "Release")):
            with _directory_descriptor(path.parent) as parent:
                if not _same_release(parent, path.name, descriptor):
                    raise AccessError(purpose + " path changed during staging; refusing to remove its replacement")
        return receipt
