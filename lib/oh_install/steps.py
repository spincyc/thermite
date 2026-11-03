"""The install, step by step: what each step checks, and what it runs."""

from __future__ import annotations

import dataclasses
import json
import re
import shlex
import sys
from pathlib import Path

import oh_deploy as deploy
import oh_logging
import oh_service
import oh_ports
import oh_metrics as metrics
from oh_install.model import Action, Check, Command, Step, Task
from oh_install.system import System

#: Podman runs openHAB through runc; Git and make carry the deploys; Python with PyYAML
#: runs this area's tools; Node.js and npm support the terminal remote.
PACKAGES = {
    "pacman": ("podman", "runc", "git", "make", "python", "python-yaml", "nodejs", "npm"),
    "apt-get": ("podman", "runc", "git", "make", "python3", "python3-yaml", "python3-venv", "nodejs", "npm"),
}
#: Quadlets need Podman 4.4.
PODMAN = (4, 4)
#: The Apple TV helper's pins have no wheels for anything else.
MACHINE = "x86_64"
NAME = oh_service.ACCOUNT
LEGACY_NAME = "openhab"
PATH_UNIT = "openhab-atv-restart.path"
METRICS_PATH_UNIT = Path(deploy.METRICS_RESTART_PATH).name
#: The unit a changed file in deploy/ restarts; the restart service runs only
#: when the path unit triggers it.
RESTARTS = {
    deploy.QUADLET: "openhab.service",
    deploy.ATV_QUADLET: "openhab-atv.service",
    deploy.ATV_RESTART_PATH: PATH_UNIT,
}
METRICS_RESTARTS = {
    **{quadlet: unit for unit, quadlet in deploy.METRICS_QUADLETS.items()},
    deploy.METRICS_RESTART_PATH: METRICS_PATH_UNIT,
}
#: How long the metrics services' first start may take (it pulls the
#: images, then Prometheus scrapes every target once).
METRICS_WAIT = 5 * 60
#: How long openHAB's first start may take to answer (it installs the
#: add-ons), and how often to ask.
WAIT = 15 * 60
POLL = 5
#: How many pastes the token prompt takes before the install stops.
PASTES = 3
ACTIVE = ("active", "activating", "reloading", "deactivating")
#: openHAB rebuilds these at its next start; a backup leaves them out.
UNSAVED = ("userdata/cache", "userdata/tmp")
#: Ends the name of a backup that holds Grafana's data, before .tar.gz.
GRAFANA_TAG = "-grafana"


@dataclasses.dataclass(frozen=True)
class Context:
    """What the install installs, and where: this checkout, its pins and
    host facts, HEAD with the digest of its conf/, and this host's name and
    LAN address, found at run time."""

    area: Path
    versions: dict
    host: dict
    head: str
    conf_digest: str
    hostname: str
    address: str | None

    @property
    def var(self) -> Path:
        return self.area / "var"

    @property
    def layout(self) -> deploy.State:
        return deploy.state(self.host)

    @property
    def url(self) -> str:
        """openHAB as this host reaches it."""
        return deploy.production_url(self.host)

    @property
    def lan_url(self) -> str:
        """openHAB as a phone or another computer reaches it."""
        return deploy.production_url(self.host, self.address or self.hostname)

    @property
    def token_file(self) -> Path:
        return deploy.token_file(self.hostname)

    @property
    def grafana_password_file(self) -> Path:
        """The owner's own copy, relative to the area, beside the API token."""
        return self.token_file.parent / metrics.GRAFANA_ADMIN_PASSWORD

    @property
    def grafana_url(self) -> str:
        return deploy.grafana_url(self.host, self.address or self.hostname)

    def ports(self) -> dict[str, list[int]]:
        """The ports each service listens on."""
        oh_ports.effective(self.host)
        return {"openhab.service": [self.host["http_port"], self.host["https_port"], deploy.KARAF_PORT,
                                    deploy.LSP_PORT],
                "openhab-atv.service": [deploy.HELPER_PORT],
                **{service.unit: [int(service.listen.rsplit(":", 1)[1])] for service in metrics.services(self.host)}}


def busy_ports(context: Context, system: System) -> list[int]:
    """The ports a service that is not running needs and something else holds."""
    return [port for service, ports in context.ports().items() if system.unit(service).active != "active"
            for port in ports if not system.port_free(port)]


def application(context: Context, *args: str) -> tuple[str, ...]:
    """Use the selected site's application, including through sg and sudo.

    A private site's launcher is tied to its Python environment. The system
    python and a relative source Makefile cannot substitute for that release.
    Original source checkouts retain their established commands.
    """
    from oh_paths import PROGRAM, SITE_FILE
    if (context.area / SITE_FILE).is_file():
        return (sys.executable, str(PROGRAM / "bin/thermite"), "--site", str(context.area), *args)
    return ("bin/thermite", *args)


def operator_command(context: Context, system: System, argv: tuple[str, ...]) -> Command:
    """Refresh newly granted group access for this owner command only."""
    if context.host["gid"] in system.process_groups():
        return Command(argv)
    # usermod -aG takes effect at the next login. sg grants the group to
    # this command now. Arch has no sg; sudo -u refreshes the owner's
    # supplementary groups without running the application as root.
    if system.which("sg"):
        return Command(("sg", NAME, "-c", shlex.join(argv)))
    return Command(("-u", system.login(), *argv), root=True)


class Preconditions(Step):
    title = "Preconditions"

    def __init__(self, context: Context):
        self.context = context

    def check(self, system: System) -> Check:
        host = self.context.host
        problems = []
        try:
            oh_service.validate_identity(host["uid"], host["gid"])
        except oh_service.ServiceError as exc:
            problems.append(str(exc))
        if system.machine() != MACHINE:
            problems.append(f"this is {system.machine()}; the Apple TV helper's pins need {MACHINE}")
        if not system.overlayfs():
            problems.append(f"the running kernel, {system.kernel()}, has no overlayfs and no modules to load it "
                            "from (it was upgraded since the last boot), and Podman needs it: reboot first")
        for key, by_id, by_name in (("gid", system.group(host["gid"]), system.group(NAME)),
                                    ("uid", system.user(host["uid"]), system.user(NAME))):
            if by_id is not None and by_id.name not in (NAME, LEGACY_NAME):
                problems.append(f"{key} {host[key]} belongs to {by_id.name}, not {NAME}")
            if by_name is not None and getattr(by_name, key) != host[key]:
                problems.append(f"{NAME} has {key} {getattr(by_name, key)}, not {host[key]}")
            if by_id is not None and by_id.name == LEGACY_NAME and by_name is not None:
                problems.append(f"both {LEGACY_NAME} and {NAME} occupy the configured {key}; resolve the accounts first")
            legacy = system.group(LEGACY_NAME) if key == "gid" else system.user(LEGACY_NAME)
            if legacy is not None and by_name is not None and getattr(legacy, key) == host[key] == getattr(by_name, key):
                problems.append(f"both {LEGACY_NAME} and {NAME} use {key} {host[key]}; resolve the duplicate names first")
            if by_name is None and legacy is not None and getattr(legacy, key) != host[key]:
                problems.append(f"{LEGACY_NAME} has {key} {getattr(legacy, key)}, not {host[key]}; refusing to renumber it")
        named = system.user(NAME) or system.user(LEGACY_NAME)
        if named is not None and named.uid == host["uid"] and named.gid != host["gid"]:
            problems.append(f"{named.name} has primary gid {named.gid}, not {host['gid']}")
        busy = busy_ports(self.context, system)
        if busy:
            problems.append(f"port {', '.join(map(str, busy))} in use (ss -ltnp shows by what)")
        if problems:
            return Check(False, "", "; ".join(problems))
        return Check(True, f"{self.context.hostname}, {MACHINE}, ids {host['uid']}:{host['gid']} and ports free or "
                           "openHAB's")


def podman_version(system: System) -> tuple[int, int] | None:
    code, out = system.capture(["podman", "--version"])
    found = re.search(r"(\d+)\.(\d+)", out) if code == 0 else None
    return (int(found[1]), int(found[2])) if found else None


class Packages(Step):
    title = "Packages"

    def manager(self, system: System) -> str | None:
        return next((name for name in PACKAGES if system.which(name)), None)

    def missing(self, system: System, manager: str) -> list[str]:
        names = PACKAGES[manager]
        if manager == "pacman":
            # -T prints the names it cannot satisfy.
            code, out = system.capture(["pacman", "-T", *names])
            return out.split() if code else []
        return [name for name in names
                if system.capture(["dpkg-query", "-W", "-f=${Status}", name])[1].strip() != "install ok installed"]

    def check(self, system: System) -> Check:
        manager = self.manager(system)
        if manager is None:
            return Check(False, "", f"no pacman or apt-get here: install {' '.join(PACKAGES['pacman'])} yourself")
        missing = self.missing(system, manager)
        if missing:
            return Check(False, f"to install: {' '.join(missing)}")
        version = podman_version(system)
        if version is None or version < PODMAN:
            found = "no version" if version is None else ".".join(map(str, version))
            return Check(False, "", f"Podman reports {found}; quadlets need {'.'.join(map(str, PODMAN))} or later")
        return Check(True, f"{' '.join(PACKAGES[manager])}; Podman {'.'.join(map(str, version))}")

    def actions(self, system: System) -> list[Action]:
        manager = self.manager(system)
        missing = self.missing(system, manager)
        if manager == "pacman":
            return [Command(("pacman", "-S", "--needed", *missing), root=True)]
        return [Command(("apt-get", "install", *missing), root=True)]


class Account(Step):
    title = "The thermite user and group, with you in the group"

    def __init__(self, context: Context):
        self.context = context

    def missing(self, system: System) -> list[str]:
        login = system.login()
        group, me = system.group(NAME), system.user(login)
        missing = []
        if group is None:
            missing.append("group")
        if system.user(NAME) is None:
            missing.append("user")
        service = system.user(NAME)
        if service is not None and ((service.home is not None and service.home != str(self.context.layout.root))
                                    or (service.shell is not None and not service.shell.endswith("/nologin"))):
            missing.append("settings")
        if group is None or not (login in group.members or (me is not None and me.gid == group.gid)):
            missing.append("member")
        return missing

    def check(self, system: System) -> Check:
        missing = self.missing(system)
        host, login = self.context.host, system.login()
        words = {"group": f"group {NAME} ({host['gid']})", "user": f"user {NAME} ({host['uid']})",
                 "member": f"{login} in group {NAME}", "settings": "service home and nologin shell"}
        if missing:
            legacy = (("group" in missing and (group := system.group(LEGACY_NAME)) is not None and group.gid == host["gid"])
                      or ("user" in missing and (user := system.user(LEGACY_NAME)) is not None and user.uid == host["uid"]))
            return Check(False, ("rename the legacy account without changing IDs; " if legacy else "")
                         + "to configure: " + ", ".join(words[m] for m in missing))
        return Check(True, f"{NAME} {host['uid']}:{host['gid']}, {login} a member")

    def actions(self, system: System) -> list[Action]:
        host, missing = self.context.host, self.missing(system)
        actions = []
        legacy_group = system.group(LEGACY_NAME) if "group" in missing else None
        legacy_user = system.user(LEGACY_NAME) if "user" in missing else None
        if legacy_group is not None and legacy_group.gid != host["gid"]:
            legacy_group = None
        if legacy_user is not None and legacy_user.uid != host["uid"]:
            legacy_user = None
        if legacy_group is not None or legacy_user is not None:
            # A login cannot be renamed while its service processes are alive.
            # Stop only this stack's managed units; preserve data and numeric IDs.
            paths = [name for name in Stop.PATHS if system.unit(name).active in ACTIVE]
            if paths:
                actions.append(Command(("systemctl", "stop", *paths), root=True))
            running = [name for name in (*Stop.SERVICES, "thermite-access.service", "thermite-access-worker.service",
                                         "thermite-access-callbacks.service") if system.unit(name).active in ACTIVE]
            if running:
                actions.append(Command(("systemctl", "stop", *running), root=True))
        if "group" in missing:
            actions.append(Command(("groupmod", "-n", NAME, LEGACY_NAME) if legacy_group else
                                   ("groupadd", "-g", str(host["gid"]), NAME), root=True))
        if "user" in missing:
            shell = system.which("nologin") or "/usr/bin/nologin"
            argv = (("usermod", "-l", NAME, "-d", str(self.context.layout.root), "-s", shell, LEGACY_NAME) if legacy_user else
                    ("useradd", "-r", "-u", str(host["uid"]), "-g", str(host["gid"]),
                     "-d", str(self.context.layout.root), "-s", shell, NAME))
            actions.append(Command(argv, root=True))
        elif "settings" in missing:
            shell = system.which("nologin") or "/usr/bin/nologin"
            actions.append(Command(("usermod", "-d", str(self.context.layout.root), "-s", shell, NAME), root=True))
        if "member" in missing:
            actions.append(Command(("usermod", "-aG", NAME, system.login()), root=True))
        return actions


class StateDirectories(Step):
    title = "Service state directories"

    def __init__(self, context: Context):
        self.context = context
        self.applied = False

    def directories(self) -> list[tuple[int, list[Path]]]:
        return self.context.layout.directories()

    def wrong(self, system: System) -> list[tuple[int, Path]]:
        """(mode, directory) for each one missing, or with another owner or mode."""
        host = self.context.host
        wrong = []
        for mode, paths in self.directories():
            for path in paths:
                found = system.stat(path)
                if not (found and found.is_dir and (found.uid, found.gid, found.mode) == (host["uid"], host["gid"], mode)):
                    wrong.append((mode, path))
        return wrong

    def check(self, system: System) -> Check:
        root = self.context.layout.root
        if system.stat(root) is None:
            return Check(False, f"{root} does not exist")
        wrong = self.wrong(system)
        if self.applied and self.context.host["gid"] not in system.process_groups():
            return Check(True, f"service directories installed for {NAME}; log in again for group access")
        if wrong:
            return Check(False, "to create or fix: " + ", ".join(str(p.relative_to(root)) for _, p in wrong))
        count = sum(len(paths) for _, paths in self.directories())
        return Check(True, f"{root} and its {count} directories, owned by {NAME}")

    def actions(self, system: System) -> list[Action]:
        root = self.context.layout.root
        actions = []
        if system.stat(root) is None:
            if system.stat(root.parent) is None:
                actions.append(Command(("mkdir", "-p", str(root.parent)), root=True))
            if system.fstype(root.parent) == "btrfs":
                actions.append(Command(("btrfs", "subvolume", "create", str(root)), root=True))
            else:
                actions.append(Command(("mkdir", str(root)), root=True))
        return actions + self.fix(system)

    def fix(self, system: System) -> list[Action]:
        self.applied = True
        host = self.context.host
        by_mode: dict[int, list[Path]] = {}
        for mode, path in self.wrong(system):
            by_mode.setdefault(mode, []).append(path)
        return [Command(("install", "-d", "-o", str(host["uid"]), "-g", str(host["gid"]),
                         "-m", f"{mode:04o}", *map(str, paths)), root=True) for mode, paths in by_mode.items()]


class MetricsDirectories(StateDirectories):
    title = "The metrics directories: their configuration, stores, secrets and exporter"

    def directories(self) -> list[tuple[int, list[Path]]]:
        return self.context.layout.metrics_directories()

    def check(self, system: System) -> Check:
        root = self.context.layout.root
        wrong = self.wrong(system)
        if self.applied and self.context.host["gid"] not in system.process_groups():
            return Check(True, f"metrics directories installed for {NAME}; log in again for group access")
        if wrong:
            return Check(False, "to create or fix: " + ", ".join(str(p.relative_to(root)) for _, p in wrong))
        return Check(True, f"{', '.join(str(p.relative_to(root)) for _, paths in self.directories() for p in paths)}, "
                           f"owned by {NAME}")

    def actions(self, system: System) -> list[Action]:
        return self.fix(system)


class BootTest(Step):
    title = "Boot test of this commit's conf/"

    def __init__(self, context: Context):
        self.context = context

    def check(self, system: System) -> Check:
        digest = self.context.conf_digest
        if system.stat(deploy.passed_dir(self.context.var) / digest):
            return Check(True, f"conf/ {digest[:12]} passed on this host")
        return Check(False, f"conf/ {digest[:12]} has not passed here; the first run fetches the pinned runtime, "
                            "about 180 MB")

    def actions(self, system: System) -> list[Action]:
        from oh_paths import SITE_FILE
        argv = (application(self.context, "test", "boot") if (self.context.area / SITE_FILE).is_file()
                else ("make", "openhab-test", "TEST=boot"))
        return [Command(argv)]


class NativeListeners(Step):
    title = "Use Thermite's native console and shutdown listeners on loopback"

    def __init__(self, context: Context):
        self.context = context
        self.applied = False

    def check(self, system: System) -> Check:
        checks = ((oh_ports.SHELL_CFG, oh_ports.native_configured_bytes),
                  (oh_ports.CUSTOM_PROPERTIES, oh_ports.shutdown_configured_bytes))
        data = []
        for relative, verify in checks:
            raw = system.read_bytes(self.context.layout.userdata / relative)
            data.append((relative, raw, verify(raw)))
        if all(done for _, _, done in data):
            return Check(True, f"Karaf console/shutdown listen on 127.0.0.1:{oh_ports.KARAF_PORT}/{oh_ports.SHUTDOWN_PORT}")
        if all(done or (raw is None and _opaque(system, self.context, self.context.layout.userdata / relative))
               for relative, raw, done in data) and (self.applied or service_receipt(self.context, system)):
            return Check(True, "native listener configuration verified under private service custody")
        return Check(False, f"set Karaf console/shutdown to 127.0.0.1:{oh_ports.KARAF_PORT}/{oh_ports.SHUTDOWN_PORT} while openHAB is stopped")

    def actions(self, system: System) -> list[Action]:
        self.applied = True
        from oh_paths import PROGRAM
        running = system.unit("openhab.service").active not in ("inactive", "failed", "unknown")
        actions: list[Action] = []
        if running:
            actions.append(Command(("systemctl", "stop", "openhab.service"), root=True))
        actions.append(Command(("python3", str(PROGRAM / "lib/oh_ports.py"),
                                str(self.context.layout.userdata), "--http-port", str(self.context.host["http_port"]),
                                "--uid", str(self.context.host["uid"]), "--gid", str(self.context.host["gid"])), root=True))
        if running:
            actions.append(Command(("systemctl", "start", "openhab.service"), root=True))
        return actions


class PairingLogging(Step):
    title = "Keep pairing credentials out of openHAB's native logs"

    def __init__(self, context: Context):
        self.context = context
        self.applied = False

    @property
    def template(self) -> Path:
        version = self.context.versions["openhab"]["version"]
        return self.context.area / "build" / "sandbox" / f"openhab-{version}" / "userdata"

    def check(self, system: System) -> Check:
        data = system.read_bytes(self.context.layout.userdata / oh_logging.RELATIVE)
        selector = system.read_bytes(self.context.layout.userdata / oh_logging.SELECTOR)
        if data is not None and selector is not None and oh_logging.selected_default(selector) and oh_logging.protected(data):
            return Check(True, "native logger filters drop pairing command payloads")
        if data is None and _opaque(system, self.context, self.context.layout.userdata / oh_logging.RELATIVE) and (self.applied or service_receipt(self.context, system)):
            return Check(True, "pairing logging protection verified under private service custody")
        return Check(False, "install the pairing filter before deploying its Items; an existing service is stopped "
                            "and started so the filter is loaded before pairing is offered")

    def actions(self, system: System) -> list[Action]:
        self.applied = True
        actions: list[Action] = []
        if system.stat(self.template / oh_logging.RELATIVE) is None:
            # A boot receipt can outlive a tidied runtime cache.
            actions.append(Command(application(self.context, "sandbox", "fetch")))
        running = system.unit("openhab.service").active not in ("inactive", "failed", "unknown")
        if running:
            actions.append(Command(("systemctl", "stop", "openhab.service"), root=True))
        from oh_paths import PROGRAM
        actions.append(Command(("python3", str(PROGRAM / "lib" / "oh_logging.py"),
                                str(self.context.layout.userdata), "--http-port", str(self.context.host["http_port"]),
                                "--template", str(self.template), "--uid", str(self.context.host["uid"]),
                                "--gid", str(self.context.host["gid"])), root=True))
        if running:
            actions.append(Command(("systemctl", "start", "openhab.service"), root=True))
        return actions


class Deploy(Step):
    title = "Deploy this commit"

    def __init__(self, context: Context):
        self.context = context
        self.applied = False

    def check(self, system: System) -> Check:
        head, layout = self.context.head, self.context.layout
        if self.applied and _opaque(system, self.context, layout.record):
            return Check(True, f"{head[:12]} deployed by the refreshed operator group")
        try:
            record = json.loads(system.read_bytes(layout.record) or b"{}")
        except ValueError:
            record = {}
        commit = record.get("commit")
        # A deploy made before the metrics directories existed installed no metrics/.
        directories = all((found := system.stat(path)) is not None and found.is_dir
                          for path in (layout.metrics, layout.exporter_app))
        if commit == head and directories and "metrics" not in record:
            return Check(False, f"{head[:12]} is deployed, but without metrics/: the metrics directories are new")
        if commit == head:
            return Check(True, f"{head[:12]} is deployed")
        return Check(False, f"deployed: {commit[:12] if commit else 'nothing yet'}; HEAD is {head[:12]}")

    def actions(self, system: System) -> list[Action]:
        self.applied = True
        from oh_paths import SITE_FILE
        installed = (self.context.area / SITE_FILE).is_file()
        argv = application(self.context, "deploy") if installed else ("make", "openhab-deploy")
        checks = [Command(application(self.context, *args)) for args in
                  (("generate", "--check"), ("validate",), ("lint",))] if installed else []
        return [*checks, operator_command(self.context, system, argv)]


class Units(Step):
    title = "The systemd units from deploy/"

    def __init__(self, context: Context):
        self.context = context

    def stale(self, system: System, names: tuple[str, ...] | None = None) -> list[str]:
        return [name for name, target in sorted(deploy.INSTALLED.items())
                if (names is None or name in names)
                and system.read_bytes(target) != (self.context.area / name).read_bytes()]

    def check(self, system: System) -> Check:
        stale = self.stale(system)
        if stale:
            return Check(False, "to install: " + ", ".join(stale))
        return Check(True, f"the {len(deploy.INSTALLED)} installed units match deploy/")

    def actions(self, system: System) -> list[Action]:
        by_directory: dict[Path, list[str]] = {}
        for name in self.stale(system):
            by_directory.setdefault(deploy.INSTALLED[name].parent, []).append(name)
        return [*(Command(("install", "-m", "0644", "-t", str(directory), *names), root=True)
                  for directory, names in by_directory.items()),
                Command(("systemctl", "daemon-reload"), root=True)]


class Services(Step):
    title = "openHAB and the Apple TV helper running"
    restarts = RESTARTS
    path_unit = PATH_UNIT

    def __init__(self, context: Context):
        self.context = context

    def plan(self, system: System) -> tuple[list[str], list[str], bool]:
        """Units to start, units to restart (their file changed after they
        started), and whether the path unit needs enabling."""
        start, restart = [], []
        for name, unit in self.restarts.items():
            state = system.unit(unit)
            if state.active != "active":
                start.append(unit)
                continue
            installed = system.stat(deploy.INSTALLED[name])
            if installed is not None and state.since is not None and installed.mtime > state.since:
                restart.append(unit)
        return start, restart, system.unit(self.path_unit).enabled != "enabled"

    def check(self, system: System) -> Check:
        start, restart, enable = self.plan(system)
        if not (start or restart or enable):
            return Check(True, "running on the installed units")
        parts = []
        if start:
            parts.append("to start: " + ", ".join(start))
        if restart:
            parts.append("to restart, its unit changed since it started: " + ", ".join(restart))
        if enable:
            parts.append(f"to enable: {self.path_unit}")
        if "openhab.service" in start:
            parts.append("stop any live test instance first (make openhab-sandbox-stop where it runs): "
                         "two live openHABs would fight over the devices")
        return Check(False, "; ".join(parts))

    def actions(self, system: System) -> list[Action]:
        start, restart, enable = self.plan(system)
        services = [unit for unit in start if unit != self.path_unit]
        actions = self.prefetch(system, [*services, *restart])
        if services:
            actions.append(Command(("systemctl", "start", *services), root=True))
        if restart:
            actions.append(Command(("systemctl", "restart", *restart), root=True))
        if enable or self.path_unit in start:
            actions.append(Command(("systemctl", "enable", "--now", self.path_unit), root=True))
        return actions

    def prefetch(self, system: System, units: list[str]) -> list[Action]:
        """Download sequentially before systemd starts any service's timer."""
        names = {"openhab.service": "openhab", "openhab-atv.service": "atv_helper",
                 **{service.unit: service.image for service in metrics.services(self.context.host)}}
        images = list(dict.fromkeys(deploy.image(self.context.versions, names[unit])
                                    for unit in units if unit.endswith(".service")))
        if not images:
            return []
        code, help_text = system.capture(("podman", "pull", "--help"))
        if code == 0 and "--policy" in help_text:
            return [Command(("podman", "pull", "--policy=missing", image), root=True) for image in images]
        # Podman 4.4 has no pull --policy. Keep cache checks in the same
        # approved root context as the pull; the digest is data, never shell.
        return [Command(("sh", "-c", 'podman image exists "$1" || exec podman pull "$1"',
                         "thermite-image-prefetch", image), root=True) for image in images]


class MetricsServices(Services):
    title = "The metrics and local notification services running"
    restarts = METRICS_RESTARTS
    path_unit = METRICS_PATH_UNIT


def _opaque(system: System, context: Context, path: Path) -> bool:
    """Missing and hidden differ: a private service ancestor hides either one."""
    if system.stat(path) is not None:
        return False
    me = system.user(system.login())
    for parent in path.parents:
        found = system.stat(parent)
        if found is not None and found.is_dir and (found.uid, found.gid) == (context.host["uid"], context.host["gid"]):
            bit = 0o100 if me is not None and me.uid == found.uid else 0o010 if found.gid in system.process_groups() else 0o001
            if not found.mode & bit:
                return True
    return False


def service_receipt(context: Context, system: System) -> bool:
    path = context.layout.root / oh_service.RECEIPT
    info = system.stat(path)
    if info is None or info.is_dir or (info.uid, info.gid, info.mode) != (context.host["uid"], context.host["gid"], 0o660):
        return False
    try:
        return json.loads(system.read_bytes(path) or b"null") == {
            "version": oh_service.RECEIPT_VERSION, "policy": oh_service.POLICY,
            "uid": context.host["uid"], "gid": context.host["gid"]}
    except ValueError:
        return False


class NativePreparation(Step):
    title = "Prepare stopped native userdata and service ownership as thermite"

    def __init__(self, context: Context):
        self.context = context
        self.applied = False

    def check(self, system: System) -> Check:
        if self.applied:
            return Check(True, "offline preparation, native guards and complete ownership repair succeeded")
        start, restart, _ = Services(self.context).plan(system)
        needed = (not service_receipt(self.context, system)
                  or StateDirectories(self.context).wrong(system) or MetricsDirectories(self.context).wrong(system)
                  or Account(self.context).missing(system)
                  or "openhab.service" in (*start, *restart)
                  or Units(self.context).stale(system, deploy.OPENHAB_UNITS)
                  or not PairingLogging(self.context).check(system).done
                  or not NativeListeners(self.context).check(system).done)
        return Check(not needed, "service ownership and native preparation are recorded" if not needed else
                     "stop every active native/metrics process and restart path, repair custody, prepare native userdata without root, then verify guards and repair final modes")

    def actions(self, system: System) -> list[Action]:
        from oh_paths import PROGRAM
        self.applied = True
        host, root = self.context.host, self.context.layout.root
        paths = [name for name in Stop.PATHS if system.unit(name).active in ACTIVE]
        running = [name for name in Stop.SERVICES if system.unit(name).active in ACTIVE]
        helper = ("/usr/bin/python3", str(PROGRAM / "lib/oh_service.py"), str(root),
                  "--uid", str(host["uid"]), "--gid", str(host["gid"]))
        userdata = str(self.context.layout.userdata)
        return [*([Command(("systemctl", "stop", *paths), root=True)] if paths else []),
                *([Command(("systemctl", "stop", *running), root=True)] if running else []),
                Command(helper, root=True),
                Command(deploy.prepare_openhab_argv(self.context.versions, host), root=True),
                Command(("python3", str(PROGRAM / "lib/oh_logging.py"), userdata, "--http-port", str(host["http_port"])), root=True),
                Command(("python3", str(PROGRAM / "lib/oh_ports.py"), userdata, "--http-port", str(host["http_port"]),
                         "--uid", str(host["uid"]), "--gid", str(host["gid"])), root=True),
                Command((*helper, "--check-pairing-logging"), root=True),
                Command((*helper, "--receipt"), root=True)]


def _secret_wrong(system: System, context: Context, path: Path) -> str | None:
    """Why a file in the metrics secrets is not right, or None when it is."""
    host, found = context.host, system.stat(path)
    if found is None:
        return "missing"
    if (found.uid, found.gid, found.mode) != (host["uid"], host["gid"], deploy.SECRET_FILE_MODE):
        return f"not {NAME}'s with mode {deploy.SECRET_FILE_MODE:04o}"
    return None


def _unseen(system: System, context: Context, path: Path) -> bool:
    """Whether private service custody makes existence opaque to the operator."""
    return _opaque(system, context, path)


#: What a secret step says of a file it installed but this login cannot see.
UNSEEN = "verified by the administrative helper; private thermite service custody"


def _install_secret(context: Context, source: Path, target: Path, *, preserve=False) -> Command:
    from oh_paths import PROGRAM
    host = context.host
    return Command(("python3", str(PROGRAM / "lib/oh_install/secrets.py"), str(context.layout.root),
                    "--name", target.name, "--source", str(context.area / source),
                    "--uid", str(host["uid"]), "--gid", str(host["gid"]),
                    *(("--preserve-existing", "--generate-if-missing") if preserve else ())), root=True)


class MetricsToken(Step):
    title = "The API token Prometheus and the exporter send to openHAB"

    def __init__(self, context: Context):
        self.context = context
        #: Whether actions() installs it where this login cannot look.
        self.unseen = False

    @property
    def target(self) -> Path:
        return self.context.layout.monitoring_secrets / metrics.OPENHAB_TOKEN

    def saved(self, system: System) -> bytes:
        return (system.read_bytes(self.context.area / self.context.token_file) or b"").strip()

    def problem(self, system: System) -> str | None:
        wrong = _secret_wrong(system, self.context, self.target)
        if wrong:
            return wrong
        installed = system.read_bytes(self.target)
        # One this user cannot read yet (not in the group until a new login) counts as current.
        if installed is not None and installed.strip() != self.saved(system):
            return f"not the token in {self.context.token_file}"
        return None

    def check(self, system: System) -> Check:
        if _unseen(system, self.context, self.target):
            if self.unseen:
                return Check(True, f"{self.target}: {UNSEEN}")
            return Check(False, f"{self.target}: private service custody; verify or install its token with the administrative helper")
        problem = self.problem(system)
        if problem:
            return Check(False, f"{self.target}: {problem}")
        return Check(True, f"{self.target} holds the token in {self.context.token_file}")

    def actions(self, system: System) -> list[Action]:
        self.unseen = True
        actions: list[Action] = []
        if not self.saved(system):
            token = Token(self.context)
            actions.append(Task(f"save the API token you create in Main UI as {self.context.token_file} (mode 0600)",
                                token.save, interactive=True))
        actions.append(_install_secret(self.context, self.context.token_file, self.target))
        return actions


class GrafanaPassword(Step):
    title = "Grafana's admin password"

    def __init__(self, context: Context):
        self.context = context
        #: Whether actions() installs it where this login cannot look.
        self.unseen = False

    @property
    def target(self) -> Path:
        return self.context.layout.monitoring_secrets / metrics.GRAFANA_ADMIN_PASSWORD

    def check(self, system: System) -> Check:
        if _unseen(system, self.context, self.target):
            if self.unseen:
                return Check(True, f"{self.target}: {UNSEEN}")
            return Check(False, f"{self.target}: private service custody; verify the existing password, "
                                "generating one only if missing")
        wrong = _secret_wrong(system, self.context, self.target)
        if wrong == "missing":
            return Check(False, f"to generate once: {self.target}")
        if wrong:
            return Check(False, f"{self.target}: {wrong}")
        return Check(True, f"in {self.target}; the admin login is admin")

    def actions(self, system: System) -> list[Action]:
        self.unseen = True
        return [Task("make the operator credential directory private (0700), preserving existing credential files", self.prepare_copy),
                _install_secret(self.context, self.context.grafana_password_file, self.target, preserve=True)]

    def prepare_copy(self, system: System) -> int:
        try:
            system.private_directory((self.context.area / self.context.grafana_password_file).parent)
        except (OSError, ValueError):
            system.say("   could not prepare the private operator credential directory; check its ownership, permissions and symlinks, then rerun")
            return 1
        return 0


class MetricsAnswer(Step):
    title = "Prometheus ready with every target up, and Grafana answering on the LAN"

    def __init__(self, context: Context):
        self.context = context

    def health(self, system: System) -> deploy.MetricsHealth:
        return system.metrics_health(self.context.host, self.context.address or self.context.hostname)

    def check(self, system: System) -> Check:
        health = self.health(system)
        if health.prometheus_ok and health.targets_ok and health.grafana_ok:
            return Check(True, health.describe())
        down = f"; down: {', '.join(health.down)}" if health.down else ""
        return Check(False, f"{health.describe()}{down}: waiting for service readiness and a successful "
                            "scrape of every target")

    def actions(self, system: System) -> list[Action]:
        return [Task(f"wait up to {METRICS_WAIT // 60} minutes for Prometheus' targets and "
                     f"{self.context.grafana_url}api/health", self.wait)]

    def wait(self, system: System) -> int:
        deadline = system.clock() + METRICS_WAIT
        while system.clock() < deadline:
            if self.check(system).done:
                return 0
            system.sleep(POLL)
        health = self.health(system)
        if health.down:
            system.say(f"   still down: {', '.join(health.down)}")
        system.say("   `sudo journalctl -u openhab-prometheus.service` (or the unit of what is down) shows why")
        return 1


class Answers(Step):
    title = "openHAB answering"

    def __init__(self, context: Context):
        self.context = context

    def check(self, system: System) -> Check:
        url = self.context.url
        version = system.openhab_version(url)
        if version:
            return Check(True, f"openHAB {version} at {url}")
        return Check(False, f"REST at {url} is not ready; startup and add-on loading may take several minutes")

    def actions(self, system: System) -> list[Action]:
        return [Task(f"wait up to {WAIT // 60} minutes for {self.context.url}/rest/", self.wait)]

    def wait(self, system: System) -> int:
        deadline = system.clock() + WAIT
        while system.clock() < deadline:
            if system.openhab_version(self.context.url):
                return 0
            system.sleep(POLL)
        system.say("   still nothing; `sudo journalctl -u openhab.service` shows why")
        return 1


class Token(Step):
    title = "The admin account and an API token"

    def __init__(self, context: Context):
        self.context = context
        self.validated = None
        self.validation_unavailable = False

    def saved(self, system: System) -> str:
        return (system.read_bytes(self.context.area / self.context.token_file) or b"").decode("utf-8", "replace").strip()

    def startup_remaining(self, system: System, *, deadline: float | None = None) -> float | None:
        """The native startup allowance left, or unknown without a start time."""
        timeout = POLL if deadline is None else max(0.001, min(POLL, deadline - system.clock()))
        unit = system.unit("openhab.service", timeout=timeout)
        if unit.active != "active" or unit.since is None:
            return None
        age = max(0, system.wall_time() - unit.since)
        return max(0, WAIT - age)

    def status(self, system: System, token: str, deadline: float) -> int | None:
        found = system.native_probe(self.context.url, token, "/rest/things?summary=true", deadline)
        if system.clock() >= deadline:
            return 0
        if found.status == 200 and (found.problem or not isinstance(found.value, list)):
            return 0
        return found.status

    @staticmethod
    def transient(status: int | None) -> bool:
        return status in (None, 0, 404) or 500 <= status <= 599

    def check(self, system: System) -> Check:
        token = self.context.token_file
        saved = self.saved(system)
        if saved != self.validated:
            self.validated = None
        if not saved:
            return Check(False, f"no {token} yet")
        # Only the exact token validated by this one step can bypass runner's
        # immediate post-action query. NativeReady performs fresh full reads.
        status = 200 if self.validated == saved else self.status(system, saved, system.clock() + POLL)
        if status == 200:
            self.validated = saved
            return Check(True, f"{token} is saved and openHAB accepts it")
        if status in (401, 403):
            return Check(False, f"openHAB refuses the token in {token} (HTTP {status}); "
                         "check authentication readiness before replacing the saved credential")
        return Check(False, f"{token} is saved but openHAB authentication is not ready; keep the saved credential")

    def actions(self, system: System) -> list[Action]:
        if self.saved(system):
            return [Task(f"check the saved API token, allowing up to {WAIT // 60} minutes for authentication readiness "
                         "before offering replacement", self.wait)]
        return [Task(f"save the API token you create in Main UI as {self.context.token_file} (mode 0600)",
                     self.save, interactive=True)]

    def wait(self, system: System) -> int:
        saved = self.saved(system)
        if not saved:
            system.say("   the saved API token is no longer available; nothing was changed")
            return 1
        deadline = system.clock() + WAIT
        while True:
            if system.clock() >= deadline:
                system.say("   openHAB authentication did not become ready within the startup allowance; "
                           "the saved API token was not changed. Check `sudo journalctl -u openhab.service`, then rerun.")
                return 1
            if self.saved(system) != saved:
                self.validated = None
                system.say("   the saved API token changed during validation; nothing was changed; rerun to check it")
                return 1
            status = self.status(system, saved, deadline)
            if status == 200:
                self.validated = saved
                return 0
            established = status in (401, 403) and self.startup_remaining(system, deadline=deadline) == 0
            if established and system.clock() < deadline:
                root = system.native_probe(self.context.url, "", "/rest/", deadline)
                info = root.value.get("runtimeInfo") if isinstance(root.value, dict) else None
                established = (root.status == 200 and not root.problem and isinstance(info, dict)
                               and isinstance(info.get("version"), str) and bool(info["version"]))
            if established and system.clock() < deadline:
                if not system.interactive():
                    system.say("   the established runtime refuses the saved API token; replacement needs you at a terminal; nothing was changed")
                    return 1
                return self.save(system)
            if not self.transient(status) and status not in (401, 403):
                system.say(f"   openHAB refused the saved API token (HTTP {status}); nothing was changed")
                return 1
            left = deadline - system.clock()
            if left <= 0:
                system.say("   openHAB authentication did not become ready within the startup allowance; "
                           "the saved API token was not changed. Check `sudo journalctl -u openhab.service`, then rerun.")
                return 1
            system.sleep(min(POLL, left))

    def save(self, system: System) -> int:
        system.say(f"   Open {self.context.lan_url}/ , create the admin account, then an API token under its "
                   "profile (the person icon).")
        deadline = None
        for _ in range(PASTES):
            token = system.secret("   Paste the token (it is not shown), then Enter: ").strip()
            if deadline is None:
                deadline = system.clock() + WAIT
            problem = self.problem(system, token, deadline=deadline)
            if problem is None:
                try:
                    system.write_private(self.context.area / self.context.token_file, token + "\n")
                except (OSError, ValueError):
                    self.validated = None
                    system.say("   could not complete a safe private token write; check the operator credential path and rerun")
                    return 1
                return 0
            system.say(f"   {problem}; nothing was saved.")
            if self.validation_unavailable:
                return 1
        return 1

    def problem(self, system: System, token: str, *, deadline: float | None = None) -> str | None:
        """Why token will not do, or None when openHAB accepts it."""
        self.validation_unavailable = False
        if not token or any(c.isspace() for c in token):
            return "That is not a token"
        # openHAB's tokens are oh.<name>.<secret>, and neither part holds a dot.
        if token.count(".") > 2:
            return "That looks like more than one paste run together (the prompt shows nothing, so paste once)"
        deadline = system.clock() + WAIT if deadline is None else deadline
        waiting = False
        while system.clock() < deadline:
            status = self.status(system, token, deadline)
            if status == 200:
                self.validated = token
                return None
            if not self.transient(status):
                return f"openHAB refused that token (HTTP {status})"
            if not waiting:
                system.say("   native authentication is not ready; retrying this same paste without saving it yet")
                waiting = True
            left = deadline - system.clock()
            if left > 0:
                system.sleep(min(POLL, left))
        self.validation_unavailable = True
        return "openHAB authentication did not become ready within the startup allowance"


class _RestartBeforeDeadline:
    """Keep runner's root approval, checking its elapsed time before execution."""

    def __init__(self, system, deadline):
        self.system, self.deadline = system, deadline

    def __getattr__(self, name):
        return getattr(self.system, name)

    def run(self, argv, area):
        remaining = self.deadline - self.system.clock()
        if remaining <= 0:
            self.system.say("   startup allowance expired during approval; no restart was requested")
            return 1
        return self.system.run(argv, area, timeout=remaining)


class NativeReady(Step):
    title = "Authenticated native add-ons, Thing handlers and metrics ready"
    grace = 60
    addons_path = "/rest/addons?serviceId=all"
    things_path = "/rest/things?summary=true"
    metrics_path = "/rest/metrics/prometheus"

    def __init__(self, context: Context):
        from oh_sandbox import addon_uids, read_requested_addons
        self.context = context
        self.required = set(addon_uids(read_requested_addons(context.area / "conf")))
        self.deadline = None
        self.signature = None
        self.since = None
        self.restarted = False
        self.previous_generation = None
        self.problem = None

    def reset(self):
        self.signature = self.since = None

    def snapshot(self, system: System) -> tuple[Check, tuple | None]:
        from oh_sandbox import FAIL_DETAILS, HANDLER_PENDING_DETAILS, installed_addon_uids
        if self.deadline is None:
            self.deadline = system.clock() + WAIT
        if system.clock() >= self.deadline:
            return Check(False, "", "native readiness exceeded the 15-minute startup allowance; inspect journalctl -u openhab.service"), None
        token = Token(self.context).saved(system)
        if not token:
            return Check(False, "", "native readiness requires the saved, validated API token; nothing was changed"), None
        if self.previous_generation is not None:
            unit = system.unit("openhab.service", timeout=max(0.001, self.deadline - system.clock()))
            if unit.active != "active" or unit.generation is None or unit.generation <= self.previous_generation:
                return Check(False, "waiting for the requested restart to reach a new active service generation"), None

        values = {}
        for path in (self.addons_path, self.things_path, self.metrics_path):
            found = system.native_probe(self.context.url, token, path, self.deadline,
                                        json_body=path != self.metrics_path)
            if found.status in (401, 403):
                return Check(False, "", f"authenticated native readiness was refused (HTTP {found.status}); keep the saved credential and check access"), None
            if found.problem == "malformed JSON":
                return Check(False, "", f"native readiness: {path} returned malformed JSON"), None
            if found.status == 0 or (found.status == 404 and path != self.metrics_path) or 500 <= found.status <= 599:
                return Check(False, f"waiting for a complete native readiness snapshot ({path} unavailable)"), None
            if found.status != 200 and not (path == self.metrics_path and found.status == 404):
                return Check(False, "", f"native readiness: {path} answered HTTP {found.status}"), None
            values[path] = found
        if system.clock() >= self.deadline:
            return Check(False, "", "native readiness exceeded the 15-minute startup allowance; inspect journalctl -u openhab.service"), None

        addons, things = values[self.addons_path].value, values[self.things_path].value
        valid_addons = isinstance(addons, list) and all(isinstance(row, dict) and type(row.get("installed")) is bool
            and (isinstance(row.get("uid"), str) and bool(row["uid"])
                 or all(isinstance(row.get(key), str) and bool(row[key]) for key in ("type", "id"))) for row in addons)
        valid_things = isinstance(things, list) and all(isinstance(row, dict) and isinstance(row.get("UID"), str)
            and bool(row["UID"]) and isinstance(row.get("statusInfo"), dict)
            and isinstance(row["statusInfo"].get("status"), str) and bool(row["statusInfo"]["status"])
            and (row["statusInfo"].get("statusDetail") is None or isinstance(row["statusInfo"]["statusDetail"], str)) for row in things)
        if not valid_addons or not valid_things:
            return Check(False, "", "native readiness returned an invalid add-on or Thing JSON shape"), None
        if any(row["statusInfo"].get("statusDetail") in FAIL_DETAILS for row in things):
            return Check(False, "", "native Things report a configuration error; fix the configuration before retrying; no recovery restart was requested"), None
        installed = installed_addon_uids(addons)
        missing = sorted(self.required - installed)
        if missing:
            return Check(False, "waiting for required add-ons: " + ", ".join(missing)), None
        pending = [row for row in things if row["statusInfo"]["status"] == "UNINITIALIZED"
                   and row["statusInfo"].get("statusDetail") in HANDLER_PENDING_DETAILS]
        metrics_status = values[self.metrics_path].status
        if not pending and metrics_status == 200:
            return Check(True, "required add-ons installed, no missing Thing handlers, and authenticated native metrics answer"), None
        detail = f"required add-ons installed; {len(pending)} Thing handler(s) waiting; native metrics HTTP {metrics_status}"
        signature = (tuple(sorted(installed)), tuple(sorted((row["UID"], row["statusInfo"]["status"],
                     row["statusInfo"].get("statusDetail") or "NONE") for row in things)), metrics_status) if pending else None
        return Check(False, detail), signature

    def check(self, system: System) -> Check:
        found, signature = self.snapshot(system)
        self.problem = found.problem
        if signature is None:
            self.reset()
        elif signature != self.signature:
            self.signature, self.since = signature, system.clock()
        # A dry-run may reach this before its planned token creation/replacement.
        # Describe the current report, then let the actual task enforce terminal
        # authentication/configuration errors without retrying those failures.
        return Check(found.done, found.detail or found.problem)

    def actions(self, system: System) -> list[Action]:
        return [Task(f"wait within the {WAIT // 60}-minute native startup allowance; offer at most one approved "
                     "same-state restart after unchanged known first-start handler symptoms for 60 seconds", self.wait)]

    def wait(self, system: System) -> int:
        from oh_install import runner
        if self.problem:
            system.say("   " + self.problem)
            return 1
        while True:
            found = self.check(system)
            if self.problem:
                system.say("   " + self.problem)
                return 1
            if found.done:
                return 0
            if not self.restarted and self.since is not None and system.clock() - self.since >= self.grace:
                generation = system.unit("openhab.service", timeout=max(0.001, self.deadline - system.clock())).generation
                if generation is None:
                    system.say("   cannot verify the native service generation; no recovery restart was requested")
                    return 1
                self.restarted = True
                system.say("   installed add-ons still have unchanged missing/not-yet-ready handlers; "
                           "one restart may recover this first-start condition without replacing userdata or credentials")
                command = Command(("systemctl", "--no-block", "restart", "openhab.service"), root=True)
                code = runner.perform(command, _RestartBeforeDeadline(system, self.deadline), self.context.area)
                if code is None or code:
                    return code or 1
                self.previous_generation = generation
                self.reset()
            left = self.deadline - system.clock()
            if left <= 0:
                system.say("   native readiness exceeded the startup allowance; inspect journalctl -u openhab.service")
                return 1
            system.sleep(min(POLL, left))


class Verify(Step):
    title = "Read-only check"
    always = True

    def __init__(self, context: Context):
        self.context = context

    def check(self, system: System) -> Check:
        return Check(False, "every Thing ONLINE, and what is deployed against this checkout")

    def actions(self, system: System) -> list[Action]:
        return [Command(application(self.context, "health", "--production")),
                operator_command(self.context, system, application(self.context, "status"))]


# --------------------------------------------------------------------------
# thermite upgrade


class Committed(Step):
    title = "Preconditions: deploy/ is current, and this area is committed"

    def __init__(self, context: Context):
        self.context = context

    def check(self, system: System) -> Check:
        area = self.context.area
        problems = []
        from oh_paths import SITE_FILE
        argv = (application(self.context, "generate", "--check") if (area / SITE_FILE).is_file()
                else (sys.executable, str(area / "bin/thermite"), "generate", "--check"))
        code, _ = system.capture(argv)
        if code:
            problems.append("conf/ or deploy/ is not what the house file and versions.json generate: "
                            "run thermite generate, then commit")
        code, out = system.capture(["git", "-C", str(area), "status", "--porcelain", "--untracked-files=all",
                                    "--", "."])
        if code:
            problems.append("git cannot tell whether this area is committed")
        elif out.strip():
            problems.append(f"{len(out.splitlines())} uncommitted change(s) in this area: commit them first, "
                            "since installation and upgrades install HEAD")
        busy = busy_ports(self.context, system)
        if busy:
            problems.append(f"port {', '.join(map(str, busy))} in use (ss -ltnp shows by what)")
        if problems:
            return Check(False, "", "; ".join(problems))
        return Check(True, f"deploy/ is generated and HEAD, {self.context.head[:12]}, has every change")


@dataclasses.dataclass(frozen=True)
class Plan:
    """What an upgrade changes: the units to install, the openHAB version
    and image the installed unit runs against the pinned ones, and the
    services not running on their installed unit."""

    units: tuple[str, ...]
    installed_version: str | None
    pinned_version: str
    installed_image: str | None
    pinned_image: str
    services: tuple[str, ...]
    migration: tuple[str, ...] = ()

    @property
    def current(self) -> bool:
        return not self.units and not self.services and not self.migration

    def describe(self) -> str:
        parts = []
        if self.units:
            parts.append("to install: " + ", ".join(self.units))
        if self.installed_image != self.pinned_image:
            parts.append(f"openHAB {self.installed_version or '?'} -> {self.pinned_version} (image "
                         f"{short_image(self.installed_image)} -> {short_image(self.pinned_image)})")
        else:
            parts.append(f"openHAB {self.pinned_version}, as pinned")
        if self.services:
            parts.append("not running on its installed unit: " + ", ".join(self.services))
        if self.migration:
            parts.append("to commission: " + ", ".join(self.migration))
        return "; ".join(parts)


def short_image(image: str | None) -> str:
    if not image:
        return "none"
    digest = image.rsplit("@", 1)[-1]
    return digest[:len("sha256:") + 12] if digest.startswith("sha256:") else image


def plan(context: Context, system: System) -> Plan:
    """What `thermite upgrade` would change on this host. The running
    image needs root to inspect (the containers are rootful), so a service
    counts as running the installed unit's image when it started after the
    unit was installed."""
    text = (system.read_bytes(deploy.INSTALLED[deploy.QUADLET]) or b"").decode("utf-8", "replace")
    services: list[str] = []
    for step in (Services(context), MetricsServices(context)):
        start, restart, enable = step.plan(system)
        services += [*start, *restart, *([step.path_unit] if enable and step.path_unit not in start else [])]
    migration = []
    if not Preconditions(context).check(system).done or not Account(context).check(system).done:
        migration.append("thermite account and group")
    if not NativePreparation(context).check(system).done:
        migration.append("service ownership and offline native preparation")
    return Plan(tuple(Units(context).stale(system)), deploy.unit_version(text), context.versions["openhab"]["version"],
                deploy.unit_image(text), deploy.image(context.versions), tuple(services), tuple(migration))


class WhatChanges(Step):
    title = "What changes"
    always = True

    def __init__(self, plan_: Plan):
        self.plan = plan_

    def check(self, system: System) -> Check:
        return Check(False, self.plan.describe())


def backup_argv(layout: deploy.State, file: Path, grafana: bool = False) -> tuple[str, ...]:
    """Prometheus' samples are left out: up to the retention size, and a
    restored openHAB needs none of them."""
    return ("tar", "--create", "--gzip", "--file", str(file), "--directory", str(layout.root),
            *(f"--exclude={path}" for path in UNSAVED), "conf", "userdata",
            *([str(layout.grafana_data.relative_to(layout.root))] if grafana else []))


def restore_argvs(layout: deploy.State, file: Path, stamp: str, grafana: bool = False) -> list[tuple[str, ...]]:
    """The commands, all under sudo, that put a backup back: openHAB (and
    Grafana, when the backup has its data) stopped, today's conf, userdata
    and Grafana data moved aside (not deleted), the backup unpacked."""
    root = layout.root
    return [("systemctl", "stop", "openhab.service", *(["openhab-grafana.service"] if grafana else [])),
            ("mv", str(layout.userdata), f"{root}/userdata.replaced-{stamp}"),
            ("mv", str(layout.conf), f"{root}/conf.replaced-{stamp}"),
            *([("mv", str(layout.grafana_data), f"{layout.grafana_data}.replaced-{stamp}")] if grafana else []),
            ("tar", "--extract", "--gzip", "--same-owner", "--preserve-permissions", "--file", str(file),
             "--directory", str(root))]


class Backup(Step):
    title = "Back up conf, userdata and Grafana's data, with openHAB and Grafana stopped"

    def __init__(self, context: Context, stamp: str, version: str | None):
        self.context = context
        self.stamp = stamp
        self.version = version or "unknown"
        #: Whether the backup holds Grafana's data, which exists once the
        #: metrics are installed. Its name says so, since a rerun finds a
        #: backup an earlier run took and the restore commands differ.
        self.grafana = False
        self.found: Path | None = None

    @property
    def file(self) -> Path:
        if self.found:
            return self.found
        tag = GRAFANA_TAG if self.grafana else ""
        return self.context.layout.backups / f"{self.stamp}-openhab-{self.version}{tag}.tar.gz"

    def latest(self, system: System) -> Path | None:
        """A backup of the installed version taken since openhab.service last ran, if one is there."""
        unit = system.unit("openhab.service")
        if unit.active in ACTIVE:
            return None
        backups = self.context.layout.backups
        endings = (f"-openhab-{self.version}.tar.gz", f"-openhab-{self.version}{GRAFANA_TAG}.tar.gz")
        for name in reversed(system.listdir(backups)):
            found = system.stat(backups / name)
            if name.endswith(endings) and found and found.mtime > (unit.since or 0):
                return backups / name
        return None

    def check(self, system: System) -> Check:
        if not Units(self.context).stale(system, deploy.OPENHAB_UNITS) and NativePreparation(self.context).check(system).done:
            return Check(True, "openHAB's units are installed already, so nothing changes that a backup would undo")
        latest = self.latest(system)
        if latest:
            self.found, self.grafana = latest, latest.name.endswith(f"{GRAFANA_TAG}.tar.gz")
            return Check(True, f"{latest}")
        self.grafana = system.stat(self.context.layout.grafana_data) is not None
        return Check(False, f"to stop {' and '.join(self.stopped(self.grafana))} and write {self.file}"
                            + (", with Grafana's data" if self.grafana else "")
                            + "; not Prometheus' samples (up to the retention size, and nothing restores them)")

    @staticmethod
    def stopped(grafana: bool) -> tuple[str, ...]:
        # Grafana's SQLite database copied while Grafana writes it may not open again.
        return ("openhab.service", *(["openhab-grafana.service"] if grafana else []))

    def actions(self, system: System) -> list[Action]:
        host, backups = self.context.host, self.context.layout.backups
        self.grafana = system.stat(self.context.layout.grafana_data) is not None
        actions: list[Action] = []
        running = [name for name in self.stopped(self.grafana) if system.unit(name).active in ACTIVE]
        if running:
            actions.append(Command(("systemctl", "stop", *running), root=True))
        directory = system.stat(backups)
        if directory is None or (directory.uid, directory.gid, directory.mode) != (host["uid"], host["gid"], 0o750):
            actions.append(Command(("install", "-d", "-o", str(host["uid"]), "-g", str(host["gid"]), "-m", "0750",
                                    str(backups)), root=True))
        actions.append(Command(("sh", "-c", 'umask 077; exec "$@"', "thermite-backup",
                                *backup_argv(self.context.layout, self.file, self.grafana)), root=True))
        actions.extend([Command(("chown", f"{host['uid']}:{host['gid']}", str(self.file)), root=True),
                        Command(("chmod", "0600", str(self.file)), root=True)])
        return actions


class Restart(Services):
    title = "openHAB and the Apple TV helper (re)started on their units"


class MetricsRestart(MetricsServices):
    title = "The metrics services (re)started on their units"


def metrics_steps(context: Context, restart: bool = False) -> list[Step]:
    """The steps that, once openHAB runs and is deployed, start the metrics
    services on the secrets they read and check that they answer."""
    return [MetricsToken(context), GrafanaPassword(context),
            MetricsRestart(context) if restart else MetricsServices(context), MetricsAnswer(context)]


def upgrade(context: Context, plan_: Plan, stamp: str) -> list[Step]:
    """The upgrade's steps after its preconditions, in order. A host where
    openHAB runs without the metrics gets them here."""
    return [WhatChanges(plan_), Preconditions(context), Packages(), BootTest(context), Backup(context, stamp, plan_.installed_version),
            Account(context), StateDirectories(context), MetricsDirectories(context),
            Units(context), NativePreparation(context), Deploy(context), Restart(context), Answers(context), Token(context), NativeReady(context),
            *metrics_steps(context, restart=True), Verify(context)]


# --------------------------------------------------------------------------
# thermite uninstall


class Stop(Step):
    title = "Stop openHAB, the Apple TV helper and the metrics services, and disable their restart paths"
    SERVICES = ("openhab.service", "openhab-atv.service", "openhab-atv-restart.service", *metrics.SERVICE_UNITS,
                Path(deploy.METRICS_RESTART_SERVICE).name)
    PATHS = (PATH_UNIT, METRICS_PATH_UNIT)

    def running(self, system: System) -> list[str]:
        return [name for name in self.SERVICES if system.unit(name).active in ACTIVE]

    def paths_on(self, system: System) -> list[str]:
        return [name for name in self.PATHS
                if system.unit(name).enabled == "enabled" or system.unit(name).active in ACTIVE]

    def check(self, system: System) -> Check:
        running, paths = self.running(system), self.paths_on(system)
        if not running and not paths:
            return Check(True, "nothing runs")
        parts = (["to stop: " + ", ".join(running)] if running else []) + \
            ([f"to disable and stop: {', '.join(paths)}"] if paths else [])
        return Check(False, "; ".join(parts))

    def actions(self, system: System) -> list[Action]:
        actions: list[Action] = []
        paths = self.paths_on(system)
        if paths:
            actions.append(Command(("systemctl", "disable", "--now", *paths), root=True))
        running = self.running(system)
        if running:
            actions.append(Command(("systemctl", "stop", *running), root=True))
        return actions


class RemoveUnits(Step):
    title = "Remove the units thermite install installed"

    def present(self, system: System) -> list[Path]:
        return [target for _, target in sorted(deploy.INSTALLED.items()) if system.stat(target) is not None]

    def check(self, system: System) -> Check:
        present = self.present(system)
        if present:
            return Check(False, "to remove: " + ", ".join(map(str, present)))
        return Check(True, f"none of the {len(deploy.INSTALLED)} units is installed")

    def actions(self, system: System) -> list[Action]:
        return [Command(("rm", "--", *map(str, self.present(system))), root=True),
                Command(("systemctl", "daemon-reload"), root=True)]


class Purge(Step):
    title = "Delete the state root (--purge)"

    def __init__(self, context: Context):
        self.context = context

    def check(self, system: System) -> Check:
        root = self.context.layout.root
        if system.stat(root) is None:
            return Check(True, f"{root} does not exist")
        return Check(False, f"to delete {root}: conf, userdata (users, tokens, pairings), secrets, the deploy "
                            "records, the backups and the metrics (Prometheus' samples, Grafana's data and the "
                            "metrics secrets)")

    def actions(self, system: System) -> list[Action]:
        root = self.context.layout.root
        if system.fstype(root.parent) == "btrfs":
            delete = ("btrfs", "subvolume", "delete", str(root))
        else:
            delete = ("rm", "-rf", "--one-file-system", "--", str(root))
        return [Task(f"type {root} to confirm", self.confirm, interactive=True), Command(delete, root=True)]

    def confirm(self, system: System) -> int:
        root = str(self.context.layout.root)
        system.say(f"   This deletes {root} and everything in it; nothing brings it back but a copy you made.")
        typed = system.ask(f"   Type {root} to delete it: ").strip()
        if typed != root:
            system.say(f"   That is not {root}; nothing was deleted.")
            return 1
        return 0


def uninstall(context: Context, purge: bool = False) -> list[Step]:
    return [Stop(), RemoveUnits(), *([Purge(context)] if purge else [])]


def install(context: Context) -> list[Step]:
    """Every step, in order."""
    return [Preconditions(context), Committed(context), Packages(), Account(context), StateDirectories(context),
            MetricsDirectories(context), BootTest(context), Units(context), NativePreparation(context), Deploy(context), Services(context),
            Answers(context), Token(context), NativeReady(context), *metrics_steps(context), Verify(context)]
