"""Build a private persistent release and owner-installable units; never activate."""

import os
import json
from contextlib import ExitStack, contextmanager
from pathlib import Path
import shutil
import stat
import oh_ports
import oh_units
from oh_service import ACCOUNT, GROUP

from . import TEXTFILE_ENVIRONMENT, AccessError, environment
from .files import (STATED_IDENTITY, atomic_private, json_private, private_directory, read_private,
                    require_service_account, service_identity)

ACCESS_UNITS = oh_units.ACCESS_UNITS
#: Where the host keeps its own units, a quadlet, and the drop-in of the
#: service systemd's generator makes from a quadlet.
HOST_UNITS = Path("/etc/systemd/system")
QUADLETS = Path("/etc/containers/systemd")
LIFECYCLE = "50-container-lifecycle.conf"
#: How long a service has to stop before podman kills it. The worker joins
#: its threads within worker.STOP_SECONDS of the signal.
STOP_SECONDS = 30
#: What of a release each service is shown, read-only. The gateway, whose
#: container the operator's commands run in too (bootstrap, setup,
#: reset-admin, audit), is shown all of it. The others see the program, its
#: environment and the descriptor each is started with; the worker also what
#: that descriptor names. The relay is shown no secret and no state; the
#: worker never the TLS key.
RELEASE_MOUNTS = {
    oh_units.ACCESS: ("",),
    oh_units.ACCESS_WORKER: ("program", environment.DIRECTORY, "config.json", "contract.json",
                             "secrets/runtime-bearer"),
    oh_units.ACCESS_CALLBACKS: ("program", environment.DIRECTORY, "callbacks.json"),
}


def _systemd_text(value):
    text = str(value)
    if any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise AccessError("Unit paths must not contain control characters")
    return text.replace("%", "%%")


def _quadlet(unit, description, root, verb, descriptor, *, state, image, identity, textfile):
    """One service's quadlet. The container is the pinned Python image with
    a read-only root, no capability and the host's network; the process is
    the service account by number, which the unit also states for the
    service's own guard (files.require_service_account). It sees the
    release at the release's own path, as the stage wrote its descriptors,
    and of the host nothing but its mounts.

    Podman 4.9 is a supported host: these are only keys and podman flags
    the units in deploy/ already use there, and podman run's --read-only
    and --workdir."""
    uid, gid = identity
    record = oh_units.BOUNDS[unit]
    if record.user != oh_units.STATE_USER or record.network != oh_units.HOST_NETWORK:
        raise AccessError(f"{unit}: no quadlet is written for user {record.user!r} on network {record.network!r}")
    release = Path(environment.plain(root))
    mounts = [f"Volume={release / part}:{release / part}:ro,z" for part in RELEASE_MOUNTS[unit]]
    if oh_units.ACCESS_STATE in record.secrets:
        mounts.append(f"Volume={environment.plain(state)}:{state}:z")
    metrics = []
    if textfile is not None:
        # The runtime's own directory, labelled as the runtime has it.
        textfile = environment.plain(textfile)
        metrics = [f"Volume={textfile}:{textfile}", f"Environment={TEXTFILE_ENVIRONMENT}={textfile}"]
    python = release / environment.DIRECTORY / "bin/python"
    return "\n".join([
        "[Unit]",
        f"Description=thermite {description}",
        "After=network-online.target",
        "Wants=network-online.target",
        f"PartOf={oh_units.TARGET}",
        "",
        "[Container]",
        f"ContainerName={oh_units.ACCESS_CONTAINERS[unit]}",
        f"Image={image}",
        f"Network={record.network}",
        f"User={uid}",
        f"Group={gid}",
        "NoNewPrivileges=true",
        "DropCapability=all",
        # As every Thermite container: runc, since crun stacks AppArmor
        # profiles under no-new-privileges; and a container id file of the
        # unit's own, which its lifecycle drop-in removes the container by.
        "PodmanArgs=--runtime=runc",
        "PodmanArgs=--replace=false",
        "PodmanArgs=--cidfile=%t/%N.cid",
        "PodmanArgs=--umask=0077",
        "PodmanArgs=--read-only",
        f"PodmanArgs=--stop-timeout={STOP_SECONDS}",
        # The release it runs, which the gateway step reads back from the
        # running process (oh_access.deployment.live).
        f"PodmanArgs=--workdir={release}",
        *mounts,
        *metrics,
        f"Environment={STATED_IDENTITY}={uid}:{gid}",
        # The program is mounted read-only: nothing could be written beside it.
        "Environment=PYTHONDONTWRITEBYTECODE=1",
        f"Exec={python} {release / 'program/bin/thermite'} access {verb} --descriptor {release / descriptor}",
        "",
        "[Service]",
        f"Slice={oh_units.SLICE}",
        f"MemoryHigh={record.memory_high}",
        f"MemoryMax={record.memory_max}",
        "UMask=0077",
        f"SyslogIdentifier={unit.removesuffix('.service')}",
        "Restart=on-failure",
        "RestartSec=5",
        # Past podman's own bound, so it is podman that ends a stop.
        f"TimeoutStopSec={STOP_SECONDS + 15}",
        "",
        "[Install]",
        f"WantedBy={oh_units.TARGET}",
        "",
    ])


def destinations(name):
    """Where the host installs a service's unit: its quadlet, then the
    lifecycle drop-in of the service systemd's generator makes from it, as
    every Thermite container has."""
    return [QUADLETS / (oh_units.ACCESS_CONTAINERS[name] + ".container"), HOST_UNITS / (name + ".d") / LIFECYCLE]


def unit_files(generated):
    """Every file the host installs for units(): [(service, copy,
    destination, text)], where `copy` names the file's protected copy in a
    stage plan."""
    from oh_deploy import render_container_dropin
    files = []
    for name, text in generated.items():
        for destination in destinations(name):
            dropin = destination.name == LIFECYCLE
            files.append((name, name + ".d.conf" if dropin else destination.name, destination,
                          render_container_dropin() if dropin else text))
    return files


def units(destination, user=ACCOUNT, *, state, image, identity, callbacks=False, textfile=None, runtime=None):
    """Each service's quadlet by its service name: a container of `image`,
    the pinned Python image, on the release's environment. unit_files()
    says where each is installed.

    identity: the service account's (uid, gid). A container has no account
    database, so its quadlet states the numbers.
    textfile: the runtime's node_exporter textfile directory, where each
    service writes its metrics (oh_access.metrics); None writes none.
    runtime: the runtime state, of which no access service needs anything
    else: a container sees only what its quadlet mounts."""
    root = Path(destination).absolute()
    state = _absolute(state)
    if runtime is not None:
        runtime = _absolute(runtime)
        if _overlap(runtime, root) or _overlap(runtime, state):
            raise AccessError("The access release and state must lie outside the runtime state")
    if user != ACCOUNT:
        raise AccessError("Access services require user thermite and group thermite")
    result = {}
    for name in (("serve", "worker", "callbacks") if callbacks else ("serve", "worker")):
        unit = "thermite-access" + ("" if name == "serve" else "-" + name) + ".service"
        description = {"worker": "room policy worker", "serve": "authenticated site",
                       "callbacks": "verified UPnP callback ingress"}[name]
        result[unit] = _quadlet(unit, description, root, name, "callbacks.json" if name == "callbacks" else "config.json",
                                state=state, image=image, identity=identity, textfile=textfile)
    return result


def wheels(area, image, application):
    """(directory, requirements): the operator's wheelhouse, below the site
    `area`, for the pins a release staged from `application` carries, and
    those pins. The install fills and checks it before a transaction
    begins; a stage only reads it."""
    requirements = (_absolute(application) / environment.REQUIREMENTS).read_bytes()
    return environment.wheelhouse(_absolute(area), image, requirements), requirements


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
    _systemd_text(plan)  # Also reject line/control characters in the receipt's commands.
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
            except BaseException:
                if not _same_release(parent, target.name, descriptor):
                    raise AccessError(purpose + " path changed during staging; refusing to remove its replacement") from None
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


def _export_plan(descriptor, receipt, custody, unit_sources=None):
    files = {"stage.json": json.dumps(receipt, indent=2, allow_nan=False) + "\n",
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
    # Installing units is privileged too: never install root units from the
    # release after its service account has acquired permission to replace
    # their bytes.
    os.mkdir("units", mode=0o700, dir_fd=descriptor)
    units_descriptor = os.open("units", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
    try:
        os.fchmod(units_descriptor, 0o700)
        for name, content in (unit_sources or {}).items():
            write(units_descriptor, name, content)
    finally:
        os.close(units_descriptor)


def stage(destination, *, state, house, assets, runtime_url, runtime_token_file, tls_cert, tls_key,
          allowed_hosts, listen, port, timezone, image, area, application, user=None,
          install_environment=True, callbacks=None, plan_directory=None, host_ports=None, guest_http=None,
          textfile=None, music=None, runtime=None, podman=None):
    """A new release with explicit reusable site state. No database is migrated.

    image is the pinned Python image the services run in. Its own Python
    builds their environment here, from the site's wheelhouse for it alone
    and without a network (wheels(), oh_access.environment); `podman` runs
    that one command with root's podman, which holds the image, and raises
    when it fails, with the partial release removed.

    area is the site the release is for, or a checkout, and application
    the directory its program is copied from. The caller names both
    (oh_install.access.source): nothing here reads the selected site.
    listen is the HTTPS listener's addresses (oh_ports.gateway_listen).
    guest_http is host.yaml's optional Guest listener mapping (oh_access.guest).
    music is host.yaml's checked music (oh_music.settings): the gateway then
    serves its libraries through Music Assistant on host_ports' music port.

    Nothing is handed over, installed or started here. The caller is the
    install transaction's gateway step (oh_install.access.Gateway): it runs the
    receipt's ownership and verification commands, migrates, installs the
    plan's protected unit copies and starts them. plan_directory is its private
    directory for the receipt, the custody manifest and helper, and those unit
    copies. The program and assets are copied, so no service points into a
    disposable checkout.
    """
    from . import guest
    from .service import CONFIG_VERSION, compile_contract, load_config, validate_listeners
    if plan_directory is None:
        raise AccessError("An operator-owned plan directory is required for a safe, replayable custody handoff")
    if install_environment and podman is None:
        raise AccessError("The services' environment needs a way to run podman")
    area = _absolute(area)
    resources = _absolute(application)
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
    generated_units = units(target, user, state=state, image=image, identity=(uid, gid),
                            callbacks=callback_config is not None, textfile=textfile, runtime=runtime)
    files = unit_files(generated_units)
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
        compile_contract(house, private_assets, target / "contract.json",
                         metrics_port=(host_ports or {}).get("prometheus"),
                         music_host=None if music is None else
                         {"port": (host_ports or oh_ports.DEFAULTS)["music"], "libraries": sorted(music["libraries"])})
        config = {"version": CONFIG_VERSION, "state": str(state), "contract_file": str(target / "contract.json"),
                  "runtime_url": runtime_url, "runtime_token_file": str(target / "secrets/runtime-bearer"),
                  "allowed_hosts": allowed_hosts, "listen": list(listen), "port": port, "timezone": timezone,
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
            # The image's Python writes it as this operator, into a directory
            # that is here before the container is.
            private_directory(target / environment.DIRECTORY)
            podman(environment.build_command(image, target, wheels(area, image, resources)[0]))
        for _, copy, _, source in files:
            atomic_private(target / "units" / copy, source.encode())
        receipt = {"version": 1, "staged": True, "activated": False, "security_boundary_active": False,
                   "destination": str(target), "persistent_state": str(state), "config": str(target / "config.json"), "units": list(generated_units),
                   # Each unit's installed files as [protected copy, destination],
                   # and its container.
                   "unit_files": {name: [[str(plan / "units" / copy), str(destination)]
                                         for unit, copy, destination, _ in files if unit == name]
                                  for name in generated_units},
                   "containers": {name: oh_units.ACCESS_CONTAINERS[name] for name in generated_units},
                   "image": image,
                   "environment_installed": install_environment,
                   "callbacks_staged": callback_config is not None, "callbacks_activated": False,
                   "service_user": ACCOUNT, "service_group": GROUP}
        # As the services run: one-shot containers of the same image, as
        # the account, which root's podman starts.
        for name, verb in (("verification_command", "verify"), ("migration_command", "migrate")):
            receipt[name] = ["sudo", *environment.service_command(image, target, (uid, gid), verb, state=state)]
        receipt["unit_install_commands"] = [
            command for _, copy, destination, _ in files for command in (
                # A drop-in's directory is the unit's own, and new with it.
                *([["sudo", "install", "-d", "-o", "root", "-g", "root", "-m", "0755", str(destination.parent)]]
                  if destination.parent not in (HOST_UNITS, QUADLETS) else []),
                ["sudo", "install", "-o", "root", "-g", "root", "-m", "0644", str(plan / "units" / copy),
                 str(destination)])]
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
        json_private(target / "stage.json", receipt)
        from .custody import manifest
        _export_plan(plan_descriptor, receipt, manifest([release_inventory, state_inventory], uid, gid),
                     {copy: source for _, copy, _, source in files})
        # Check while both contexts are open, so either replacement fails
        # the entire attempt without deleting the replacing directory.
        for path, descriptor, purpose in ((plan, plan_descriptor, "Plan"), (target, release_descriptor, "Release")):
            with _directory_descriptor(path.parent) as parent:
                if not _same_release(parent, path.name, descriptor):
                    raise AccessError(purpose + " path changed during staging; refusing to remove its replacement")
        return receipt
