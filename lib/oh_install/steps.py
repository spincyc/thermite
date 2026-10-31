"""The install, step by step: what each step checks, and what it runs."""

from __future__ import annotations

import dataclasses
import json
import re
import secrets
import shlex
import sys
from pathlib import Path

import oh_deploy as deploy
import oh_logging
import oh_metrics as metrics
from oh_install.model import Action, Check, Command, Step, Task
from oh_install.system import System

#: Podman runs openHAB; Git and make carry the deploys; Python with PyYAML
#: runs this area's tools, and Node.js the tests of its rules.
PACKAGES = {
    "pacman": ("podman", "git", "make", "python", "python-yaml", "nodejs"),
    "apt-get": ("podman", "git", "make", "python3", "python3-yaml", "nodejs"),
}
#: Quadlets need Podman 4.4.
PODMAN = (4, 4)
#: The Apple TV helper's pins have no wheels for anything else.
MACHINE = "x86_64"
NAME = "openhab"
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
ACTIVE = ("active", "activating", "reloading")
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
        return {"openhab.service": [self.host["http_port"], self.host["https_port"], deploy.KARAF_PORT,
                                    deploy.LSP_PORT],
                "openhab-atv.service": [deploy.HELPER_PORT],
                **{service.unit: [int(service.listen.rsplit(":", 1)[1])] for service in metrics.services(self.host)}}


def busy_ports(context: Context, system: System) -> list[int]:
    """The ports a service that is not running needs and something else holds."""
    return [port for service, ports in context.ports().items() if system.unit(service).active != "active"
            for port in ports if not system.port_free(port)]


class Preconditions(Step):
    title = "Preconditions"

    def __init__(self, context: Context):
        self.context = context

    def check(self, system: System) -> Check:
        host = self.context.host
        problems = []
        if system.machine() != MACHINE:
            problems.append(f"this is {system.machine()}; the Apple TV helper's pins need {MACHINE}")
        if not system.overlayfs():
            problems.append(f"the running kernel, {system.kernel()}, has no overlayfs and no modules to load it "
                            "from (it was upgraded since the last boot), and Podman needs it: reboot first")
        for key, by_id, by_name in (("gid", system.group(host["gid"]), system.group(NAME)),
                                    ("uid", system.user(host["uid"]), system.user(NAME))):
            if by_id is not None and by_id.name != NAME:
                problems.append(f"{key} {host[key]} belongs to {by_id.name}, not {NAME}")
            if by_name is not None and getattr(by_name, key) != host[key]:
                problems.append(f"{NAME} has {key} {getattr(by_name, key)}, not {host[key]}")
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
    title = "The openhab user and group, with you in the group"

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
        if group is None or not (login in group.members or (me is not None and me.gid == group.gid)):
            missing.append("member")
        return missing

    def check(self, system: System) -> Check:
        missing = self.missing(system)
        host, login = self.context.host, system.login()
        words = {"group": f"group {NAME} ({host['gid']})", "user": f"user {NAME} ({host['uid']})",
                 "member": f"{login} in group {NAME}"}
        if missing:
            return Check(False, "to create: " + ", ".join(words[m] for m in missing))
        return Check(True, f"{NAME} {host['uid']}:{host['gid']}, {login} a member")

    def actions(self, system: System) -> list[Action]:
        host, missing = self.context.host, self.missing(system)
        actions = []
        if "group" in missing:
            actions.append(Command(("groupadd", "-g", str(host["gid"]), NAME), root=True))
        if "user" in missing:
            shell = system.which("nologin") or "/usr/bin/nologin"
            actions.append(Command(("useradd", "-r", "-u", str(host["uid"]), "-g", str(host["gid"]),
                                    "-d", str(self.context.layout.root), "-s", shell, NAME), root=True))
        if "member" in missing:
            actions.append(Command(("usermod", "-aG", NAME, system.login()), root=True))
        return actions


class StateDirectories(Step):
    title = "The state subvolume and its directories (ADR 0054)"

    def __init__(self, context: Context):
        self.context = context

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
        return [Command(("make", "openhab-test", "TEST=boot"))]


class PairingLogging(Step):
    title = "Keep pairing credentials out of openHAB's native logs"

    def __init__(self, context: Context):
        self.context = context

    @property
    def template(self) -> Path:
        version = self.context.versions["openhab"]["version"]
        return self.context.area / "build" / "sandbox" / f"openhab-{version}" / "userdata"

    def check(self, system: System) -> Check:
        data = system.read_bytes(self.context.layout.userdata / oh_logging.RELATIVE)
        selector = system.read_bytes(self.context.layout.userdata / oh_logging.SELECTOR)
        if data is not None and selector is not None and oh_logging.selected_default(selector) and oh_logging.protected(data):
            return Check(True, "native logger filters drop pairing command payloads")
        return Check(False, "install the pairing filter before deploying its Items; an existing service is stopped "
                            "and started so the filter is loaded before pairing is offered")

    def actions(self, system: System) -> list[Action]:
        actions: list[Action] = []
        if system.stat(self.template / oh_logging.RELATIVE) is None:
            # A boot receipt can outlive a tidied runtime cache.
            actions.append(Command(("bin/thermite", "sandbox", "fetch")))
        running = system.unit("openhab.service").active not in ("inactive", "failed", "unknown")
        if running:
            actions.append(Command(("systemctl", "stop", "openhab.service"), root=True))
        actions.append(Command(("python3", str(self.context.area / "lib" / "oh_logging.py"),
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

    def check(self, system: System) -> Check:
        head, layout = self.context.head, self.context.layout
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
        argv = ("make", "openhab-deploy")
        if self.context.host["gid"] in system.process_groups():
            return [Command(argv)]
        # usermod -aG takes effect at the next login. sg gives this one
        # command the group now. Arch has no sg (util-linux ships only
        # newgrp, which cannot run one command); sudo -u can, since it sets
        # the user's groups afresh from the group database.
        if system.which("sg"):
            return [Command(("sg", NAME, "-c", shlex.join(argv)))]
        return [Command(("-u", system.login(), *argv), root=True)]


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
        actions = []
        if services:
            actions.append(Command(("systemctl", "start", *services), root=True))
        if restart:
            actions.append(Command(("systemctl", "restart", *restart), root=True))
        if enable or self.path_unit in start:
            actions.append(Command(("systemctl", "enable", "--now", self.path_unit), root=True))
        return actions


class MetricsServices(Services):
    title = "The metrics services running: Prometheus, node_exporter, blackbox_exporter, the exporter, Grafana"
    restarts = METRICS_RESTARTS
    path_unit = METRICS_PATH_UNIT


def _secret_wrong(system: System, context: Context, path: Path) -> str | None:
    """Why a file in the metrics secrets is not right, or None when it is."""
    host, found = context.host, system.stat(path)
    if found is None:
        return "missing"
    if (found.uid, found.gid, found.mode) != (host["uid"], host["gid"], deploy.SECRET_FILE_MODE):
        return f"not {NAME}'s with mode {deploy.SECRET_FILE_MODE:04o}"
    return None


def _unseen(system: System, context: Context, path: Path) -> bool:
    """Whether this login cannot see path at all: its directory is the
    openhab group's alone, and usermod -aG puts the owner in that group only
    at their next login."""
    parent = system.stat(path.parent)
    return (system.stat(path) is None and parent is not None and not parent.mode & 0o001
            and context.host["gid"] not in system.process_groups())


#: What a secret step says of a file it installed but this login cannot see.
UNSEEN = f"installed; this login, not in group {NAME} yet, cannot see it (the next one can)"


def _install_secret(context: Context, source: Path, target: Path) -> Command:
    host = context.host
    return Command(("install", "-o", str(host["uid"]), "-g", str(host["gid"]), "-m", f"{deploy.SECRET_FILE_MODE:04o}",
                    str(source), str(target)), root=True)


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
            return Check(False, f"{self.target}: this login cannot see it (not in group {NAME} yet); to install")
        problem = self.problem(system)
        if problem:
            return Check(False, f"{self.target}: {problem}")
        return Check(True, f"{self.target} holds the token in {self.context.token_file}")

    def actions(self, system: System) -> list[Action]:
        self.unseen = _unseen(system, self.context, self.target)
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
            return Check(False, f"{self.target}: this login cannot see it (not in group {NAME} yet); to install "
                                f"from {self.context.grafana_password_file}, generated once")
        wrong = _secret_wrong(system, self.context, self.target)
        if wrong == "missing":
            return Check(False, f"to generate once: {self.target}")
        if wrong:
            return Check(False, f"{self.target}: {wrong}")
        return Check(True, f"in {self.target}; the admin login is admin")

    def actions(self, system: System) -> list[Action]:
        host = self.context.host
        self.unseen = _unseen(system, self.context, self.target)
        if system.stat(self.target) is not None:
            return [Command(("chown", f"{host['uid']}:{host['gid']}", str(self.target)), root=True),
                    Command(("chmod", f"{deploy.SECRET_FILE_MODE:04o}", str(self.target)), root=True)]
        actions: list[Action] = []
        copy = self.context.grafana_password_file
        if not (system.read_bytes(self.context.area / copy) or b"").strip():
            actions.append(Task(f"generate a random password into {copy} (mode 0600)", self.generate))
        actions.append(_install_secret(self.context, copy, self.target))
        return actions

    def generate(self, system: System) -> int:
        # No newline: Grafana takes the file's bytes as the password.
        system.write_private(self.context.area / self.context.grafana_password_file, secrets.token_urlsafe(24))
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
        return Check(False, f"{health.describe()}{down}: the first start pulls the images, then Prometheus "
                            "scrapes each target within a minute")

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
        return Check(False, f"nothing answers at {url} yet: the first start installs the add-ons, a few minutes")

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

    def check(self, system: System) -> Check:
        token = self.context.token_file
        saved = (system.read_bytes(self.context.area / token) or b"").decode("utf-8", "replace").strip()
        if not saved:
            return Check(False, f"no {token} yet")
        status = system.token_status(self.context.url, saved)
        if status in (401, 403):
            return Check(False, f"openHAB refuses the token in {token} (HTTP {status})")
        return Check(True, f"{token} is saved" + (" and openHAB accepts it" if status == 200 else ""))

    def actions(self, system: System) -> list[Action]:
        return [Task(f"save the API token you create in Main UI as {self.context.token_file} (mode 0600)",
                     self.save, interactive=True)]

    def save(self, system: System) -> int:
        system.say(f"   Open {self.context.lan_url}/ , create the admin account, then an API token under its "
                   "profile (the person icon).")
        for _ in range(PASTES):
            token = system.secret("   Paste the token (it is not shown), then Enter: ").strip()
            problem = self.problem(system, token)
            if problem is None:
                system.write_private(self.context.area / self.context.token_file, token + "\n")
                return 0
            system.say(f"   {problem}; nothing was saved.")
        return 1

    def problem(self, system: System, token: str) -> str | None:
        """Why token will not do, or None when openHAB accepts it."""
        if not token or any(c.isspace() for c in token):
            return "That is not a token"
        status = system.token_status(self.context.url, token)
        if status == 200:
            return None
        if status is None:
            return f"openHAB did not answer at {self.context.url}, so the token could not be checked"
        # openHAB's tokens are oh.<name>.<secret>, and neither part holds a dot.
        if token.count(".") > 2:
            return "That looks like more than one paste run together (the prompt shows nothing, so paste once)"
        return f"openHAB refused that token (HTTP {status})"


class Verify(Step):
    title = "Read-only check"
    always = True

    def __init__(self, context: Context):
        self.context = context

    def check(self, system: System) -> Check:
        return Check(False, "every Thing ONLINE, and what is deployed against this checkout")

    def actions(self, system: System) -> list[Action]:
        return [Command(("bin/thermite", "health", "--production")), Command(("bin/thermite", "status"))]


# --------------------------------------------------------------------------
# thermite upgrade


class Committed(Step):
    title = "Preconditions: deploy/ is current, and this area is committed"

    def __init__(self, context: Context):
        self.context = context

    def check(self, system: System) -> Check:
        area = self.context.area
        problems = []
        code, _ = system.capture([sys.executable, str(area / "bin/thermite"), "generate", "--check"])
        if code:
            problems.append("conf/ or deploy/ is not what the house file and versions.json generate: "
                            "run thermite generate, then commit")
        code, out = system.capture(["git", "-C", str(area), "status", "--porcelain", "--untracked-files=all",
                                    "--", "."])
        if code:
            problems.append("git cannot tell whether this area is committed")
        elif out.strip():
            problems.append(f"{len(out.splitlines())} uncommitted change(s) in this area: commit them first, "
                            "since an upgrade installs HEAD")
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

    @property
    def current(self) -> bool:
        return not self.units and not self.services

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
    return Plan(tuple(Units(context).stale(system)), deploy.unit_version(text), context.versions["openhab"]["version"],
                deploy.unit_image(text), deploy.image(context.versions), tuple(services))


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
        if not Units(self.context).stale(system, deploy.OPENHAB_UNITS):
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
        if system.stat(backups) is None:
            actions.append(Command(("install", "-d", "-o", str(host["uid"]), "-g", str(host["gid"]), "-m", "0750",
                                    str(backups)), root=True))
        actions.append(Command(backup_argv(self.context.layout, self.file, self.grafana), root=True))
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
    return [WhatChanges(plan_), BootTest(context), Backup(context, stamp, plan_.installed_version),
            MetricsDirectories(context), PairingLogging(context), Units(context), Restart(context), Deploy(context), Answers(context),
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
    return [Preconditions(context), Packages(), Account(context), StateDirectories(context),
            MetricsDirectories(context), BootTest(context), PairingLogging(context), Deploy(context), Units(context), Services(context),
            Answers(context), Token(context), *metrics_steps(context), Verify(context)]
