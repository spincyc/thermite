"""The install, step by step: what each step checks, and what it runs."""

from __future__ import annotations

import dataclasses
import json
import re
import shlex
import stat
import sys
import tempfile
from pathlib import Path

import oh_addons
import oh_deploy as deploy
import oh_apparmor
import oh_http
import oh_logging
import oh_service
import oh_units
import oh_ports
import oh_metrics as metrics
import oh_music as music
import oh_shares as shares
from oh_install import manifest, ownership, progress
from oh_install.access import Gateway
from oh_install.model import Action, Check, Command, ReadinessStep, Step, Task
from oh_install.system import System

#: Podman runs openHAB through runc; Git and make carry the deploys; Python with PyYAML
#: runs this area's tools; Node.js and npm support the terminal remote; util-linux
#: supplies setpriv for Grafana's credential readiness checks.
PACKAGES = {
    "pacman": ("podman", "runc", "util-linux", "git", "make", "python", "python-yaml", "nodejs", "npm", "openssl", "nftables"),
    # Podman's --init binary: a dependency of Arch's podman, only recommended by Ubuntu's.
    "apt-get": ("podman", "runc", "catatonit", "util-linux", "git", "make", "python3", "python3-yaml", "python3-venv", "nodejs", "npm", "openssl", "nftables"),
}
#: Quadlets and the additive node-exporter confinement override need Podman 4.5.
PODMAN = (4, 5)
#: The Apple TV helper's pins have no wheels for anything else.
MACHINE = "x86_64"
NAME = oh_service.ACCOUNT
#: node_exporter's own account.
NODE = metrics.NODE_EXPORTER_ACCOUNT
#: The unit each quadlet in deploy/ runs.
RESTARTS = {
    deploy.QUADLET: "thermite-openhab.service",
    deploy.ATV_QUADLET: "thermite-atv.service",
}
METRICS_RESTARTS = {quadlet: unit for unit, quadlet in deploy.METRICS_QUADLETS.items()}
MUSIC_RESTARTS = {deploy.MUSIC_QUADLET: oh_units.MUSIC}
#: How long the metrics services' first start may take (it pulls the
#: images, then Prometheus scrapes every target once).
METRICS_WAIT = 5 * 60
#: How long Music Assistant may take to run: an upgrade may migrate its
#: library database first.
MUSIC_WAIT = 10 * 60
#: How long openHAB's first start may take to answer (it installs the
#: add-ons), and how often to ask.
WAIT = 15 * 60
POLL = 5
#: Bounds for an operator's --native-timeout: one native readiness allowance
#: shared by runtime startup, gateway commissioning and final settlement.
NATIVE_TIMEOUT_RANGE = (5 * 60, 2 * 60 * 60)


def native_timeout_argument(text: str) -> int:
    """argparse type for --native-timeout SECONDS."""
    import argparse
    low, high = NATIVE_TIMEOUT_RANGE
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError("--native-timeout takes whole seconds") from None
    if not low <= value <= high:
        raise argparse.ArgumentTypeError(f"--native-timeout must be between {low} and {high} seconds")
    return value


class NativeDeadline:
    """One native readiness allowance per run.

    Runtime startup, runtime access validation, the settle before gateway
    commissioning, commissioning itself and the final readiness wait spend
    one budget. Each native phase gets an absolute deadline for what is left;
    only time inside native phases counts, so monitoring image downloads and
    other steps between them cannot exhaust it. A step constructed alone (a
    dry run, a later check) gets its own allowance.
    """

    def __init__(self, seconds: int | None = None):
        self.seconds = seconds
        self.spent = 0.0
        self.deadline: float | None = None
        self.began: float | None = None

    @classmethod
    def of(cls, context) -> NativeDeadline:
        return cls(getattr(context, "native_timeout", None))

    @property
    def budget(self) -> float:
        # Read WAIT when used, so the default follows the module setting.
        return WAIT if self.seconds is None else self.seconds

    def start(self, system: System) -> float:
        """The current native phase's absolute deadline, beginning a phase if needed."""
        if self.deadline is None:
            self.began = system.clock()
            self.deadline = self.began + max(0.0, self.budget - self.spent)
        return self.deadline

    def stop(self, system: System) -> None:
        """End the current native phase; its time is spent."""
        if self.deadline is not None:
            began = self.deadline if self.began is None else self.began
            self.spent += max(0.0, min(system.clock(), self.deadline) - began)
        self.deadline = self.began = None

    def remaining(self, system: System) -> float:
        return self.start(system) - system.clock()

    def describe(self) -> str:
        minutes = self.budget / 60
        return f"{minutes:g}-minute" if minutes >= 1 else f"{self.budget:g}-second"


class UnitWatch:
    """End a native wait early when its service failed or keeps restarting.

    systemd assigns each activation a new active-enter timestamp. Restarts a
    wait requested itself are excluded; others mean a crash loop.
    """

    restarts = 3

    def __init__(self, name: str = "thermite-openhab.service"):
        self.name = name
        self.generations: list[int] = []
        self.requested = 0

    def problem(self, system: System, deadline: float) -> str | None:
        unit = system.unit(self.name, timeout=max(0.001, min(POLL, deadline - system.clock())))
        hint = f"`sudo journalctl -u {self.name}` shows why; fix the cause, then rerun"
        if unit.active == "failed":
            return f"{self.name} failed; {hint}"
        generation = unit.generation if type(unit.generation) is int else None
        if generation is not None and (not self.generations or self.generations[-1] != generation):
            self.generations.append(generation)
        restarted = len(self.generations) - 1 - self.requested
        if restarted >= self.restarts:
            return f"{self.name} restarted {restarted} times during this wait (crash loop); {hint}"
        return None
ACTIVE = ("active", "activating", "reloading", "deactivating")
#: The install engine's transactions, in the installation root.
TRANSACTIONS = ".transaction"


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
    runtime_token_file: Path | None = None
    # Schema-2 sites render only committed inputs with their exact cached
    # application. Keep these reviewed bytes authoritative throughout the run.
    prepared: dict[str, deploy.Blob] | None = None
    application: dict | None = None
    restart_runtime: bool = False
    #: The operator's --native-timeout, in seconds; None selects WAIT.
    native_timeout: int | None = None

    @property
    def var(self) -> Path:
        return self.area / "var"

    @property
    def configuration(self) -> Path:
        if self.prepared is None:
            return self.area
        return self.var / "site-builds" / f"{self.head}-{self.application['digest']}"

    @property
    def program(self) -> Path:
        from oh_paths import PROGRAM
        return self.var / "application-releases" / self.application["digest"] if self.application is not None else PROGRAM

    def generated(self) -> dict[str, deploy.Blob]:
        if self.prepared is None:
            return {}
        files = {name: blob for name, blob in self.prepared.items()
                 if name.startswith(("conf/", "metrics/", "deploy/", "instance/"))}
        files["versions.json"] = deploy.Blob((json.dumps(self.versions, sort_keys=True) + "\n").encode())
        return files

    def unit(self, name: str) -> bytes | None:
        """A unit's bytes; None for one this host does not have (music)."""
        if name not in deploy.host_units(self.host):
            return None
        return self.prepared[name].data if self.prepared is not None else (self.area / name).read_bytes()

    @property
    def installation(self) -> deploy.Installation:
        return deploy.installation(self.host)

    @property
    def layout(self) -> deploy.State:
        return self.installation.state

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
    def runtime_credentials_file(self) -> Path:
        return self.token_file.parent / "runtime-credentials.json"

    def share_login(self, name: str) -> Path:
        """The owner's private copy of an SMB share's login, relative to the
        area, beside the API token (oh_shares)."""
        return self.token_file.parent / f"share-{name}.credentials"

    @property
    def grafana_url(self) -> str:
        return deploy.grafana_url(self.host, self.address or self.hostname)

    def ports(self) -> dict[str, list[int]]:
        """The ports each service listens on."""
        ports = oh_ports.effective(self.host)
        return {"thermite-openhab.service": [self.host["http_port"], self.host["https_port"], deploy.KARAF_PORT,
                                    deploy.LSP_PORT, oh_ports.SHUTDOWN_PORT],
                "thermite-atv.service": [deploy.HELPER_PORT],
                **{service.unit: [int(service.listen.rsplit(":", 1)[1])] for service in metrics.services(self.host)},
                **({oh_units.MUSIC: [ports["music"], ports["music_stream"]]} if music.enabled(self.host) else {})}


def committed_context(area: Path, system: System, runtime_token_file: Path | None = None, *,
                      revision: str | None = None) -> Context:
    """Plan schema-2 installs from committed inputs, never working output:
    HEAD's, or `revision`'s (thermite rollback), which must pin the same
    application release.

    Rendering may use a cleaned temporary directory; planning does not
    materialize a site build, fetch anything or change production state.
    Complete checkouts and schema-1 sites retain their existing behavior.
    """
    from oh_paths import descriptor
    head = deploy.resolve(area, revision or "HEAD")
    selected = descriptor(area)
    files = application_ = None
    if selected and selected["version"] == 2:
        versions, host, committed = deploy.committed_inputs(area, head)
        if committed != selected and revision is not None:
            raise deploy.DeployError(f"{head[:12]} pins another application release than the site's HEAD; "
                                     "installing it needs that release")
        if committed != selected:
            raise deploy.DeployError("commit the site's application requirement before installation")
        files, application_ = deploy.prepared_files(area, head, area / "var")
        conf = {name.removeprefix("conf/"): blob for name, blob in files.items() if name.startswith("conf/")}
        if not conf or application_ is None:
            raise deploy.DeployError("the committed site did not produce a complete configuration")
    else:
        versions, host = deploy.load(area)
        conf = deploy.read_tree(area / "conf")
    deploy.check_pins(versions, host)
    return Context(area, versions, host, head, deploy.tree_digest(conf), system.hostname(), system.address(),
                   runtime_token_file, files, application_)


class DerivedConfiguration(Step):
    """Materialize an exact, private build only after workflow approval."""

    title = "Committed configuration cache"

    def __init__(self, context: Context):
        self.context = context

    def matches(self) -> bool:
        root = self.context.configuration
        if not root.exists() and not root.is_symlink():
            return False
        expected, found = self.context.generated(), {}
        for parent in (root, *root.parents):
            if parent.is_symlink():
                raise deploy.DeployError("configuration cache paths must not contain symlinks")
        if not root.is_dir():
            raise deploy.DeployError("configuration cache must be a directory")
        directories = {parent.as_posix() for name in expected for parent in Path(name).parents if parent != Path(".")}
        for path in root.rglob("*"):
            info = path.lstat()
            name = path.relative_to(root).as_posix()
            if stat.S_ISLNK(info.st_mode) or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                raise deploy.DeployError("configuration cache contains an unsafe entry")
            if stat.S_ISDIR(info.st_mode) and name not in directories:
                raise deploy.DeployError("configuration cache differs from the committed build; inspect it before retrying")
            if stat.S_ISREG(info.st_mode):
                if info.st_nlink != 1:
                    raise deploy.DeployError("configuration cache contains a hard-linked file")
                if name not in expected or info.st_size != len(expected[name].data):
                    raise deploy.DeployError("configuration cache differs from the committed build; inspect it before retrying")
                found[name] = deploy.Blob(path.read_bytes(), bool(info.st_mode & 0o111))
        if found != expected:
            raise deploy.DeployError("configuration cache differs from the committed build; inspect it before retrying")
        return True

    def check(self, system: System) -> Check:
        import oh_release
        try:
            store = self.context.var / "application-releases"
            program = oh_release.resolve(store, self.context.application["digest"], self.context.application["version"])
            if program != self.context.program:
                return Check(False, "save the exact installed application and prepare the committed configuration")
            done = self.matches()
        except (OSError, ValueError, deploy.DeployError) as exc:
            return Check(False, "", str(exc))
        return Check(done, f"committed build {self.context.head[:12]} verified" if done else
                     "prepare the committed configuration in the private build cache")

    def actions(self, system: System) -> list[Action]:
        return [Task("prepare the committed configuration cache", self.prepare)]

    def prepare(self, system: System) -> int:
        import oh_release
        # Approval does not allow a new commit or edits to replace the plan.
        if deploy.resolve(self.context.area, "HEAD") != self.context.head or deploy.uncommitted(self.context.area):
            raise deploy.DeployError("the site changed after planning; rerun installation from the intended clean commit")
        store = self.context.var / "application-releases"
        program = oh_release.resolve(store, self.context.application["digest"], self.context.application["version"])
        if program != store / self.context.application["digest"]:
            oh_release.cache(program, store)
        if self.matches():
            return 0
        root = self.context.configuration
        for parent in (root.parent, *root.parent.parents):
            if parent.is_symlink():
                raise deploy.DeployError("configuration cache paths must not contain symlinks")
        root.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".build-", dir=root.parent) as temporary:
            complete = Path(temporary) / "complete"
            complete.mkdir(mode=0o700)
            for name, blob in self.context.generated().items():
                # Names originate in the finite release render, but fence the
                # filesystem seam independently before creating a file.
                target = complete / oh_release.safe(name)
                target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
                target.write_bytes(blob.data)
                target.chmod(0o500 if blob.program else 0o400)
            try:
                complete.rename(root)
            except FileExistsError:
                if not self.matches():
                    raise
        if not self.matches():
            raise deploy.DeployError("configuration cache was not prepared")
        return 0


def configuration_steps(context: Context) -> list[Step]:
    return [DerivedConfiguration(context)] if context.prepared is not None else []


def busy_ports(context: Context, system: System) -> list[int]:
    """The ports a service that is not running needs and something else holds."""
    owned = ownership.inspect(context, system)
    return [port for service, ports in context.ports().items()
            if not any(system.unit(name).active == "active" for name in owned.units((service,)))
            for port in ports if not system.port_free(port)]


def application(context: Context, *args: str) -> tuple[str, ...]:
    """Use the selected site's application, including through sg and sudo.

    A private site's launcher is tied to its Python environment. The system
    python and a relative source Makefile cannot substitute for that release.
    Original source checkouts retain their established commands.
    """
    from oh_paths import SITE_FILE
    if (context.area / SITE_FILE).is_file():
        return (sys.executable, str(context.program / "bin/thermite"), "--config", str(context.area), *args)
    return ("bin/thermite", *args)


def write(context: Context, *args: str) -> tuple[str, ...]:
    """One of the transaction's own writes below the installation root
    (oh_install.writes), run with this release's Python; operator_command
    gives it the thermite group."""
    return (sys.executable, str(context.program / "lib/oh_install/writes.py"), *args)


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
            if by_id is not None and by_id.name != NAME:
                problems.append(f"{key} {host[key]} belongs to {by_id.name}, not {NAME}; "
                                "refusing to rename or adopt another service account")
            if by_name is not None and getattr(by_name, key) != host[key]:
                problems.append(f"{NAME} has {key} {getattr(by_name, key)}, not {host[key]}")
        named = system.user(NAME)
        if named is not None and named.gid != host["gid"]:
            problems.append(f"{named.name} has primary gid {named.gid}, not {host['gid']}")
        problems.extend(ownership.inspect(self.context, system).problems)
        top = self.context.installation.root
        if not system.safe_directory(self.context.layout.root):
            problems.append(f"{self.context.layout.root} has a symlink or unreadable ancestor; "
                            "refusing to change state outside the configured directory")
        elif not installation_owned(self.context, system):
            problems.append(f"cannot prove {top} is Thermite's installation root: it may hold only "
                            f"{deploy.RUNTIME}/ and {deploy.ACCESS}/; refusing to adopt or change unrelated data")
        elif not state_owned(self.context, system):
            problems.append(f"cannot prove {self.context.layout.root} is Thermite state or empty installation "
                            "scaffolding; refusing to adopt or change unrelated data")
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

    def __init__(self, context: Context | None = None):
        #: With a context, also what mounts its host's network shares.
        self.context = context

    def manager(self, system: System) -> str | None:
        return next((name for name in PACKAGES if system.which(name)), None)

    def names(self, system: System, manager: str) -> tuple[str, ...]:
        return (*PACKAGES[manager], *(("apparmor",) if oh_apparmor.enabled(system) else ()),
                *(shares.packages(self.context.host, manager) if self.context is not None else ()))

    def missing(self, system: System, manager: str) -> list[str]:
        names = self.names(system, manager)
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
        try:
            missing = self.missing(system, manager)
        except ValueError as exc:
            return Check(False, "", str(exc))
        if missing:
            return Check(False, f"to install: {' '.join(missing)}")
        version = podman_version(system)
        if version is None or version < PODMAN:
            found = "no version" if version is None else ".".join(map(str, version))
            return Check(False, "", f"Podman reports {found}; quadlets and the node confinement override need {'.'.join(map(str, PODMAN))} or later")
        return Check(True, f"{' '.join(self.names(system, manager))}; Podman {'.'.join(map(str, version))}")

    def actions(self, system: System) -> list[Action]:
        manager = self.manager(system)
        missing = self.missing(system, manager)
        if manager == "pacman":
            return [Command(("pacman", "-S", "--needed", *missing), root=True,
                            approved_argv=("pacman", "--noconfirm", "-S", "--needed", *missing), terminal=True)]
        return [Command(("apt-get", "install", *missing), root=True,
                        approved_argv=("apt-get", "--assume-yes", "install", *missing), terminal=True)]


class NodeConfinement(Step):
    title = "Node exporter confinement"
    always = True

    def __init__(self, context: Context):
        self.context = context

    def stale(self, system: System, enabled: bool) -> list[Path]:
        return [path for path, content in oh_apparmor.expected(enabled).items()
                if (info := system.stat(path)) is None or info.is_dir
                or (info.uid, info.gid, info.mode) != (0, 0, 0o644) or system.read_bytes(path) != content]

    def parser(self, system: System) -> str | None:
        parser = system.which("apparmor_parser")
        return parser if parser and Path(parser).is_absolute() else None

    def check(self, system: System) -> Check:
        try:
            enabled = oh_apparmor.enabled(system)
            stale = self.stale(system, enabled)
        except (OSError, ValueError) as exc:
            return Check(False, "", f"cannot verify node exporter confinement: {exc}")
        if enabled and not self.parser(system):
            return Check(False, "", "the kernel enables AppArmor but apparmor_parser is unavailable after package preparation; fix the AppArmor installation before continuing")
        waiting = ["to install root-owned configuration: " + ", ".join(map(str, stale))] if stale else []
        if enabled:
            unit = system.unit("apparmor.service")
            if unit.active != "active" or unit.enabled != "enabled":
                waiting.append("to enable and start apparmor.service")
            config, policy = system.stat(oh_apparmor.CONFIG), system.stat(oh_apparmor.POLICY)
            if config is not None and policy is not None and policy.mtime > config.mtime:
                waiting.append("to reload the changed node exporter policy")
        if waiting:
            return Check(False, "; ".join(waiting))
        return Check(True, "configuration current; enforcement is verified by the approved root command on every run" if enabled else
                     "kernel AppArmor disabled; neutral service configuration current")

    def actions(self, system: System) -> list[Action]:
        PROGRAM = self.context.program
        enabled = oh_apparmor.enabled(system)
        parser = self.parser(system) if enabled else None
        if enabled and parser is None:
            raise ValueError("the required AppArmor parser is unavailable; confinement was not disabled")
        actions = []
        if self.stale(system, enabled):
            actions.append(Command(("python3", str(PROGRAM / "lib/oh_apparmor.py")), root=True))
            if not enabled:
                actions.append(Command(("touch", str(oh_apparmor.CONFIG)), root=True))
        if enabled:
            unit = system.unit("apparmor.service")
            if unit.active != "active" or unit.enabled != "enabled":
                actions.append(Command(("systemctl", "enable", "--now", "apparmor.service"), root=True))
            # securityfs can deny the owner even when its mode is world-readable.
            # The helper verifies actual enforcement as root, and only repairs
            # and touches CONFIG when the loaded policy needs replacement.
            actions.append(Command(("python3", str(PROGRAM / "lib/oh_apparmor.py"), "--verify", parser), root=True))
        return actions

    def desired(self, system: System) -> list[manifest.Item]:
        classes = {oh_apparmor.POLICY: "apparmor"}
        return [manifest.Item(path, classes.get(path, "file")) for path in oh_apparmor.expected(oh_apparmor.enabled(system))]


class Account(Step):
    title = "Service account and operator access"

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
        for key, found in (("uid", system.user(self.context.host["uid"])),
                           ("gid", system.group(self.context.host["gid"]))):
            if found is not None and found.name != NAME:
                return Check(False, "", f"{key} {self.context.host[key]} belongs to {found.name}; "
                             "refusing to rename or adopt another service account")
        own = metrics.node_exporter_id(self.context.host)
        for key, found in (("uid", system.user(own)), ("gid", system.group(own))):
            if found is not None and found.name != NODE:
                return Check(False, "", f"{key} {own}, for node_exporter's own account {NODE}, belongs to "
                             f"{found.name}; set metrics: node_exporter_uid in instance/host.yaml to a free number")
        user, group = system.user(NODE), system.group(NODE)
        if {*((user.uid, user.gid) if user else ()), *((group.gid,) if group else ())} - {own}:
            return Check(False, "", f"{NODE} exists with another uid or gid than {own}; set metrics: "
                         "node_exporter_uid in instance/host.yaml to its number, or remove that account")
        missing = [*self.missing(system), *self.node_missing(system)]
        host, login = self.context.host, system.login()
        words = {"group": f"group {NAME} ({host['gid']})", "user": f"user {NAME} ({host['uid']})",
                 "member": f"{login} in group {NAME}", "settings": "service home and nologin shell",
                 "node group": f"group {NODE} ({own})", "node user": f"user {NODE} ({own})"}
        if missing:
            return Check(False, "to configure: " + ", ".join(words[m] for m in missing))
        return Check(True, f"{NAME} {host['uid']}:{host['gid']}, {login} a member; {NODE} {own}:{own}")

    def actions(self, system: System) -> list[Action]:
        host, missing = self.context.host, self.missing(system)
        actions = []
        if "group" in missing:
            actions.append(Command(("groupadd", "-g", str(host["gid"]), NAME), root=True))
        if "user" in missing:
            shell = system.which("nologin") or "/usr/bin/nologin"
            actions.append(Command(("useradd", "-r", "-u", str(host["uid"]), "-g", str(host["gid"]),
                                    "-d", str(self.context.layout.root), "-s", shell, NAME), root=True))
        elif "settings" in missing:
            shell = system.which("nologin") or "/usr/bin/nologin"
            actions.append(Command(("usermod", "-d", str(self.context.layout.root), "-s", shell, NAME), root=True))
        if "member" in missing:
            actions.append(Command(("usermod", "-aG", NAME, system.login()), root=True))
        own, absent = str(metrics.node_exporter_id(host)), self.node_missing(system)
        if "node group" in absent:
            actions.append(Command(("groupadd", "-g", own, NODE), root=True))
        if "node user" in absent:
            shell = system.which("nologin") or "/usr/bin/nologin"
            actions.append(Command(("useradd", "-r", "-u", own, "-g", own, "-d", "/nonexistent", "-s", shell, NODE),
                                   root=True))
        return actions

    def node_missing(self, system: System) -> list[str]:
        """What of node_exporter's own account is to make. It has a real
        account, not a bare number: the number stays reserved, and a system
        bus that looks its callers up (dbus-daemon) finds it. It owns no
        file, has no home, and nobody is made a member of its group."""
        return [*(["node group"] if system.group(NODE) is None else []),
                *(["node user"] if system.user(NODE) is None else [])]


class InstallationRoot(Step):
    """The installation root, root's own with ROOT_MODE and a btrfs
    subvolume where its parent filesystem is btrfs: made while preparing,
    since a transaction plans and begins inside it (engine check_root)."""

    title = "Installation root"

    def __init__(self, context: Context):
        self.context = context

    def wrong(self, system: System) -> bool:
        """Whether the installation root is missing, or not root's with ROOT_MODE."""
        found = system.stat(self.context.installation.root)
        return not (found and found.is_dir and (found.uid, found.gid, found.mode) == (0, 0, deploy.ROOT_MODE))

    def check(self, system: System) -> Check:
        top = self.context.installation.root
        if system.stat(top) is None:
            return Check(False, f"{top} does not exist")
        if self.wrong(system):
            return Check(False, f"to fix: {top} (root, {deploy.ROOT_MODE:04o})")
        return Check(True, f"{top}, owned by root")

    def actions(self, system: System) -> list[Action]:
        # The installation root is the subvolume; runtime/ and the gateway's
        # access/ are plain directories inside it.
        top = self.context.installation.root
        actions = []
        if system.stat(top) is None:
            if system.stat(top.parent) is None:
                actions.append(Command(("mkdir", "-p", str(top.parent)), root=True))
            if system.fstype(top.parent) == "btrfs":
                actions.append(Command(("btrfs", "subvolume", "create", str(top)), root=True))
            else:
                actions.append(Command(("mkdir", str(top)), root=True))
        return [*actions, Command(("install", "-d", "-o", "root", "-g", "root", "-m", f"{deploy.ROOT_MODE:05o}",
                                   str(top)), root=True)]


def music_state(context: Context, system: System) -> bool:
    """Whether the host has Music Assistant's state: it has music:, or an
    earlier version that had it left runtime/music, as found or as the
    installed manifest records it. Before the owner logs in again for the
    thermite group, runtime/ hides it, and only the record can say."""
    if music.enabled(context.host):
        return True
    path = context.layout.music
    if system.stat(path) is not None:
        return True
    try:
        installed = manifest.installed(system, context.installation.root)
    except manifest.ManifestError:
        return False
    return installed is not None and any(item["path"] == str(path) for item in installed["items"])


class StateDirectories(Step):
    title = "Service state directories"
    #: What the details call the directories once made.
    made = "service directories"

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
        wrong = [path for _, path in self.wrong(system)]
        # Until the owner logs in again for the thermite group, runtime/
        # (0750) hides what fix() made: stat finds none of it.
        if self.applied and self.context.host["gid"] not in system.process_groups():
            return Check(True, f"{self.made} installed for {NAME}; log in again for group access")
        if wrong:
            return Check(False, "to create or fix: " + ", ".join(str(path.relative_to(self.top)) for path in wrong))
        return Check(True, self.found())

    @property
    def top(self) -> Path:
        """What the details name the directories relative to."""
        return self.context.installation.root

    def found(self) -> str:
        count = sum(len(paths) for _, paths in self.directories())
        return f"the {count} runtime directories in {self.top}, owned by {NAME}"

    def actions(self, system: System) -> list[Action]:
        return self.fix(system)

    def fix(self, system: System) -> list[Action]:
        self.applied = True
        host = self.context.host
        by_mode: dict[int, list[Path]] = {}
        for mode, path in self.wrong(system):
            by_mode.setdefault(mode, []).append(path)
        # GNU install preserves existing directory set-ID bits with four-digit
        # modes. The extra leading zero requests the exact mode, including
        # clearing inherited setgid on service-private directories.
        return [Command(("install", "-d", "-o", str(host["uid"]), "-g", str(host["gid"]),
                         "-m", f"{mode:05o}", *map(str, paths)), root=True) for mode, paths in by_mode.items()]

    def desired(self, system: System) -> list[manifest.Item]:
        # The trees a version may change; logs and the old tar backups stay
        # where they are, whichever version runs.
        # So does Music Assistant's, once the host has no music: an item
        # the desired set drops would be deleted.
        layout = self.context.layout
        left = not music.enabled(self.context.host) and music_state(self.context, system)
        return [*(manifest.Item(path, "tree") for path in (layout.atv_venv, layout.atv_secrets, layout.secrets)),
                *(manifest.Item(path, "shared") for path in (layout.logs, layout.backups, *([layout.music] if left else [])))]


class MetricsDirectories(StateDirectories):
    title = "Monitoring directories"
    made = "metrics directories"

    def directories(self) -> list[tuple[int, list[Path]]]:
        return self.context.layout.metrics_directories()

    @property
    def top(self) -> Path:
        return self.context.layout.root

    def found(self) -> str:
        return f"{', '.join(str(p.relative_to(self.top)) for _, paths in self.directories() for p in paths)}, owned by {NAME}"

    def unreadable(self, system: System) -> list[Path]:
        """Textfiles a release before node_exporter's own account left for the
        state's user and group alone. A running service writes its own again
        at once; one that no longer runs here, and the deploy's until the
        next deploy, would stay unreadable and fail every scrape."""
        directory = self.context.layout.textfile
        found = []
        for name in system.directory_entries(directory) or []:
            info = system.stat(directory / name)
            if name.endswith(".prom") and info is not None and not info.is_dir and not info.mode & 0o004:
                found.append(directory / name)
        return found

    def check(self, system: System) -> Check:
        check, stale = super().check(system), self.unreadable(system)
        if check.done and stale:
            return Check(False, "to remove, unreadable to node_exporter: "
                         + ", ".join(str(path.relative_to(self.top)) for path in stale))
        return check

    def actions(self, system: System) -> list[Action]:
        # Removed, not made readable: root changes no mode through a name the
        # service account could have replaced. Their writers write them again.
        stale = self.unreadable(system)
        return [*self.fix(system), *([Command(("rm", "-f", "--", *map(str, stale)), root=True)] if stale else [])]

    def desired(self, system: System) -> list[manifest.Item]:
        # Prometheus' samples are copied aside only when its image changes:
        # up to the retention size, and only a new version may rewrite them.
        layout = self.context.layout
        return [manifest.Item(layout.prometheus_data, "shared-pinned", image="prometheus",
                              value=self.context.versions["prometheus"]["image"]["digest"]),
                *(manifest.Item(path, "tree") for path in (layout.grafana_data, layout.monitoring_secrets)),
                manifest.Item(layout.textfile, "shared")]


class MusicDirectories(StateDirectories):
    """Music Assistant's state, on a host with music: (StateDirectories
    keeps what an earlier version left once it has none)."""
    title = "Music Assistant's state directory"
    made = "Music Assistant's state directory"

    def directories(self) -> list[tuple[int, list[Path]]]:
        return self.context.layout.music_directories()

    def found(self) -> str:
        return f"{self.context.layout.music}, owned by {NAME}"

    def desired(self, system: System) -> list[manifest.Item]:
        # Copied aside only when Music Assistant's image changes: a new
        # version migrates its library and settings, and has no way back.
        return [manifest.Item(self.context.layout.music, "shared-pinned", image="music_assistant",
                              value=self.context.versions["music_assistant"]["image"]["digest"])]


class BootTest(Step):
    title = "Configuration boot check"

    def __init__(self, context: Context):
        self.context = context

    def check(self, system: System) -> Check:
        digest = self.context.conf_digest
        if system.stat(deploy.passed_dir(self.context.var) / digest):
            return Check(True, f"conf/ {digest[:12]} passed on this host")
        return Check(False, f"conf/ {digest[:12]} has not passed here; the first run fetches the pinned runtime, "
                            "about 180 MB, and warms its test cache. Allow a few minutes; unchanged configuration skips this check next time")

    def actions(self, system: System) -> list[Action]:
        argv = application(self.context, "test", "boot")
        if self.context.prepared is not None:
            argv += ("--conf", str(self.context.configuration / "conf"),
                     "--versions", str(self.context.configuration / "versions.json"))
        return [Command(argv)]


class NativeListeners(Step):
    title = "Native loopback listeners"

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
        PROGRAM = self.context.program
        running = system.unit("thermite-openhab.service").active not in ("inactive", "failed", "unknown")
        actions: list[Action] = []
        if running:
            actions.append(Command(("systemctl", "stop", "thermite-openhab.service"), root=True))
        actions.append(Command(("python3", str(PROGRAM / "lib/oh_ports.py"),
                                str(self.context.layout.userdata), "--http-port", str(self.context.host["http_port"]),
                                "--uid", str(self.context.host["uid"]), "--gid", str(self.context.host["gid"]),
                                "--console-password", str(oh_ports.CONSOLE_PASSWORD)), root=True))
        if running:
            actions.append(Command(("systemctl", "start", "thermite-openhab.service"), root=True))
        return actions


class PairingLogging(Step):
    title = "Pairing log protection"

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
        if data is not None and selector is not None and oh_logging.selected_default(selector) and oh_logging.configured(data):
            return Check(True, "native logger filters drop pairing command payloads; the log files are group-readable "
                               "and openhab.log also reaches the journal")
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
        running = system.unit("thermite-openhab.service").active not in ("inactive", "failed", "unknown")
        if running:
            actions.append(Command(("systemctl", "stop", "thermite-openhab.service"), root=True))
        PROGRAM = self.context.program
        actions.append(Command(("python3", str(PROGRAM / "lib" / "oh_logging.py"),
                                str(self.context.layout.userdata), "--http-port", str(self.context.host["http_port"]),
                                "--template", str(self.template), "--uid", str(self.context.host["uid"]),
                                "--gid", str(self.context.host["gid"])), root=True))
        if running:
            actions.append(Command(("systemctl", "start", "thermite-openhab.service"), root=True))
        return actions


class Deploy(Step):
    title = "Deploy this commit"
    #: What the deploy history calls it: install, upgrade or rollback.
    how = "install"

    def __init__(self, context: Context):
        self.context = context
        self.applied = False

    def pairs(self) -> bool:
        """Whether this commit's conf/ pairs a device: its command payloads
        hold credentials, which the native log must not take."""
        prepared = self.context.prepared
        conf = (deploy.read_tree(self.context.area / "conf") if prepared is None else
                {name.removeprefix("conf/"): blob for name, blob in prepared.items() if name.startswith("conf/")})
        return oh_logging.pairing_conf(conf)

    def check(self, system: System) -> Check:
        head, layout = self.context.head, self.context.layout
        if self.applied and _opaque(system, self.context, layout.record):
            return Check(True, f"{head[:12]} deployed by the refreshed operator group")
        try:
            record = json.loads(system.read_bytes(layout.record) or b"{}")
        except ValueError:
            record = {}
        if not isinstance(record, dict):
            record = {}
        commit = record.get("commit")
        # A deploy made before the metrics directories existed installed no metrics/.
        directories = all((found := system.stat(path)) is not None and found.is_dir
                          for path in (layout.metrics, layout.exporter_app))
        if commit == head and directories and "metrics" not in record:
            return Check(False, f"{head[:12]} is deployed, but without metrics/: the metrics directories are new")
        if commit == head:
            if self.context.application is not None and record.get("application") != self.context.application:
                return Check(False, f"{head[:12]} needs its pinned application deployed")
            if record.get("conf_digest") != self.context.conf_digest:
                return Check(False, f"{head[:12]} needs its generated configuration deployed")
            return Check(True, f"{head[:12]} is deployed")
        return Check(False, f"deployed: {commit[:12] if commit else 'nothing yet'}; HEAD is {head[:12]}")

    def actions(self, system: System) -> list[Action]:
        self.applied = True
        context = self.context
        # Installation checks the selected configuration just as the packaged
        # application does. The full unit suite is a developer/release gate;
        # repeating it here delays deployment and can expire sudo credentials.
        checks = ([Command(application(self.context, *args)) for args in
                  (("generate", "--check"), ("validate",), ("lint",))]
                  if self.context.prepared is None else [])
        # The owner writes the commit's files through the thermite group.
        # Root is asked only for what the owner cannot do: before the
        # write, that the native log drops pairing payloads; after it, the
        # written paths handed to the service account. Both are commands of
        # the approved run, as every root command of the transaction is.
        helper = ("/usr/bin/python3", str(context.program / "lib/oh_service.py"), str(context.layout.root),
                  "--uid", str(context.host["uid"]), "--gid", str(context.host["gid"]), "--deployment")
        guard = [Command((*helper, "--check-pairing-logging"), root=True)] if self.pairs() else []
        deployment = operator_command(context, system, write(context, "--config", str(context.area), "deploy",
                                                             context.head, self.how))
        return [*checks, *guard, deployment, Command(helper, root=True)]

    def desired(self, system: System) -> list[manifest.Item]:
        # The deploy's records are files, so that its history (shared, as
        # all logs are) can stay beside them.
        layout = self.context.layout
        return [*(manifest.Item(path, "tree") for path in (layout.conf, layout.metrics, layout.exporter_app, layout.atv_app)),
                *(manifest.Item(path, "file") for path in (layout.record, layout.pending)),
                manifest.Item(layout.history, "shared")]


class Units(Step):
    title = "Service units"

    def __init__(self, context: Context):
        self.context = context

    def stale(self, system: System, names: tuple[str, ...] | None = None) -> list[str]:
        """Units to install, and ones this host no longer has to remove."""
        return [name for name, target in sorted(deploy.every_unit(self.context.host).items())
                if (names is None or name in names)
                and system.read_bytes(target) != self.context.unit(name)]

    def check(self, system: System) -> Check:
        owned = ownership.inspect(self.context, system)
        if owned.problems:
            return Check(False, "", "; ".join(owned.problems))
        stale = self.stale(system)
        if stale:
            return Check(False, "to install: " + ", ".join(stale))
        return Check(True, f"the {len(deploy.host_units(self.context.host))} installed unit files match deploy/")

    def actions(self, system: System) -> list[Action]:
        by_directory: dict[Path, list[str]] = {}
        units = deploy.every_unit(self.context.host)
        stale = self.stale(system)
        removed = [str(units[name]) for name in stale if self.context.unit(name) is None]
        # A share's mount unit is installed under its mount point's name.
        renamed = [name for name in stale if self.context.unit(name) is not None and Path(name).name != units[name].name]
        for name in stale:
            if self.context.unit(name) is not None and name not in renamed:
                by_directory.setdefault(units[name].parent, []).append(name)
        missing = [directory for directory in dict.fromkeys([*by_directory, *(units[name].parent for name in renamed)])
                   if system.stat(directory) is None]

        def source(name: str) -> str:
            return str(self.context.configuration / name) if self.context.prepared is not None else name
        return [*([Command(("rm", "-f", "--", *removed), root=True)] if removed else []),
                *([Command(("install", "-d", "-o", "0", "-g", "0", "-m", "0755", *map(str, missing)), root=True)] if missing else []),
                *(Command(("install", "-m", "0644", "-t", str(directory), *map(source, names)), root=True)
                  for directory, names in by_directory.items()),
                *(Command(("install", "-m", "0644", "-T", source(name), str(units[name])), root=True) for name in renamed),
                *([Command(("systemctl", "daemon-reload"), root=True)] if stale else [])]

    def desired(self, system: System) -> list[manifest.Item]:
        items = []
        for source, target in sorted(deploy.host_units(self.context.host).items()):
            items.append(manifest.unit(target))
            # Quadlets are enabled by their generator, and drop-ins not at all.
            if target.parent == manifest.UNITS:
                items += manifest.wants_links(self.context.unit(source).decode(), target.name)
        return items


class Services(Step):
    title = "Runtime and Apple TV helper"
    restarts = RESTARTS

    def __init__(self, context: Context, *, restart_runtime: bool = False):
        self.context = context
        self.force_runtime = restart_runtime
        self.runtime_restarted = False

    def plan(self, system: System) -> tuple[list[str], list[str]]:
        """Units to start, and units to restart (their file changed after
        they started)."""
        start, restart = [], []
        for name, unit in self.restarts.items():
            state = system.unit(unit)
            if state.active != "active":
                start.append(unit)
                continue
            if self.force_runtime and unit == oh_units.RUNTIME and not self.runtime_restarted:
                restart.append(unit)
                continue
            installed = system.stat(deploy.INSTALLED[name])
            support = system.stat(deploy.INSTALLED[deploy.CONTAINER_DROPINS[unit]]) if unit in deploy.CONTAINER_DROPINS else None
            confinement = system.stat(oh_apparmor.CONFIG) if unit == "thermite-node-exporter.service" else None
            if (confinement is not None and state.since is None
                    or state.since is not None and any(info is not None and info.mtime > state.since
                                                       for info in (installed, support, confinement))):
                restart.append(unit)
        return start, restart

    def check(self, system: System) -> Check:
        start, restart = self.plan(system)
        if not (start or restart):
            return Check(True, "running on the installed units")
        parts = []
        if start:
            parts.append("to start: " + ", ".join(start))
        if restart:
            reason = ("explicit fresh runtime requested" if self.force_runtime and oh_units.RUNTIME in restart
                      else "its unit or confinement changed since it started")
            parts.append("to restart, " + reason + ": " + ", ".join(restart))
        if "thermite-openhab.service" in start:
            parts.append("stop any live test instance first (thermite sandbox stop where it runs): "
                         "two live runtimes would fight over the devices")
        return Check(False, "; ".join(parts))

    def actions(self, system: System) -> list[Action]:
        start, restart = self.plan(system)
        actions = self.prefetch(system, [*start, *restart])
        if start:
            actions.append(Command(("systemctl", "start", *start), root=True))
        if restart:
            actions.append(Command(("systemctl", "restart", *restart), root=True))
        if self.force_runtime and oh_units.RUNTIME in (*start, *restart) and not self.runtime_restarted:
            actions.append(Task("record the requested fresh runtime start", self.record_runtime_restart))
        return actions

    def record_runtime_restart(self, system: System) -> int:
        # This task follows the successful start/restart command. A failed or
        # cancelled command never reaches it; check() still requires an active unit.
        self.runtime_restarted = True
        return 0

    def prefetch(self, system: System, units: list[str]) -> list[Action]:
        """Download sequentially before systemd starts any service's timer."""
        names = {"thermite-openhab.service": "openhab", "thermite-atv.service": "atv_helper",
                 **{service.unit: service.image for service in metrics.services(self.context.host)},
                 oh_units.MUSIC: "music_assistant"}
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
    title = "Monitoring"
    restarts = METRICS_RESTARTS


class MusicServices(Services):
    """Music Assistant, on a host with music:; each start pins its listeners."""
    title = "Music Assistant"
    restarts = MUSIC_RESTARTS


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
    info = system.unit_file(path)
    if info is None or (info.uid, info.gid, info.mode) != (context.host["uid"], context.host["gid"], 0o660):
        return False
    try:
        value = json.loads(info.content)
        return (isinstance(value, dict) and all(type(value.get(key)) is int for key in ('version', 'uid', 'gid'))
                and value == {
            "version": oh_service.RECEIPT_VERSION, "policy": oh_service.POLICY,
            "uid": context.host["uid"], "gid": context.host["gid"]})
    except ValueError:
        return False


def installation_owned(context: Context, system: System) -> bool:
    """The installation root holds nothing but its two fixed children and
    the install engine's transactions."""
    top = context.installation.root
    if system.stat(top) is None:
        return not system.lexists(top)
    names = system.directory_entries(top)
    return names is not None and set(names) <= {deploy.RUNTIME, deploy.ACCESS, TRANSACTIONS}


def state_owned(context: Context, system: System) -> bool:
    """Existing data needs positive ownership; empty setup can be resumed."""
    root = context.layout.root
    if system.stat(root) is None:
        return not system.lexists(root)
    if service_receipt(context, system) or ownership.inspect(context, system).runtime_bound:
        return True
    allowed = {path for _, paths in (*context.layout.directories(), *context.layout.metrics_directories(),
                                     *context.layout.music_directories()) for path in paths}
    pending = [root]
    while pending:
        directory = pending.pop()
        names = system.directory_entries(directory)
        if names is None:
            return False
        for name in names:
            child = directory / name
            found = system.stat(child)
            if (child not in allowed or found is None or not found.is_dir
                    or (found.uid, found.gid) != (context.host['uid'], context.host['gid'])
                    or not system.safe_directory(child)):
                return False
            pending.append(child)
    return True


#: The installed files that define openHAB's own container: a change to them
#: needs openHAB's offline preparation.
RUNTIME_FILES = (deploy.QUADLET, deploy.CONTAINER_DROPINS[oh_units.RUNTIME])


class NativePreparation(Step):
    """Offline openHAB preparation, in one of two scopes. Custody (a missing
    receipt, account or directory) repairs the whole state tree, which every
    service writes. Otherwise only openHAB's own trees are repaired. Either
    way Halt has stopped all of Thermite first."""
    title = "Native state and ownership"

    def __init__(self, context: Context):
        self.context = context
        self.applied = False

    def custody(self, system: System) -> bool:
        return bool(not service_receipt(self.context, system)
                    or StateDirectories(self.context).wrong(system) or MetricsDirectories(self.context).wrong(system)
                    or music.enabled(self.context.host) and MusicDirectories(self.context).wrong(system)
                    or Account(self.context).missing(system))

    def check(self, system: System) -> Check:
        if self.applied:
            return Check(True, "offline preparation, native guards and ownership repair succeeded")
        if self.custody(system):
            return Check(False, "repair custody, prepare native userdata without root, then verify guards and repair final modes")
        start, restart = Services(self.context).plan(system)
        needed = (oh_units.RUNTIME in (*start, *restart)
                  or Units(self.context).stale(system, RUNTIME_FILES)
                  or not PairingLogging(self.context).check(system).done
                  or not NativeListeners(self.context).check(system).done)
        return Check(not needed, "service ownership and native preparation are recorded" if not needed else
                     "prepare openHAB's userdata without root, then verify guards and repair its own files")

    def actions(self, system: System) -> list[Action]:
        PROGRAM = self.context.program
        custody = self.custody(system)
        self.applied = True
        host, root = self.context.host, self.context.layout.root
        helper = ("/usr/bin/python3", str(PROGRAM / "lib/oh_service.py"), str(root),
                  "--uid", str(host["uid"]), "--gid", str(host["gid"]))
        # Only a full repair may record the receipt; openHAB's own repair keeps it.
        repair, final = (helper, (*helper, "--receipt")) if custody else ((*helper, "--runtime"),) * 2
        userdata = str(self.context.layout.userdata)
        return [Command(repair, root=True),
                Command(deploy.prepare_openhab_argv(self.context.versions, host), root=True),
                Command(("python3", str(PROGRAM / "lib/oh_logging.py"), userdata, "--http-port", str(host["http_port"])), root=True),
                # The listeners, and the console's own password in place of the
                # distribution's: every transaction runs this, so the first
                # upgrade of an older install makes the password.
                Command(("python3", str(PROGRAM / "lib/oh_ports.py"), userdata, "--http-port", str(host["http_port"]),
                         "--uid", str(host["uid"]), "--gid", str(host["gid"]),
                         "--console-password", str(oh_ports.CONSOLE_PASSWORD)), root=True),
                Command((*helper, "--check-pairing-logging"), root=True),
                Command(final, root=True)]

    def desired(self, system: System) -> list[manifest.Item]:
        # The update tool migrates userdata in place: a copy is set aside.
        # The receipt records a full ownership repair of the state trees as
        # they are, so it goes where they go: a rollback puts back the one
        # that went with the trees it reinstates, and takes away a first
        # install's, which would otherwise be all that is left of it under
        # runtime/ and stop the next install.
        # The console password goes with the userdata that holds its digest:
        # a revert to a version without one takes the file away as it puts
        # that version's userdata back.
        return [manifest.Item(self.context.layout.userdata, "tree"), manifest.Item(self.context.layout.addons, "tree"),
                manifest.Item(self.context.layout.root / oh_service.RECEIPT, "file"),
                manifest.Item(oh_ports.CONSOLE_PASSWORD, "file")]


def addon_uids(context: Context) -> list[str]:
    """The add-ons this version's conf/services/addons.cfg lists, as
    versions.json names them (binding-http)."""
    import oh_sandbox as sandbox
    name = "conf/services/addons.cfg"
    if context.prepared is not None:
        text = context.prepared[name].data.decode("utf-8") if name in context.prepared else ""
    else:
        text = (context.area / name).read_text(encoding="utf-8") if (context.area / name).is_file() else ""
    return sandbox.addon_uids(sandbox.parse_addons_cfg(text))


def addon_store(context: Context) -> Path:
    """The operator's cache of fetched add-on files: the boot check's."""
    return context.area / "build" / "sandbox" / "downloads" / "addons"


def binding_repairs(context: Context) -> list[oh_addons.Repair]:
    """The binding repairs this version's add-ons run with (lib/oh_addons.py)."""
    return oh_addons.repairs(context.versions, addon_uids(context))


def addon_pins(context: Context) -> dict[str, str]:
    """What the installed manifest records of this version's add-on files:
    one digest over the pins of every file its add-ons are installed from
    and the repaired bundles beside them, and each repair by name with the
    content of its bundle. A version whose add-ons are not all pinned, or
    whose repair is for another openHAB, is never installed (AddonFiles
    refuses it, with what to do), so it has none to record."""
    try:
        wanted = binding_repairs(context)
        return {"addons": oh_addons.digest(oh_addons.pinned(context.versions, addon_uids(context)), wanted),
                **{repair.pin: "sha256:" + repair.content for repair in wanted}}
    except oh_addons.AddonError:
        return {"addons": "unpinned"}


class AddonFiles(Step):
    """Every file the conf's add-ons are installed from, in the operator's
    cache and verified against versions.json, while Thermite still runs: a
    file that cannot be fetched or is not the pinned one refuses the
    transaction before anything stops."""
    title = "Add-on files"

    def __init__(self, context: Context, download=None):
        self.context = context
        self.download = download

    def missing(self, system: System) -> list[str]:
        # The operator's own cache, read whole: a file of the right size
        # and other content is as missing as one that is not there.
        store = addon_store(self.context)
        return [path for path, pin in oh_addons.pinned(self.context.versions, addon_uids(self.context)).items()
                if not oh_addons.stored(store, path, pin)]

    def check(self, system: System) -> Check:
        try:
            missing = self.missing(system)
            wanted = binding_repairs(self.context)
        except oh_addons.AddonError as exc:
            return Check(False, "", str(exc))
        count = len(oh_addons.pinned(self.context.versions, addon_uids(self.context)))
        if missing:
            return Check(False, f"to fetch and verify: {len(missing)} of the {count} pinned add-on files")
        repaired = "".join(f"; {repair.bundle} runs with the {repair.title}" for repair in wanted)
        return Check(True, f"the {count} pinned files of {len(addon_uids(self.context))} add-ons are fetched{repaired}")

    def actions(self, system: System) -> list[Action]:
        return [Task("fetch each pinned add-on file that is not here yet, and verify every one", self.fetch)]

    def fetch(self, system: System) -> int:
        import oh_sandbox as sandbox
        import urllib.error
        try:
            oh_addons.ensure(self.context.versions, addon_uids(self.context), addon_store(self.context),
                             self.download or sandbox.download, log=lambda line: system.say("   " + line))
        except (oh_addons.AddonError, sandbox.SandboxError, urllib.error.URLError, OSError) as exc:
            system.say(f"   An add-on file could not be fetched and verified: {exc}. Nothing was changed. With no "
                       "network here, copy build/sandbox/downloads/addons from a site that has them and run the "
                       "command again: every file is checked against versions.json wherever it came from.")
            return 1
        return 0


class AddonRepository(Step):
    """The pinned add-on files and the repaired bundles of the bindings
    that need one, in userdata's own repository; Karaf's resolver pointed
    at it alone and its overrides at those bundles; and an OSGi cache that
    holds another build of a repaired binding cleared (lib/oh_addons.py).
    After openHAB's offline preparation, whose update replaces the
    resolver's settings and the overrides with the distribution's, and
    before anything starts. All of it is inside userdata, so it is set
    aside and put back with it."""
    title = "Add-on repository"
    always = True

    def __init__(self, context: Context):
        self.context = context

    def helper(self, *options: str) -> tuple[str, ...]:
        host = self.context.host
        return ("python3", str(self.context.program / "lib/oh_addons.py"), str(self.context.layout.userdata),
                "--versions", str(self.context.configuration / "versions.json"),
                "--store", str(addon_store(self.context)),
                "--uid", str(host["uid"]), "--gid", str(host["gid"]), *options, *addon_uids(self.context))

    def check(self, system: System) -> Check:
        try:
            repaired = "".join(f", and the {repair.title}" for repair in binding_repairs(self.context))
        except oh_addons.AddonError:
            # AddonFiles has refused this version, with what to do.
            repaired = ""
        return Check(False, f"the pinned files of {len(addon_uids(self.context))} add-ons{repaired}, installed from "
                            "the verified cache while openHAB is stopped")

    def actions(self, system: System) -> list[Action]:
        return [Command(self.helper(), root=True, show_output=True)]


class BindingRepairs(Step):
    """The started runtime installed the repaired bundle of each binding
    that needs one, and no other build of it: read from its OSGi cache
    once the add-ons are installed. A runtime that runs an unrepaired
    binding is not committed; the helper's words say what that means."""
    title = "Binding repairs"
    always = True

    def __init__(self, context: Context):
        self.context = context

    def check(self, system: System) -> Check:
        return Check(False, "openHAB installed " + " and ".join(
            f"the {repair.title} ({repair.bundle} {repair.patched})" for repair in binding_repairs(self.context)))

    def actions(self, system: System) -> list[Action]:
        return [Command(AddonRepository(self.context).helper("--verify"), root=True, show_output=True)]


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
    PROGRAM = context.program
    host = context.host
    return Command(("python3", str(PROGRAM / "lib/oh_install/secrets.py"), str(context.layout.root),
                    "--name", target.name, "--source", str(context.area / source),
                    "--uid", str(host["uid"]), "--gid", str(host["gid"]),
                    *(("--preserve-existing", "--generate-if-missing") if preserve else ())), root=True)


class MetricsToken(Step):
    title = "Monitoring API token"

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
            actions.append(Task("configure private internal runtime access", token.save))
        actions.append(_install_secret(self.context, self.context.token_file, self.target))
        return actions


class GrafanaPassword(Step):
    title = "Grafana administrator password"

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


class ShareLogins(Step):
    """The owner's private copy of each SMB share's login (oh_shares), in the
    site's var/ beside the API token: asked for once, while Thermite still
    runs, and checked before anything stops. ShareCredentials installs it."""

    title = "Network share logins"

    def __init__(self, context: Context):
        self.context = context

    def shares(self) -> list[shares.Share]:
        return [share for _, share in sorted(shares.settings(self.context.host).items()) if share.type == "cifs"]

    def problem(self, system: System, share: shares.Share) -> str | None:
        """Why its copy is not usable; "missing" when there is none yet."""
        from oh_install import secrets
        path = self.context.share_login(share.name)
        try:
            text = system.read_private(self.context.area / path)
        except FileNotFoundError:
            return "missing"
        except (OSError, ValueError):
            return f"{path} must be a regular file of yours with mode 0600"
        try:
            secrets.share_login(text.encode())
        except secrets.SecretError as exc:
            return f"{path}: {exc}"
        return None

    def check(self, system: System) -> Check:
        found = {share.name: self.problem(system, share) for share in self.shares()}
        wrong = [problem for problem in found.values() if problem not in (None, "missing")]
        if wrong:
            return Check(False, "", "; ".join(wrong))
        missing = [str(self.context.share_login(name)) for name, problem in found.items() if problem == "missing"]
        if missing:
            return Check(False, "to ask for: " + ", ".join(missing))
        return Check(True, f"{len(found)} in {self.context.token_file.parent}" if found else "no SMB share")

    def actions(self, system: System) -> list[Action]:
        return [Task(f"ask for the login of {share.what} and keep it in {self.context.share_login(share.name)} "
                     "(or write username= and password= lines there yourself, mode 0600)",
                     lambda system, share=share: self.ask(system, share), interactive=True)
                for share in self.shares() if self.problem(system, share) == "missing"]

    def ask(self, system: System, share: shares.Share) -> int:
        from oh_install import secrets
        path = self.context.share_login(share.name)
        username = system.ask(f"   User name for {share.what}: ").strip()
        text = f"username={username}\npassword={system.secret(f'   Password for {share.what}: ')}\n"
        try:
            secrets.share_login(text.encode())
        except secrets.SecretError:
            system.say(f"   no usable login given; write username= and password= lines to {path} (mode 0600), "
                       "then rerun")
            return 1
        try:
            system.write_private(self.context.area / path, text)
        except (OSError, ValueError):
            system.say(f"   could not write {path}; check the site's var/ custody, then rerun")
            return 1
        return 0


class ShareCredentials(Step):
    """Each SMB share's login, from the owner's private copy (ShareLogins) to
    root's alone, where its mount reads it (oh_shares.CREDENTIALS). The
    administrative helper installs it with a random salt, kept while the
    login stays the same, so that the digest the installed manifest records
    checks no guessed password. A copy changed since the install is newer."""

    title = "Network share credentials"

    def __init__(self, context: Context):
        self.context = context

    def shares(self) -> list[shares.Share]:
        return [share for _, share in sorted(shares.settings(self.context.host).items()) if share.type == "cifs"]

    def stale(self, system: System) -> list[shares.Share]:
        found = []
        for share in self.shares():
            target = system.stat(Path(share.credentials))
            source = system.stat(self.context.area / self.context.share_login(share.name))
            if (target is None or (target.uid, target.gid, target.mode) != (0, 0, 0o600)
                    or source is None or source.mtime > target.mtime):
                found.append(share)
        return found

    def check(self, system: System) -> Check:
        stale = self.stale(system)
        if stale:
            return Check(False, "to install: " + ", ".join(str(share.credentials) for share in stale))
        count = len(self.shares())
        return Check(True, f"{count} installed, root's alone (0600)" if count else "no SMB share")

    def actions(self, system: System) -> list[Action]:
        return [Command(("python3", str(self.context.program / "lib/oh_install/secrets.py"), "--share", share.name,
                         "--source", str(self.context.area / self.context.share_login(share.name))), root=True)
                for share in self.stale(system)]

    def desired(self, system: System) -> list[manifest.Item]:
        return [manifest.Item(Path(share.credentials), "file") for share in self.shares()]


class MetricsAnswer(ReadinessStep):
    title = "Monitoring readiness"

    def __init__(self, context: Context):
        super().__init__()
        self.context = context

    def health(self, system: System) -> deploy.MetricsHealth:
        return system.metrics_health(self.context.host, self.context.address or self.context.hostname)

    def check(self, system: System) -> Check:
        health = self.health(system)
        if health.prometheus_ok and health.targets_ok and health.grafana_ok and health.systemd is True:
            return Check(True, health.describe())
        down = f"; down: {', '.join(health.down)}" if health.down else ""
        return Check(False, f"{health.describe()}{down}: waiting for service readiness and a successful "
                            "scrape of every target and node_exporter's systemd collector")

    def actions(self, system: System) -> list[Action]:
        return [Task(f"wait up to {METRICS_WAIT // 60} minutes for Prometheus' targets, a successful "
                     f"node_exporter systemd collector scrape, and "
                     f"{self.context.grafana_url}api/health", self.wait)]

    def wait(self, system: System) -> int:
        self.begin_wait()
        deadline = system.clock() + METRICS_WAIT
        while system.clock() < deadline:
            found = self.check(system)
            if found.done:
                return self.ready(found)
            system.sleep(POLL)
        health = self.health(system)
        if health.down:
            system.say(f"   still down: {', '.join(health.down)}")
        if health.systemd is not True:
            system.say("   still waiting for Prometheus to report a successful node_exporter systemd collector scrape")
        system.say("   `sudo journalctl -u thermite-prometheus.service` (or the unit of what is down) shows why")
        return 1


class MusicAnswer(ReadinessStep):
    """Music Assistant running, its API on loopback alone, on a host with music:."""
    title = "Music Assistant readiness"

    def __init__(self, context: Context):
        super().__init__()
        self.context = context

    def check(self, system: System) -> Check:
        health = system.music_health(self.context.host)
        if health.ok:
            return Check(True, health.describe() + ", its API on loopback alone")
        return Check(False, f"{health.describe()}: waiting for it to run, its API on loopback alone")

    def actions(self, system: System) -> list[Action]:
        return [Task(f"wait up to {MUSIC_WAIT // 60} minutes for Music Assistant to run, its API on loopback alone",
                     self.wait)]

    def wait(self, system: System) -> int:
        self.begin_wait()
        deadline = system.clock() + MUSIC_WAIT
        while system.clock() < deadline:
            found = self.check(system)
            if found.done:
                return self.ready(found)
            progress.update(found.detail)
            system.sleep(POLL)
        health = system.music_health(self.context.host)
        if health.addresses and not health.loopback:
            system.say("   its API listens beyond loopback, where Music Assistant falls back when it cannot listen "
                       "where its settings say; the native boundary still refuses the LAN there")
        system.say(f"   `sudo journalctl -u {oh_units.MUSIC}` shows why")
        return 1


class Answers(ReadinessStep):
    title = "Runtime readiness"

    def __init__(self, context: Context, native: NativeDeadline | None = None):
        super().__init__()
        self.context = context
        self.native = native or NativeDeadline.of(context)

    def check(self, system: System) -> Check:
        url = self.context.url
        version = system.openhab_version(url)
        if version:
            return Check(True, f"openHAB {version} at {url}")
        return Check(False, f"REST at {url} is not ready; startup and add-on loading may take several minutes")

    def actions(self, system: System) -> list[Action]:
        return [Task(f"wait for {self.context.url}/rest/ within the {self.native.describe()} native readiness allowance "
                     "this run shares", self.wait)]

    def wait(self, system: System) -> int:
        try:
            return self._wait(system)
        finally:
            self.native.stop(system)

    def _wait(self, system: System) -> int:
        self.begin_wait()
        deadline = self.native.start(system)
        watch = UnitWatch()
        while system.clock() < deadline:
            found = self.check(system)
            if found.done:
                return self.ready(found)
            problem = watch.problem(system, deadline)
            if problem:
                system.say("   " + problem)
                return 1
            progress.update(found.detail)
            system.sleep(max(0, min(POLL, deadline - system.clock())))
        system.say("   still nothing; `sudo journalctl -u thermite-openhab.service` shows why; a slower host can "
                   "rerun with --native-timeout SECONDS")
        return 1


class Token(Step):
    title = "Runtime access"

    def __init__(self, context: Context, native: NativeDeadline | None = None):
        self.context = context
        self.native = native or NativeDeadline.of(context)
        self.validated = None
        self.validation_unavailable = False
        self.imported = False

    def saved(self, system: System) -> str:
        try:
            value = system.read_private(self.context.area / self.context.token_file).strip()
        except FileNotFoundError:
            return ""
        except (OSError, ValueError):
            raise ValueError("Cannot safely read the saved runtime credential; check its private file custody.") from None
        if any(ord(character) < 33 or ord(character) > 126 for character in value):
            raise ValueError("The saved runtime credential is malformed; use --runtime-token-file PATH for explicit recovery.")
        return value

    def startup_remaining(self, system: System, *, deadline: float | None = None) -> float | None:
        """The native startup allowance left, or unknown without a start time."""
        timeout = POLL if deadline is None else max(0.001, min(POLL, deadline - system.clock()))
        unit = system.unit("thermite-openhab.service", timeout=timeout)
        if unit.active != "active" or unit.since is None:
            return None
        age = max(0, system.wall_time() - unit.since)
        return max(0, self.native.budget - age)

    def status(self, system: System, token: str, deadline: float) -> int | None:
        found = system.native_probe(self.context.url, token, "/rest/things?summary=true", deadline)
        if system.clock() >= deadline:
            return 0
        if found.status == 200 and (found.problem or not isinstance(found.value, list)):
            return 0
        return found.status

    @staticmethod
    def transient(status: int | None) -> bool:
        return oh_http.retryable("GET", status)

    def check(self, system: System) -> Check:
        if self.context.runtime_token_file is not None and not self.imported:
            return Check(False, "validate and import the explicitly supplied runtime credential")
        token = self.context.token_file
        try:
            saved = self.saved(system)
        except ValueError as exc:
            return Check(False, "", str(exc))
        if saved != self.validated:
            self.validated = None
        if not saved:
            return Check(False, "reuse this installation's private runtime access, or set it up for a new runtime")
        # Only the exact token validated by this one step can bypass runner's
        # immediate post-action query. NativeReady performs fresh full reads.
        status = 200 if self.validated == saved else self.status(system, saved, system.clock() + POLL)
        if status == 200:
            self.validated = saved
            return Check(True, "private runtime access verified")
        if status in (401, 403):
            return Check(False, f"runtime refuses the credential in {token} (HTTP {status}); "
                         "check authentication readiness before replacing the saved credential")
        return Check(False, f"{token} is saved but runtime authentication is not ready; keep the saved credential")

    def actions(self, system: System) -> list[Action]:
        if self.context.runtime_token_file is not None:
            return [Task("validate and import the explicitly supplied runtime credential", self.save)]
        if self.saved(system):
            return [Task(f"check the saved API token within the {self.native.describe()} native readiness allowance "
                         "before offering replacement", self.wait)]
        return [Task("configure private internal runtime access", self.save)]

    def wait(self, system: System) -> int:
        try:
            return self._wait(system)
        finally:
            self.native.stop(system)

    def _wait(self, system: System) -> int:
        saved = self.saved(system)
        if not saved:
            system.say("   the saved API token is no longer available; nothing was changed")
            return 1
        deadline = self.native.start(system)
        while True:
            if system.clock() >= deadline:
                system.say("   runtime authentication did not become ready within the startup allowance; "
                           "the saved API token was not changed. Check `sudo journalctl -u thermite-openhab.service`, then rerun.")
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
                return self.save(system)
            if not self.transient(status) and status not in (401, 403):
                system.say(f"   runtime refused the saved API token (HTTP {status}); nothing was changed")
                return 1
            left = deadline - system.clock()
            if left <= 0:
                system.say("   runtime authentication did not become ready within the startup allowance; "
                           "the saved API token was not changed. Check `sudo journalctl -u thermite-openhab.service`, then rerun.")
                return 1
            system.sleep(min(POLL, left))

    def save(self, system: System) -> int:
        try:
            return self._save(system)
        finally:
            self.native.stop(system)

    def _save(self, system: System) -> int:
        from oh_install.native_auth import NativeAuthError
        deadline = self.native.start(system)
        if self.context.runtime_token_file is not None:
            source = self.context.runtime_token_file
            if not source.is_absolute():
                source = self.context.area / source
            try:
                token = system.read_private(source).strip()
            except (OSError, ValueError):
                system.say("   cannot read the explicit runtime credential; it must be your private mode-0600 regular file")
                return 1
            problem = self.problem(system, token, deadline=deadline)
            if problem is not None:
                system.say(f"   {problem}; the saved runtime credential was not changed")
                return 1
            try:
                system.import_runtime_access(self.context.area / self.context.runtime_credentials_file,
                                             self.context.area / self.context.token_file, token)
            except (OSError, ValueError):
                self.validated = None
                system.say("   could not save the private runtime credential; check its path and custody")
                return 1
            self.imported = True
            return 0
        while system.clock() < deadline:
            try:
                token = system.configure_runtime_access(self.context.url,
                    self.context.area / self.context.runtime_credentials_file,
                    self.context.area / self.context.token_file, deadline)
                self.validated = token
                return 0
            except NativeAuthError as exc:
                if not exc.retryable:
                    if exc.code in ("existing_users", "credentials") and not self.saved(system):
                        try:
                            system.read_private(self.context.area / self.context.runtime_credentials_file)
                        except FileNotFoundError:
                            return self.recover_installed(system, deadline)
                        except (OSError, ValueError):
                            pass  # An unreadable managed record is not permission to replace it.
                    system.say(f"   {exc}")
                    if exc.code in ("existing_users", "credentials", "retired"):
                        system.say("   Advanced recovery: rerun with --runtime-token-file PATH to import an existing private runtime credential.")
                    return 1
            except (OSError, ValueError):
                system.say("   runtime access setup could not complete safely; check private credential custody and rerun")
                return 1
            remaining = deadline - system.clock()
            if remaining > 0:
                system.sleep(min(POLL, remaining))
        system.say("   runtime access did not become ready within the startup allowance; rerun to resume setup")
        return 1

    def recover_installed(self, system: System, deadline: float) -> int:
        """Reuse the finite installed credential under this run's approval."""
        from oh_install import runner
        system.say("   Reconnecting this configuration directory to the existing runtime account.")
        action = Command(("python3", str(self.context.program / "lib/oh_install/secrets.py"),
                          str(self.context.layout.root), "--name", metrics.OPENHAB_TOKEN,
                          "--source", str(self.context.area / self.context.token_file),
                          "--uid", str(self.context.host["uid"]), "--gid", str(self.context.host["gid"]),
                          "--recover-runtime-token", "--runtime-url", self.context.url), root=True)
        code = runner.perform(action, system, self.context.area)
        if code != 0:
            system.say("   Could not recover usable access from this installation. Restore the site's private var/ backup, "
                       "or rerun with --runtime-token-file PATH to import an existing runtime credential.")
            return code or 1
        token = self.saved(system)
        problem = self.problem(system, token, deadline=deadline)
        if problem is not None:
            system.say(f"   {problem}; the recovered credential was kept for inspection")
            return 1
        return 0

    def problem(self, system: System, token: str, *, deadline: float | None = None) -> str | None:
        """Why token will not do, or None when runtime accepts it."""
        self.validation_unavailable = False
        if not token or any(ord(c) < 33 or ord(c) > 126 for c in token):
            return "That is not a token"
        # openHAB's tokens are oh.<name>.<secret>, and neither part holds a dot.
        if token.count(".") > 2:
            return "The credential file contains more than one token"
        deadline = self.native.start(system) if deadline is None else deadline
        waiting = False
        while system.clock() < deadline:
            status = self.status(system, token, deadline)
            if status == 200:
                self.validated = token
                return None
            if not self.transient(status):
                return f"runtime refused that token (HTTP {status})"
            if not waiting:
                system.say("   native authentication is not ready; retrying this credential without saving it yet")
                waiting = True
            left = deadline - system.clock()
            if left > 0:
                system.sleep(min(POLL, left))
        self.validation_unavailable = True
        return "runtime authentication did not become ready within the startup allowance"


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


class NativeReadiness(ReadinessStep):
    """One snapshot of required add-ons, Things and native metrics, polled
    on the run's shared native deadline."""

    title = "Native add-ons and handlers"
    grace = 60
    settle = 10
    addons_path = "/rest/addons?serviceId=all"
    things_path = "/rest/things?summary=false"
    metrics_path = "/rest/metrics/prometheus"
    #: Whether every Thing must finish initializing and native metrics answer.
    complete = True
    #: Whether unchanged known first-start handler symptoms get one restart.
    recovery = True
    stable = "native initialization is ready; waiting for {settle} seconds of stable readiness after commissioning"

    def __init__(self, context: Context, native: NativeDeadline | None = None):
        super().__init__()
        from oh_sandbox import addon_uids, parse_addons_cfg, read_requested_addons
        self.context = context
        self.native = native or NativeDeadline.of(context)
        requested = (parse_addons_cfg(context.prepared["conf/services/addons.cfg"].data.decode("utf-8"))
                     if context.prepared is not None else read_requested_addons(context.area / "conf"))
        self.required = set(addon_uids(requested))
        self.watch = UnitWatch()
        self.signature = None
        self.since = None
        self.restarted = False
        self.previous_generation = None
        self.problem = None
        self.ready_signature = None
        self.ready_since = None
        self.pending_rows: list[dict] = []
        #: The last snapshot's Things with known first-start symptoms, and
        #: those of them with untyped state channels (untyped_channels).
        self.symptoms: set[str] = set()
        self.untyped: set[str] = set()
        self.last = ""

    @property
    def deadline(self) -> float | None:
        return self.native.deadline

    @deadline.setter
    def deadline(self, value: float | None):
        self.native.deadline = value

    def reset(self):
        self.signature = self.since = None

    def expired(self) -> str:
        """The deadline failure, naming what was still unfinished."""
        rows = self.pending_rows
        shown = ", ".join(f"{row['UID']} {row['statusInfo']['status']}/{row['statusInfo'].get('statusDetail') or 'NONE'}"
                          for row in rows[:8]) + (f" and {len(rows) - 8} more" if len(rows) > 8 else "")
        state = f"; still initializing: {shown}" if rows else f"; last state: {self.last}" if self.last else ""
        return (f"native readiness exceeded the {self.native.describe()} native readiness allowance{state}; "
                "inspect journalctl -u thermite-openhab.service, or give a slower host --native-timeout SECONDS")

    def snapshot(self, system: System) -> tuple[Check, tuple | None]:
        from oh_sandbox import HANDLER_PENDING_DETAILS, installed_addon_uids, thing_readiness, untyped_channels
        deadline = self.native.start(system)
        if system.clock() >= deadline:
            return Check(False, "", self.expired()), None
        token = Token(self.context).saved(system)
        if not token:
            return Check(False, "", "native readiness requires the saved, validated API token; nothing was changed"), None
        if self.previous_generation is not None:
            unit = system.unit("thermite-openhab.service", timeout=max(0.001, deadline - system.clock()))
            if unit.active != "active" or unit.generation is None or unit.generation <= self.previous_generation:
                return Check(False, "waiting for the requested restart to reach a new active service generation"), None

        values = {}
        paths = (self.addons_path, self.things_path, self.metrics_path) if self.complete else (self.addons_path, self.things_path)
        for path in paths:
            found = system.native_probe(self.context.url, token, path, deadline,
                                        json_body=path != self.metrics_path)
            if found.status in (401, 403):
                return Check(False, "", f"authenticated native readiness was refused (HTTP {found.status}); keep the saved credential and check access"), None
            if found.problem == "malformed JSON":
                return Check(False, "", f"native readiness: {path} returned malformed JSON"), None
            if path == self.metrics_path and found.status == 404:
                pass  # The metrics add-on may still be starting; reported below.
            elif oh_http.retryable("GET", found.status):
                return Check(False, f"waiting for a complete native readiness snapshot ({path} unavailable)"), None
            elif found.status != 200:
                return Check(False, "", f"native readiness: {path} answered HTTP {found.status}"), None
            values[path] = found
        if system.clock() >= deadline:
            return Check(False, "", self.expired()), None

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
        # The one installation Thing predicate, shared with final health:
        # only configuration errors are terminal. Every other unfinished state
        # (missing or reloading handlers, bridged players whose bridge is
        # initializing, unknown details) waits within the deadline.
        pending, handlers_pending, failed, untyped = [], [], [], set()
        for row in things:
            verdict = thing_readiness(row)
            if verdict == "fail":
                failed.append(row)
            elif verdict == "pending":
                pending.append(row)
                if row["statusInfo"]["status"] == "UNINITIALIZED" and row["statusInfo"].get("statusDetail") in HANDLER_PENDING_DETAILS:
                    handlers_pending.append(row)
                elif untyped_channels(row):
                    # A lost YAML start-up race: only a restart rebuilds these.
                    handlers_pending.append(row)
                    untyped.add(row["UID"])
        self.symptoms, self.untyped = {row["UID"] for row in handlers_pending}, untyped
        if failed:
            shown = ", ".join(f"{row['UID']} {row['statusInfo']['status']}/{row['statusInfo'].get('statusDetail')}"
                              for row in failed[:8])
            return Check(False, "", f"native Things report a configuration error ({shown}); "
                         "fix the configuration before retrying; no recovery restart was requested"), None
        self.pending_rows = pending if self.complete else []
        installed = installed_addon_uids(addons)
        missing = sorted(self.required - installed)
        if missing:
            return Check(False, "waiting for required add-ons: " + ", ".join(missing)), None
        if not self.complete:
            # Gateway commissioning reloads authentication and jUPnP; Thing
            # initialization is required after it, not before.
            return Check(True, "required add-ons installed and native REST answers"), (tuple(sorted(installed)),)
        metrics_status = values[self.metrics_path].status
        if not pending and metrics_status == 200:
            # Device reachability can fluctuate without invalidating an
            # installation. Track the complete inventory, not ONLINE/OFFLINE.
            signature = (tuple(sorted(installed)), tuple(sorted(row["UID"] for row in things)))
            return Check(True, "required add-ons installed, Thing initialization complete, and authenticated native metrics answer"), signature
        first = (f"; e.g. {pending[0]['UID']} {pending[0]['statusInfo']['status']}/"
                 f"{pending[0]['statusInfo'].get('statusDetail') or 'NONE'}") if pending else ""
        detail = f"required add-ons installed; {len(pending)} Thing(s) still initializing{first}; native metrics HTTP {metrics_status}"
        # Only the symptoms count: another Thing's device flapping ONLINE and
        # OFFLINE must not hold off the restart they need.
        signature = (tuple(sorted(installed)), tuple(sorted((row["UID"], row["statusInfo"]["status"],
                     row["statusInfo"].get("statusDetail") or "NONE") for row in handlers_pending)), metrics_status) if handlers_pending else None
        return Check(False, detail), signature

    def check(self, system: System) -> Check:
        found, signature = self.snapshot(system)
        self.problem = found.problem
        if found.done:
            self.reset()
            now = system.clock()
            if signature != self.ready_signature or self.ready_since is None:
                self.ready_signature, self.ready_since = signature, now
            if now - self.ready_since < self.settle:
                found = Check(False, self.stable.format(settle=self.settle))
        else:
            self.ready_signature = self.ready_since = None
            if signature is None:
                self.reset()
            elif signature != self.signature:
                self.signature, self.since = signature, system.clock()
        if not found.problem:
            self.last = found.detail
        # A dry-run may reach this before its planned token creation/replacement.
        # Describe the current report, then let the actual task enforce terminal
        # authentication/configuration errors without retrying those failures.
        return Check(found.done, found.detail or found.problem)

    def actions(self, system: System) -> list[Action]:
        return [Task(f"wait for {self.settle} seconds of stable initialization within the {self.native.describe()} native readiness "
                     "allowance this run shares; offer at most one approved same-state restart after unchanged known first-start "
                     "handler symptoms for 60 seconds", self.wait)]

    def wait(self, system: System) -> int:
        try:
            return self._wait(system)
        finally:
            self.native.stop(system)

    def _wait(self, system: System) -> int:
        self.begin_wait()
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
                return self.ready(found)
            deadline = self.native.start(system)
            problem = self.watch.problem(system, deadline)
            if problem:
                system.say("   " + problem)
                return 1
            progress.update(found.detail)
            if self.recovery and not self.restarted and self.since is not None and system.clock() - self.since >= self.grace:
                generation = system.unit("thermite-openhab.service", timeout=max(0.001, deadline - system.clock())).generation
                if generation is None:
                    system.say("   cannot verify the native service generation; no recovery restart was requested")
                    return 1
                self.restarted = True
                causes = [f"{len(self.untyped)} Thing(s) have channels openHAB loaded without their item types "
                          f"(e.g. {min(self.untyped)}), so they never poll"] if self.untyped else []
                if len(self.untyped) < len(self.symptoms):
                    causes.append("installed add-ons still have unchanged missing/not-yet-ready handlers")
                system.say(f"   {' and '.join(causes)}; one restart may recover this first-start condition without "
                           "replacing userdata or credentials")
                command = Command(("systemctl", "--no-block", "restart", "thermite-openhab.service"), root=True)
                code = runner.perform(command, _RestartBeforeDeadline(system, deadline), self.context.area)
                if code is None or code:
                    return code or 1
                self.watch.requested += 1
                self.previous_generation = generation
                self.reset()
            left = deadline - system.clock()
            if left <= 0:
                system.say("   " + self.expired())
                return 1
            system.sleep(min(POLL, left))


class NativeReady(NativeReadiness):
    """The authoritative final wait, after commissioning and activation."""


class NativeSettle(NativeReadiness):
    """Wait for add-on installation to stop churning REST before commissioning.

    Commissioning starts as soon as required add-ons are installed and native
    REST has answered consistently for `settle` seconds. Thing initialization
    is checked afterwards, by NativeReady, within the same native deadline.
    """

    title = "Native add-ons before commissioning"
    complete = False
    recovery = False
    stable = "required add-ons are installed; waiting for {settle} seconds of stable native REST before commissioning"

    def actions(self, system: System) -> list[Action]:
        return [Task(f"wait for required add-ons and {self.settle} seconds of stable native REST within the "
                     f"{self.native.describe()} native readiness allowance this run shares", self.wait)]


class Commissioning:
    """Gateway boundary hook: commission native security within the run's
    native deadline. The native configurations it replaces live in userdata,
    which an install transaction sets aside and puts back."""

    def __init__(self, context: Context, native: NativeDeadline):
        self.context = context
        self.native = native
        self.gateway = None

    def __call__(self, system: System, metadata: dict, callback: Path | None) -> Path | None:
        from oh_conf import House
        from oh_access import AccessError
        from oh_access.files import json_private
        from oh_access.native_boundary import MAX_DEADLINE, configure
        try:
            remaining = self.native.remaining(system)
            if remaining <= 0:
                raise AccessError(f"the {self.native.describe()} native readiness allowance expired before gateway "
                                  "commissioning; rerun the deployment, with --native-timeout SECONDS for a slower host")
            house = Path(self.gateway.options.get("house", self.context.configuration / "instance/devices.yaml"))
            result = configure(self.context.url, self.context.area / self.context.token_file,
                               House.load(house).data, self.context.address, existing_callbacks=callback,
                               timeout=min(remaining, MAX_DEADLINE), clock=system.clock, sleep=system.sleep)
        finally:
            self.native.stop(system)
        if result["callbacks"] is None:
            return None
        selected = self.gateway.inputs / "callbacks.json"
        json_private(selected, result["callbacks"])
        return selected


def gateway_history(context: Context):
    """Gateway record hook: append the activated release to the runtime
    deploy history, through the thermite group as the deploy itself does."""
    def record(system: System, event: dict) -> bool:
        from oh_install.runner import perform
        argv = write(context, "gateway", str(context.layout.root), json.dumps(event, sort_keys=True))
        return perform(operator_command(context, system, argv), system, context.area) == 0
    return record


def gateway_steps(context: Context, native: NativeDeadline, transaction: str | None = None) -> list[Step]:
    """Settle native REST, then commission and activate the HTTPS gateway."""
    hook = Commissioning(context, native)
    hook.gateway = Gateway(context, boundary=hook, record=gateway_history(context), transaction=transaction)
    return [NativeSettle(context, native), hook.gateway]


class Verify(Step):
    title = "Read-only check"
    always = True
    #: Seconds final health gives a start to finish. Things that a late
    #: handler reload left initializing are warnings then, not failures;
    #: monitoring still not answering then fails it.
    settle = 120

    def __init__(self, context: Context, *, materialized: bool = True):
        self.context = context
        self.materialized = materialized

    def check(self, system: System) -> Check:
        return Check(False, "installation readiness, device warnings, and what is deployed against this checkout")

    def actions(self, system: System) -> list[Action]:
        addons = (("--addons", str(self.context.configuration / "conf/services/addons.cfg"))
                  if self.materialized and self.context.prepared is not None else ())
        revision = ("--revision", self.context.head) if self.context.prepared is not None else ()
        prefix = application(self.context)
        if not self.materialized and self.context.application is not None:
            import oh_release
            # An already-current deployment needs no local build or cache write.
            # Health can derive its add-on requirements from the committed inputs.
            program = oh_release.resolve(self.context.var / "application-releases",
                                         self.context.application["digest"], self.context.application["version"])
            prefix = (sys.executable, str(program / "bin/thermite"), "--config", str(self.context.area))
        return [Command((*prefix, "health", "--production", "--installation", "--settle", str(self.settle), *addons, *revision),
                        show_output=True),
                operator_command(self.context, system, (*prefix, "status", *revision))]


# --------------------------------------------------------------------------
# thermite upgrade


class Committed(Step):
    title = "Committed configuration"

    def __init__(self, context: Context, *, head: str | None = None):
        self.context = context
        #: The site's HEAD: the commit installed, unless a rollback installs
        #: an earlier one.
        self.head = head or context.head

    def check(self, system: System) -> Check:
        area = self.context.area
        problems = []
        from oh_paths import SITE_FILE
        if self.context.prepared is None:
            argv = (application(self.context, "generate", "--check") if (area / SITE_FILE).is_file()
                    else (sys.executable, str(area / "bin/thermite"), "generate", "--check"))
            code, _ = system.capture(argv)
            if code:
                problems.append("conf/ or deploy/ is not what the house file and versions.json generate: "
                                "run thermite generate, then commit")
        else:
            code, out = system.capture(["git", "-C", str(area), "rev-parse", "HEAD"])
            if code or out.strip() != self.head:
                problems.append("the site commit changed during planning; rerun from the intended commit")
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
        detail = "committed inputs and their pinned application are verified" if self.context.prepared is not None else "deploy/ is generated"
        if self.head != self.context.head:
            return Check(True, f"{detail}; HEAD, {self.head[:12]}, is clean, and {self.context.head[:12]} is "
                               "installed from it")
        return Check(True, f"{detail}; HEAD, {self.context.head[:12]}, has every change")


class AppArmorService(Step):
    """AppArmor's own service, enabled before an install transaction
    begins: it is the host's, never one of the transaction's items."""

    title = "AppArmor service"

    def check(self, system: System) -> Check:
        if not oh_apparmor.enabled(system):
            return Check(True, "kernel AppArmor disabled")
        unit = system.unit("apparmor.service")
        return Check(unit.active == "active" and unit.enabled == "enabled", "apparmor.service enabled and running")

    def actions(self, system: System) -> list[Action]:
        return [Command(("systemctl", "enable", "--now", "apparmor.service"), root=True)]


class Halt(Step):
    """Stop all of Thermite inside an install transaction, whatever changes:
    the engine sets the version aside only once nothing of it runs. The
    transaction's rollback starts the previous version again."""

    title = "Stop Thermite"
    #: What runs while Thermite does. The slice holds every service, so it
    #: is the authority; the names say what still runs.
    UNITS = (oh_units.TARGET, oh_units.SLICE, *oh_units.DAEMONS)

    def __init__(self, context: Context):
        self.context = context

    def running(self, system: System) -> list[str]:
        """What of Thermite runs, or cannot be shown not to: its network
        shares too, this host's and those it installed before (the installed
        manifest), which the target's stop unmounts."""
        mounts = [*(share.unit for _, share in sorted(shares.settings(self.context.host).items())),
                  *(Path(path).name for path in sorted(ownership.recorded(self.context, system))
                    if path.endswith(".mount"))]
        return [f"{name} ({origin.active})"
                for name, origin in system.unit_origins(list(dict.fromkeys([*self.UNITS, *mounts]))).items()
                if not origin.known or origin.active in ACTIVE]

    def check(self, system: System) -> Check:
        running = self.running(system)
        if not running:
            return Check(True, f"nothing in {oh_units.SLICE} runs")
        return Check(False, f"to stop {oh_units.TARGET} and {oh_units.SLICE} ({', '.join(running)})")

    def actions(self, system: System) -> list[Action]:
        return [Command(("systemctl", "stop", oh_units.TARGET, oh_units.SLICE), root=True),
                Task(f"confirm nothing in {oh_units.SLICE} still runs", self.stopped)]

    def stopped(self, system: System) -> int:
        running = self.running(system)
        if running:
            system.say(f"   still running after the stop: {', '.join(running)}; nothing was set aside. "
                       f"`systemctl status {oh_units.SLICE}` shows what runs in it; find out why it did not "
                       "stop, then rerun.")
            return 1
        return 0


class StartTarget(Step):
    """Start thermite.target inside an install transaction. The boot guard
    keeps it disabled; the commit enables it."""

    title = "Start Thermite"

    def __init__(self, context: Context):
        self.context = context

    def check(self, system: System) -> Check:
        return Check(system.unit(oh_units.TARGET).active == "active", f"{oh_units.TARGET} running")

    def actions(self, system: System) -> list[Action]:
        return [Command(("systemctl", "start", oh_units.TARGET), root=True)]


class Health(Verify):
    """The gate an install transaction commits on: health --production
    --installation, whose exit status counts configuration errors, missing
    add-ons and failed monitoring, never device warnings.
    Any exit status but 0 stops the run, as in every step, so the
    transaction rolls back: changeover.run commits only after a run in
    which all steps passed."""

    title = "Health of the new version"

    def actions(self, system: System) -> list[Action]:
        return super().actions(system)[:1]


def boundary_owned(system: System) -> bool | None:
    """Whether the native boundary unit is the installer's exact root-owned
    file; None when there is none."""
    from oh_install.ingress import UNIT_FILE, recognized_unit
    if not system.lexists(UNIT_FILE):
        return None
    file = system.unit_file(UNIT_FILE)
    return (file is not None and file.uid == 0 and not file.mode & 0o022 and file.ancestors_trusted
            and recognized_unit(file.content))


def install_preflight(context: Context) -> list[Step]:
    return [Preconditions(context), Committed(context)]
