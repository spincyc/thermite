"""Thermite's install transactions: install (and install --clean),
upgrade, uninstall (and --purge), rollback to an earlier site commit, and
revert to the version kept before the last one.

Each first prepares everything it can while Thermite keeps running: the
configuration, packages, the boot test, the pinned images, and the room the
install engine (lib/oh_install/engine.py, run as root) needs to set the
installed version aside. Then the engine begins the transaction, guards the
boot (thermite.target stays disabled), and once Thermite is stopped collects
the installed version. The new version is switched in by the ordinary
install steps (an uninstall switches in nothing; a revert has the engine
put the kept version back) and verified by its health; only then does the
engine commit it. A failure from begin on rolls the transaction back in the
same run and the version that ran before runs again: the rollback is the
only undo.

The operator keeps its own record of each transaction in the site's
var/transaction/<id>/: record.json, the inputs it handed the engine, the
operator files it set aside (F3) and the gateway's attempt record.
var/transaction/open names a transaction that has not finished; the next
owner command that runs a transaction finishes or rolls it back first, then
stops and says so, and thermite status names it.
"""
from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path
import shlex
import shutil
from typing import Callable

import oh_units
from oh_install import bridge, engine, manifest, ownership, runner, steps
from oh_install.ingress import NativeIngress, NativeIngressPrerequisites
from oh_install.model import Check, Command, Step, Task
from oh_install.system import System

#: The exit statuses of a transaction's outcomes, for every owner command
#: that runs one.
COMMITTED, REFUSED, ROLLED_BACK, UNHEALTHY, STUCK, INTERRUPTED = 0, 1, 3, 4, 5, 130
#: The interpreter that runs the engine as root: the system's, isolated.
PYTHON = "/usr/bin/python3"
#: The file naming the transaction that has not finished, in var/transaction.
OPEN = "open"
#: How long the reinstated version's health waits for initializing Things.
SETTLE = 120


def transactions(var: Path) -> Path:
    return Path(var) / "transaction"


def open_transaction(var: Path) -> str | None:
    """The id of the install transaction that has not finished, if any."""
    try:
        value = (transactions(var) / OPEN).read_text().strip()
    except FileNotFoundError:
        return None
    return value or None


#: The owner commands that run an install transaction, as their records
#: name them, and what they are doing.
DOING = {"install": "installing", "upgrade": "upgrading", "uninstall": "uninstalling",
         "rollback": "rolling back", "revert": "reverting"}
#: What the messages call a transaction of these kinds.
NAMES = {"clean": "Clean install", "purge": "Purge", "revert": "Revert"}


def command(site: Path, verb: str = "upgrade") -> str:
    """The owner command, for this site; each resolves an unfinished
    transaction before anything else."""
    return f"thermite {verb} --config {shlex.quote(str(site))}"


def upgrade_command(site: Path) -> str:
    return command(site, "upgrade")


def escaped(site: Path, verb: str, exc) -> str:
    """What to say of a failed command no step handled (a
    CalledProcessError): never that nothing changed, since an install
    transaction may have begun."""
    program = Path(str(exc.cmd[0] if isinstance(exc.cmd, (list, tuple)) and exc.cmd else exc.cmd)).name
    id = open_transaction(Path(site) / "var")
    if id:
        return (f"a step failed ({program} exited {exc.returncode}); install transaction {id} has not finished: "
                f"run {pending_command(site)} to finish or roll it back")
    return f"a step failed ({program} exited {exc.returncode}); no install transaction is open: run " \
           f"{command(site, verb)} again to go on"


def pending_command(site: Path) -> str:
    """The owner command that resolves the open transaction: the one that
    began it."""
    var = Path(site) / "var"
    try:
        verb = Transaction.load(var, open_transaction(var) or "").record.get("verb")
    except (OSError, ValueError):
        verb = None
    return command(site, verb if verb in DOING else "upgrade")


def _write(path: Path, data: bytes) -> None:
    """An operator file, private, replaced whole."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name("." + path.name + ".partial")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _json(path: Path, value) -> None:
    _write(path, (json.dumps(value, indent=2, sort_keys=True) + "\n").encode())


def _allocated(path: Path) -> int:
    """Bytes allocated below `path`: what a copy of it may need."""
    total = 0
    for directory, _, names in os.walk(path):
        for name in names:
            try:
                total += os.lstat(os.path.join(directory, name)).st_blocks * 512
            except OSError:
                pass
    return total


@dataclasses.dataclass
class Transaction:
    """One install transaction, as the operator records it."""

    var: Path
    id: str
    record: dict

    @property
    def directory(self) -> Path:
        return transactions(self.var) / self.id

    @property
    def site(self) -> Path:
        return Path(self.record["site"])

    @property
    def root(self) -> Path:
        return Path(self.record["root"])

    @property
    def engine(self) -> Path:
        """The copy of the engine this transaction began with; any copy
        hands a begun transaction to the one root pinned."""
        copy = self.directory / "engine.py"
        return copy if copy.is_file() else Path(engine.__file__)

    @property
    def pinned(self) -> Path:
        return self.root / ".transaction" / self.id / "engine.py"

    def file(self, name: str) -> Path:
        return self.directory / name

    @property
    def verb(self) -> str:
        return self.record.get("verb", "upgrade")

    @property
    def name(self) -> str:
        """What the messages call it: Upgrade, Install, Clean install..."""
        return NAMES.get(self.record.get("kind"), self.verb.capitalize())

    @property
    def rerun(self) -> str:
        return command(self.site, self.verb)

    def argv(self, verb: str, *args) -> tuple[str, ...]:
        named = ("--id", self.id) if verb not in ("plan", "status") else ()
        return (PYTHON, "-I", "-S", str(self.engine), verb, "--root", str(self.root), *named, *map(str, args))

    def command(self, verb: str, *args) -> Command:
        return Command(self.argv(verb, *args), root=True, terminal=True)

    def save(self, **changes) -> None:
        self.record.update(changes)
        _json(self.file("record.json"), self.record)

    @classmethod
    def create(cls, context: steps.Context, id: str, *, kind: str, verb: str, before: str | None,
               after: str | None, previous: str | None, gid: int, installed: bool, history: bool,
               pinned: bool = True, bridged: bool = False, gateway: str | None = None,
               then: dict | None = None) -> Transaction:
        """`before`/`after`: the site commits of the version that runs and of
        the one installed. `installed`: whether a version of Thermite runs
        before it, to come back if it rolls back. `history`: whether it
        deploys a site commit, so that a rollback is recorded in thermite
        history. `pinned`: whether the site was moved to `after` for it (a
        pin commit made for it, or a revert's reset back), to undo if it
        does not commit. `gateway`: the gateway release the
        bridged version runs, kept until the next commit. `then`: the
        operator's side of its commit (committed())."""
        transaction = cls(context.var, id, {
            "version": 1, "id": id, "kind": kind, "verb": verb, "site": str(context.area),
            "root": str(context.installation.root), "state": str(context.layout.root), "gid": gid,
            "head_before": before, "head_after": after, "installed": installed, "history": history,
            "pinned": pinned, "bridged": bridged, "gateway_before": gateway,
            "application": {"current": (context.application or {}).get("digest"), "previous": previous},
            "then": dict(then or {}), "asides": {}, "phase": "preparing"})
        transaction.directory.mkdir(mode=0o700, parents=True)
        _write(transaction.directory / "engine.py", Path(engine.__file__).read_bytes())
        transaction.save()
        return transaction

    @classmethod
    def load(cls, var: Path, id: str) -> Transaction:
        record = json.loads((transactions(var) / id / "record.json").read_text())
        if not isinstance(record, dict) or record.get("id") != id:
            raise ValueError(f"{transactions(var) / id / 'record.json'} is not transaction {id}'s record")
        return cls(Path(var), id, record)

    # The open pointer ----------------------------------------------------

    def open(self) -> None:
        _write(transactions(self.var) / OPEN, (self.id + "\n").encode())

    def close(self, phase: str) -> None:
        self.save(phase=phase)
        if open_transaction(self.var) == self.id:
            (transactions(self.var) / OPEN).unlink()

    # Operator files (F3) -------------------------------------------------

    def set_aside(self, paths) -> None:
        """Copy the operator's own files the switch may rewrite: the runtime
        token, the Grafana password, runtime credentials and the gateway
        profile. A rollback puts them back as they were, or removes them."""
        asides = {}
        for number, path in enumerate(paths):
            path = Path(path)
            if path.is_file() and not path.is_symlink():
                aside = self.file("operator") / str(number)
                _write(aside, path.read_bytes())
                asides[str(path)] = aside.name
            elif not path.exists() and not path.is_symlink():
                asides[str(path)] = None
        self.save(asides=asides)

    def put_back(self) -> None:
        for path, aside in self.record.get("asides", {}).items():
            path = Path(path)
            if aside is None:
                path.unlink(missing_ok=True)
            else:
                _write(path, (self.file("operator") / aside).read_bytes())

    # The site pin (F7) ---------------------------------------------------

    def unpin(self, system: System) -> None:
        """Undo the pin commit made for this transaction, so the site pins
        the version that runs; say how when that cannot be done safely.
        Rerunning it finishes a reset that stopped partway."""
        before, after = self.record.get("head_before"), self.record.get("head_after")
        site = shlex.quote(str(self.site))
        if not self.record.get("pinned", True):
            return
        if before is None:
            system.say("   The site may still pin the new release: its revision before the transaction is not known. "
                       f"Review `git -C {site} log -2` and revert the pin commit if it is there.")
            return
        if before == after:
            return
        # A revert's pin reset went back to an ancestor; undoing it goes forward.
        back = (f"git -C {site} merge --ff-only {before[:12]}" if self.record.get("kind") == "revert"
                else f"git -C {site} revert --no-edit {after[:12]}")
        reset_site(system, self.site, expected=after, previous=before, back=back, indent="   ")

    # What root's engine says ---------------------------------------------

    def journal(self, system: System) -> dict | None:
        """This transaction as engine status reports it; None when begin
        never wrote its journal. ValueError when status cannot run."""
        code = runner.perform(Command(("-v",), root=True, terminal=True), system, self.site, root_confirmed=True)
        if code:
            raise ValueError("sudo authentication is needed to read the install engine's journal")
        code, out = system.capture(("sudo", "-n", *self.argv("status", "--json")), diagnostics=True)
        try:
            found = json.loads(out) if code == 0 else None
        except ValueError:
            found = None
        if not isinstance(found, list):
            raise ValueError(f"the install engine's status failed (exit status {code})")
        return next((each for each in found if each.get("id") == self.id and each.get("phase")), None)


def _operator(record: dict, system: System, argv: tuple[str, ...]) -> Command:
    """An owner command with the thermite group, as steps.operator_command."""
    if record["gid"] in system.process_groups():
        return Command(argv)
    if system.which("sg"):
        return Command(("sg", steps.NAME, "-c", shlex.join(argv)))
    return Command(("-u", system.login(), *argv), root=True)


def _application(record: dict, *args: str) -> tuple[str, ...]:
    """This application's own command for the site."""
    import sys
    from oh_paths import PROGRAM
    return (sys.executable, str(PROGRAM / "bin/thermite"), "--site", record["site"], *args)


# Steps of the transaction ---------------------------------------------------

class Prefetch(Step):
    """Every pinned image, pulled before anything stops."""

    title = "Container images"
    always = True
    stop_on_failure = True

    def __init__(self, context: steps.Context):
        self.context = context
        self.pulled: list[str] = []

    def check(self, system: System) -> Check:
        return Check(False, f"the {len(manifest.images(self.context))} pinned images, pulled while Thermite runs")

    def actions(self, system: System) -> list:
        return [Task("pull each pinned image that is not here yet", self.fetch)]

    def fetch(self, system: System) -> int | None:
        code = runner.perform(Command(("-v",), root=True, terminal=True), system, self.context.area)
        if code:
            return code
        for ref in manifest.images(self.context):
            found, _ = system.capture(("sudo", "-n", "podman", "image", "exists", ref))
            if found == 0:
                continue
            if found != 1:
                system.say(f"   podman image exists {ref}: exit status {found}")
                return found
            code = runner.perform(Command(("podman", "pull", ref), root=True, terminal=True), system, self.context.area)
            if code is None or code:
                return code
            self.pulled.append(ref)
        return 0


class Room(Step):
    """The inputs the engine reads, and the room it needs, before anything
    stops; too little refuses the transaction with the table (9-Q15)."""

    title = "Room to set the installed version aside"
    always = True
    stop_on_failure = True

    def __init__(self, transaction: Transaction, inputs):
        self.transaction = transaction
        self.inputs = inputs

    def check(self, system: System) -> Check:
        return Check(False, "what the install engine copies aside, and the free space for it")

    def actions(self, system: System) -> list:
        return [Task("write the transaction's inputs and ask the install engine for the room it needs", self.measure)]

    def measure(self, system: System) -> int | None:
        self.inputs(system)
        transaction = self.transaction
        code = runner.perform(Command(("-v",), root=True, terminal=True), system, transaction.site)
        if code:
            return code
        current = ("--current", transaction.file("current.json")) if transaction.file("current.json").exists() else ()
        code, out = system.capture(("sudo", "-n", *transaction.argv("plan", "--desired", transaction.file("desired.json"),
                                                                     *current)), diagnostics=True)
        try:
            space = json.loads(out)
        except ValueError:
            space = None
        if space is not None and code in (0, engine.SHORT):
            for line in engine.describe_space(space, transaction.root).splitlines():
                system.say("   " + line)
        return code or (0 if space is not None else 1)


class Begin(Step):
    """Record the transaction: the operator's record and open pointer, then
    the engine's journal, as root, with the engine pinned beside it."""

    title = "Begin the install transaction"
    always = True
    stop_on_failure = True

    def __init__(self, transaction: Transaction, paths):
        self.transaction = transaction
        self.paths = paths

    def check(self, system: System) -> Check:
        return Check(False, f"transaction {self.transaction.id}")

    def actions(self, system: System) -> list:
        transaction = self.transaction
        current = ("--current", transaction.file("current.json")) if transaction.file("current.json").exists() else ()
        return [Task("set aside the operator's own files and mark the transaction open", self.opened),
                transaction.command("begin", "--desired", transaction.file("desired.json"),
                                    "--operator", transaction.file("operator.json"), *current)]

    def opened(self, system: System) -> int:
        self.transaction.set_aside(self.paths)
        self.transaction.save(phase="open")
        self.transaction.open()
        return 0


class Engine(Step):
    """One engine verb, as root, with the engine root pinned at begin."""

    always = True
    stop_on_failure = True

    def __init__(self, transaction: Transaction, verb: str, title: str, detail: str, *args):
        self.transaction = transaction
        self.verb = verb
        self.title = title
        self.detail = detail
        self.args = args

    def check(self, system: System) -> Check:
        return Check(False, self.detail)

    def actions(self, system: System) -> list:
        return [self.transaction.command(self.verb, *self.args)]


class Authorize(Step):
    """sudo -v, so the commit or rollback after a long verification does
    not wait at a password prompt (9-Q9)."""

    title = "Authorize the commit or rollback"
    always = True
    stop_on_failure = True

    def check(self, system: System) -> Check:
        return Check(False, "sudo credentials fresh")

    def actions(self, system: System) -> list:
        return [Command(("-v",), root=True, terminal=True)]


class Confirm(Step):
    """Typing a path, for a transaction whose commit deletes what is under
    it: nothing has stopped yet, and declining refuses it."""

    always = True
    stop_on_failure = True

    def __init__(self, title: str, path: Path, warning: str):
        self.title = title
        self.path = Path(path)
        self.warning = warning

    def check(self, system: System) -> Check:
        return Check(False, f"type {self.path} to confirm")

    def actions(self, system: System) -> list:
        return [Task(f"type {self.path} to confirm", self.confirm, interactive=True)]

    def confirm(self, system: System) -> int:
        system.say("   " + self.warning)
        typed = system.ask(f"   Type {self.path} to go on: ").strip()
        if typed != str(self.path):
            system.say(f"   That is not {self.path}; nothing was changed.")
            return 1
        return 0


class ClearState(Step):
    """A clean install's fresh start, inside the transaction: the deploy's
    records and the operator's credential copies, which collect copied
    aside and Begin set aside, go; the state trees already moved aside
    whole. A rollback puts all of it back."""

    title = "Clear the state the clean install replaces"
    always = True
    stop_on_failure = True

    def __init__(self, context: steps.Context, files):
        self.context = context
        self.files = list(files)

    def check(self, system: System) -> Check:
        return Check(False, "the deploy records and local credential copies, set aside first")

    def actions(self, system: System) -> list:
        records = [str(path) for path in self.files]
        return [*([Command(("rm", "-f", "--", *records), root=True)] if records else []),
                Command(("rm", "-f", "--", *map(str, operator_files(self.context)[:3])))]


# What each transaction does -------------------------------------------------

@dataclasses.dataclass
class Work:
    """One transaction's own part: its engine kind, the owner command that
    runs it, what it prepares while Thermite runs, what it switches in and
    how it verifies, and what commit records (None: the installed release
    carries over)."""

    kind: str
    verb: str
    preparation: list[Step]
    switching: list[Step]
    verification: list[Step]
    release: dict | None
    #: The desired items, when not those the switching steps declare.
    items: Callable[[System], list[manifest.Item]] | None = None
    #: The steps whose items describe a version 1 installation (the bridge).
    described: list[Step] | None = None
    pull: bool = True
    confirm: Step | None = None
    #: Whether the engine puts the kept version back (a revert).
    reinstate: bool = False
    #: The operator's own side of the commit, as data the record keeps, so
    #: that whichever run finishes the commit does it (committed()):
    #: {"select": the owner command ~/.local/bin/thermite selects then}.
    then: dict = dataclasses.field(default_factory=dict)
    #: The step whose deferred callback relay needs one more install.
    gateway: Step | None = None


def release(context: steps.Context) -> dict:
    """What commit records about this version."""
    return {"release": {"site": context.head, "configuration": context.conf_digest,
                        "hostname": context.hostname, "address": context.address},
            "pins": manifest.pins(context), "images": manifest.images(context),
            "application": (context.application or {}).get("digest")}


def preparation(context: steps.Context) -> list[Step]:
    """What the transaction needs that is the host's, never an item: done
    while Thermite runs. It ends with the installation root, which the
    transaction plans and begins inside."""
    return [*steps.configuration_steps(context), steps.Packages(), NativeIngressPrerequisites(context),
            steps.AppArmorService(), steps.BootTest(context), steps.Account(context), steps.InstallationRoot(context)]


def switching(context: steps.Context, transaction: str | None, how: str = "upgrade") -> list[Step]:
    """The new version, installed over the collected one, and started."""
    native = steps.NativeDeadline.of(context)
    deployment = steps.Deploy(context)
    deployment.how = how
    return [steps.StateDirectories(context), steps.MetricsDirectories(context), steps.Units(context),
            steps.NodeConfinement(context), steps.NativePreparation(context), NativeIngress(context), deployment,
            steps.Services(context, restart_runtime=context.restart_runtime), steps.Answers(context, native),
            steps.Token(context, native), *steps.gateway_steps(context, native, transaction=transaction),
            steps.MetricsToken(context), steps.GrafanaPassword(context), steps.MetricsServices(context),
            steps.StartTarget(context), steps.NativeReady(context, native)]


def verification(context: steps.Context) -> list[Step]:
    return [Authorize(), steps.MetricsAnswer(context), steps.Health(context)]


def operator_files(context: steps.Context) -> list[Path]:
    return [context.area / context.token_file, context.area / context.grafana_password_file,
            context.area / context.runtime_credentials_file, context.var / "gateway/current.json"]


def install_work(context: steps.Context, id: str | None, *, kind: str = "install", verb: str | None = None) -> Work:
    """Install, upgrade, a clean install or a rollback to an earlier
    commit: the version `context` describes, installed over the one that
    runs. A clean install moves the runtime state trees aside whole and
    clears the deploy's records and local credential copies; the commit
    deletes what was moved aside."""
    verb = verb or ("upgrade" if kind == "upgrade" else "install")
    changes = switching(context, id, "upgrade" if kind == "upgrade" else "rollback" if verb == "rollback"
                        else "install")
    gateway = next((step for step in changes if hasattr(step, "deferred")), None)
    work = Work(kind, verb, preparation(context), changes, verification(context), release(context),
                described=changes, gateway=gateway)
    if kind == "clean":
        root = context.installation.root
        layout = context.layout

        def items(system: System) -> list[manifest.Item]:
            declared = manifest.desired(changes, system, root)
            return [manifest.Item(item.path, "tree-move") if item.cls in ("tree", "shared-pinned")
                    and engine.below(item.path, layout.root) else item for item in declared]

        files = [item.path for step in changes if isinstance(step, steps.Deploy)
                 for item in step.desired(None) if item.cls == "file"]
        work.items = items
        work.switching = [ClearState(context, files), *changes]
        work.confirm = Confirm(
            "Confirm the clean install", layout.root,
            f"This clean install deletes the state under {layout.root} once it commits: accounts, pairings, "
            "credentials, the deploy's records, monitoring data and manually installed binding repairs; it "
            f"also clears {context.token_file}, {context.grafana_password_file} and "
            f"{context.runtime_credentials_file}. Logs, the deploy history and old backups stay; so do the "
            "site's configuration and the gateway's identity and TLS state. Until the commit a rollback puts "
            "all of it back.")
    return work


def uninstall_work(context: steps.Context, id: str | None, *, purge: bool = False) -> Work:
    """Uninstall: Thermite stopped and its root files, units, filter and
    confinement set aside; its state stays and is recorded, so a later
    install finds it. --purge also moves runtime/ and access/ aside whole
    and, at the commit, removes them with the installed manifest, the
    images and every transaction."""
    root = context.installation.root
    work = Work("purge" if purge else "uninstall", "uninstall", [], [], [Authorize()], None,
                items=lambda system: [], described=switching(context, None), pull=False)
    if purge:
        work.items = lambda system: [manifest.Item(root / "access", "tree-move"),
                                     manifest.Item(root / "runtime", "tree-move")]
        work.confirm = Confirm(
            "Confirm the purge", root,
            f"This purge deletes {root}/runtime and {root}/access once it commits: accounts, pairings, credentials, "
            "history, monitoring data, backups, and the gateway's identity, TLS state and releases; also Thermite's "
            "images and the installed manifest. Nothing brings them back but a copy you made.")
    return work


def unchanged(context: steps.Context, system: System, installed: dict | None) -> bool:
    """Whether the installed version is this one, as recorded and as found:
    then the owner command only verifies it."""
    if installed is None or not installed.get("installed", True):
        return False
    wanted = release(context)
    if (installed["release"] != wanted["release"] or installed["pins"] != wanted["pins"]
            or installed["application"]["current"] != wanted["application"]):
        return False
    items = manifest.desired(switching(context, None), system, context.installation.root)
    if {(item["path"], item["class"]) for item in installed["items"]} != {(str(item.path), item.cls) for item in items}:
        return False
    if (steps.Units(context).stale(system) or not NativeIngress(context).current(system)
            or not steps.NodeConfinement(context).check(system).done or not steps.Deploy(context).check(system).done):
        return False
    target = system.unit(oh_units.TARGET)
    if target.active != "active" or target.enabled != "enabled":
        return False
    return not any(any(step.plan(system)) for step in (steps.Services(context), steps.MetricsServices(context)))


def previous_application(installed: dict | None, system: System) -> str | None:
    if installed is not None:
        return installed["application"]["current"]
    found = system.unit_file(ownership.MANIFEST)
    try:
        value = json.loads(found.content).get("release") if found else None
    except (ValueError, AttributeError):
        value = None
    return value if isinstance(value, str) else None


def preview(context: steps.Context, system: System, work: Work, *, first: int) -> int:
    """What a dry run shows: the preparation's checks, then the transaction."""
    code = runner.run(work.preparation, system, context.area, dry_run=True, first=first)
    titles = ", ".join(step.title for step in work.switching)
    pull = "pull the pinned images, " if work.pull else ""
    confirm = f"ask you to type {work.confirm.path}, " if work.confirm else ""
    switch = (f"install and start the new version ({titles}); verify its health" if work.switching and not
              work.reinstate else "put the kept version back, move the site back to it and start it; verify its health "
              "with its own application" if work.reinstate
              else "nothing new is installed")
    system.say(f"Then, as one install transaction: {pull}check the room to set the installed version aside, "
               f"{confirm}begin, guard the boot, stop Thermite and set it aside; {switch}; and commit. Any failure "
               "from begin on rolls back in the same run and starts the installed version again.")
    return code


# Running one -------------------------------------------------------------------

def run(context: steps.Context, system: System, build: Callable[[str], Work], *, installed: dict | None,
        bridged: bool, before: str | None, first: int, after: str | None = None, pinned: bool = True) -> int:
    """Prepare, begin, switch, verify and commit; roll back on failure.

    `build` makes the transaction's Work for its id. `before`/`after` are
    the site commits of the version that runs and of the one installed;
    `pinned` says `after` is a pin commit made for this transaction, undone
    when it does not commit."""
    id = manifest.transaction_id()
    work = build(id)
    after = after or context.head
    if before is None and not pinned:
        before = after
    present = bool(installed and installed.get("installed", True)) or bridged
    transaction = Transaction.create(
        context, id, kind=work.kind, verb=work.verb, before=before, after=after,
        previous=previous_application(installed, system), gid=context.host["gid"], installed=present,
        history=work.release is not None and work.kind != "revert", pinned=pinned, bridged=bridged,
        gateway=_gateway(system) if bridged else None, then=work.then)
    prefetch = Prefetch(context)
    root = context.installation.root

    def inputs(system: System) -> None:
        tools = manifest.tools(system)
        items = work.items(system) if work.items is not None else manifest.desired(work.switching, system, root)
        _json(transaction.file("desired.json"), {
            "kind": work.kind, "tools": tools, "items": [item.to_json() for item in items],
            "pulled": prefetch.pulled, "extra": _allocated(context.program) if work.release is not None else 0})
        if bridged:
            described = manifest.desired(work.described or work.switching, system, root)
            _json(transaction.file("current.json"), bridge.describe(context, system, described, tools, engine._now()))
        _json(transaction.file("operator.json"), {
            "uid": os.getuid(), "site": str(context.area), "record": str(transaction.file("record.json")),
            "head_before": transaction.record["head_before"], "head_after": transaction.record["head_after"]})
        if work.release is not None:
            _json(transaction.file("release.json"), work.release)

    opening = [*work.preparation, *([prefetch] if work.pull else []), Room(transaction, inputs),
               *([work.confirm] if work.confirm else []), Begin(transaction, operator_files(context))]
    rerun = f"Nothing of Thermite was stopped or changed. Fix that, then run {transaction.rerun} again."
    try:
        code = runner.run(opening, system, context.area, first=first, rerun=rerun, root_confirmed=True)
    except KeyboardInterrupt:
        return _interrupted(system, transaction)
    except (OSError, ValueError) as exc:
        system.say(f"Stopped: {exc}. {rerun}")
        code = REFUSED
    if code:
        return refused(system, transaction)
    reinstate = [Engine(transaction, "reinstate", "Put the kept version back",
                        "its files, state and live rules, from the last transaction's asides")] if work.reinstate else []
    commit = ("--release", transaction.file("release.json")) if work.release is not None else ()
    rest = [Engine(transaction, "guard", "Guard the boot", f"{oh_units.TARGET} stays disabled until the commit"),
            steps.Halt(context),
            Engine(transaction, "collect", "Set the installed version aside", "copies and moves, as the plan says"),
            *reinstate, *work.switching, *work.verification,
            Engine(transaction, "commit", "Commit", "record what is installed now", *commit)]
    failed = f"The {transaction.verb} rolls back transaction {transaction.id} now."
    try:
        code = runner.run(rest, system, context.area, first=first + len(opening), rerun=failed,
                          root_confirmed=True)
    except KeyboardInterrupt:
        return _interrupted(system, transaction)
    except Exception as exc:  # Every failure rolls back; none leaves a half-installed version.
        system.say(f"   {type(exc).__name__}: {exc}")
        code = REFUSED
    code = roll_back(system, transaction, "a step failed") if code else committed(system, transaction)
    if code == COMMITTED and work.gateway is not None and getattr(work.gateway, "deferred", False):
        return relay(context, system, transaction, first=first + len(opening) + len(rest))
    return code


def retain(system: System, transaction: Transaction) -> None:
    """Retention after a commit (Q13 aside): the gateway releases the
    installed manifest names as current and previous stay, as does, until
    the next commit, the one a bridged version ran; the engine prunes
    older recorded ones, and this removes those staged before the engine,
    which no manifest records. Old runtime/backups tars stay untouched."""
    import oh_deploy
    try:
        installed = manifest.installed(system, transaction.root)
    except manifest.ManifestError:
        return
    releases = oh_deploy.Installation(transaction.root).access / "releases"
    item = next((item for item in (installed or {}).get("items", []) if item["path"] == str(releases)), None)
    names = system.directory_entries(releases) if item is not None else None
    if not names:
        return
    if transaction.record.get("bridged") and not transaction.record.get("gateway_before"):
        return  # The release the described version runs is not known: nothing goes.
    keep = {item.get("current"), item.get("previous"), transaction.record.get("gateway_before")}
    older = [releases / name for name in names if name not in keep]
    if not older:
        return
    code = runner.perform(Command(("rm", "-rf", "--one-file-system", "--", *map(str, older)), root=True),
                          system, transaction.site, root_confirmed=True)
    if code == 0:
        system.say(f"   Removed {len(older)} older gateway release(s) from {releases}.")
    else:
        system.say(f"   Could not remove the older gateway releases in {releases}; they stay until the next commit.")


def _gateway(system: System) -> str | None:
    """The release the installed gateway runs, by name; None when unknown."""
    from oh_access import AccessError
    from oh_install import access
    try:
        found = access.installed_release(system)
    except (OSError, ValueError, AccessError):
        return None
    return found.name if found and found.parent.name == "releases" else None


def relay(context: steps.Context, system: System, transaction: Transaction, *, first: int) -> int:
    """Commissioning found Sonos event callbacks the committed version did
    not declare a relay for: one more install transaction of the same
    version activates it. The commit stands whatever that one does."""
    system.say("Sonos event callbacks were found: one more install transaction activates their relay.")
    code = run(context, system, lambda id: install_work(context, id, verb=transaction.verb),
               installed=manifest.installed(system, context.installation.root), bridged=False,
               before=None, after=context.head, first=first, pinned=False)
    if code in (ROLLED_BACK, UNHEALTHY, REFUSED):
        system.say(f"The relay was not activated, but transaction {transaction.id} stays committed: run "
                   f"{command(context.area, 'install')} to activate it.")
        return COMMITTED
    return code


def _toplevel(site: Path) -> Path:
    from oh_source_upgrade import git
    return Path(git(site, "rev-parse", "--show-toplevel"))


def refused(system: System, transaction: Transaction) -> int:
    """Nothing began: the site goes back to pinning the version that runs."""
    transaction.unpin(system)
    transaction.close("refused")
    system.say(f"{transaction.name} refused: nothing of Thermite was stopped or changed.")
    return REFUSED


def _interrupted(system: System, transaction: Transaction) -> int:
    if open_transaction(transaction.var) == transaction.id:
        system.say(f"\nInterrupted. Transaction {transaction.id} is open: Thermite may be stopped, with "
                   f"{oh_units.TARGET} kept from booting. Run {transaction.rerun} to roll it back.")
    else:
        system.say(f"\nInterrupted before the transaction began: nothing of Thermite was stopped or changed. "
                   f"Run {transaction.rerun} again.")
        transaction.unpin(system)
        transaction.close("refused")
    return INTERRUPTED


def committed(system: System, transaction: Transaction) -> int:
    """The operator's side of a commit, whichever run reached it (straight
    through, a rollback that found it committed, or a later run resolving
    it): say so, prune the older gateway releases, select the owner command
    the record names, then close it. Rerunning it after a crash is
    harmless. A purge removes the site's transaction records instead."""
    kind = transaction.record.get("kind")
    say_committed(system, transaction)
    if kind not in engine.UNINSTALLING:
        retain(system, transaction)
    command = transaction.record.get("then", {}).get("select")
    if command:
        select(system, Path(command))
    if kind == "purge":
        # Its records go too; the gateway's authority stays with the site,
        # as its configuration does.
        shutil.rmtree(transactions(transaction.var), ignore_errors=True)
        return COMMITTED
    transaction.close("committed")
    # Earlier transactions' operator records are finished; the engine
    # removed their root side at this commit.
    for found in transactions(transaction.var).iterdir():
        if found.name != transaction.id and engine.ID.fullmatch(found.name) and found.is_dir():
            try:
                phase = json.loads((found / "record.json").read_text()).get("phase")
            except (OSError, ValueError, AttributeError):
                continue
            if phase in ("committed", "rolled-back", "refused"):
                shutil.rmtree(found, ignore_errors=True)
    return COMMITTED


def select(system: System, command: Path) -> None:
    """Point ~/.local/bin/thermite at `command` (a revert's previous
    application)."""
    import oh_application_update
    try:
        launcher = oh_application_update.select(Path.home(), command)
    except (OSError, ValueError) as exc:
        system.say(f"   {Path.home() / '.local/bin/thermite'} still selects the application before ({exc}); "
                   f"use {command} directly.")
        return
    system.say(f"   {launcher} selects {command.parent.parent.name} again.")


def say_committed(system: System, transaction: Transaction) -> None:
    kind, aside = transaction.record.get("kind"), f"{transaction.root}/.transaction/{transaction.id}"
    if kind == "purge":
        system.say(f"Purge committed: Thermite's files, its state under {transaction.root}, its images, the "
                   "installed manifest and every transaction are removed from this host.")
    elif kind == "uninstall":
        system.say(f"Uninstall committed: transaction {transaction.id}. Thermite's units, filter and confinement "
                   f"are removed; its state stays under {transaction.root}, and the removed version stays set "
                   f"aside under {aside} until the next transaction commits.")
    elif transaction.record.get("installed", True):
        system.say(f"{transaction.name} committed: transaction {transaction.id}. The version that ran before "
                   f"stays set aside under {aside} until the next transaction commits.")
    else:
        system.say(f"{transaction.name} committed: transaction {transaction.id}.")


def roll_back(system: System, transaction: Transaction, reason: str) -> int:
    """Put the version that ran before back, in this run, and say how it is."""
    system.say(f"Rolling back transaction {transaction.id} ({reason}): "
               + ("the installed version is put back and started again." if transaction.record.get("installed", True)
                  else "what it installed is set aside."))
    site = transaction.site
    try:
        code = runner.perform(Command(("-v",), root=True, terminal=True), system, site, root_confirmed=True)
        if code is None:
            return _interrupted(system, transaction)
        code = runner.perform(transaction.command("rollback"), system, site, root_confirmed=True)
    except KeyboardInterrupt:
        return _interrupted(system, transaction)
    if code is None:
        return _interrupted(system, transaction)
    if code == engine.STUCK or code not in (0, engine.ROLLED_BACK):
        return _stuck(system, transaction)
    if code == 0:
        found = transaction.journal(system)
        if found is not None and found["phase"] in ("committed", "kept"):
            system.say("   The transaction had already committed: the new version stays installed.")
            return committed(system, transaction)
    return rolled_back(system, transaction)


def _stuck(system: System, transaction: Transaction) -> int:
    system.say(f"The rollback of transaction {transaction.id} did not finish: Thermite stays stopped and "
               f"{oh_units.TARGET} is kept from booting. Run {transaction.rerun} to try again. "
               f"To roll back by hand instead, print the commands with: sudo {PYTHON} -I -S {transaction.pinned} "
               f"rollback --root {transaction.root} --id {transaction.id} --print, run them, then run "
               f"{transaction.rerun} so the engine checks and finishes it (docs/installation.md#a-stuck-rollback)")
    return STUCK


def rolled_back(system: System, transaction: Transaction) -> int:
    """The operator's side of a finished rollback: its own files, the site
    pin, the history; then the reinstated version's own health (9-Q10)."""
    transaction.put_back()
    transaction.unpin(system)
    record = transaction.record
    if record.get("head_after") and record.get("history", True):
        event = {"transaction": transaction.id, "commit": record["head_after"], "restored": record.get("head_before")}
        argv = _application(record, "history", "--state", record["state"], "--_record-rollback",
                            json.dumps(event, sort_keys=True))
        if runner.perform(_operator(record, system, argv), system, transaction.site, root_confirmed=True) != 0:
            system.say("   The rollback was not added to thermite history.")
    transaction.close("rolled-back")
    if not record.get("installed", True):
        system.say(f"Rolled back: transaction {transaction.id} is undone. Nothing of Thermite was installed "
                   "before it, and nothing is now.")
        return ROLLED_BACK
    healthy = health(system, transaction)
    if healthy:
        system.say(f"Rolled back: transaction {transaction.id} is undone and the version that ran before runs "
                   "again, healthy.")
    else:
        system.say(f"Rolled back: transaction {transaction.id} is undone and the version that ran before is back, "
                   "but its health check does not pass (above); the engine says so above when it did not even "
                   f"start. It is left as it is: fix what health reports (systemctl start {oh_units.TARGET} if it "
                   "did not start), then run thermite health --production --installation to check again.")
    return ROLLED_BACK if healthy else UNHEALTHY


def health(system: System, transaction: Transaction) -> bool:
    """The reinstated version's own health, through the owner command it
    kept: the owner command is swapped only after a commit."""
    launcher = Path.home() / ".local/bin/thermite"
    if not os.access(launcher, os.X_OK):
        system.say(f"   Cannot check the reinstated version's health: {launcher} is not its owner command.")
        return False
    before = transaction.record.get("head_before")
    argv = (str(launcher), "--site", str(transaction.site), "health", "--production", "--installation",
            "--settle", str(SETTLE), *(("--revision", before) if before else ()))
    return runner.perform(Command(argv, show_output=True), system, transaction.site, root_confirmed=True) == 0


def resolve(site: Path, system: System, verb: str = "upgrade") -> int | None:
    """Finish an earlier transaction that has not finished, then stop
    (9-F8b): a committed one is finished, one that never began is closed
    and its pin commit undone, any other rolled back; a rollback finished
    by hand from rollback --print is checked by the engine and completed.
    None when there is none."""
    var = Path(site) / "var"
    id = open_transaction(var)
    if id is None:
        return None
    system.say(f"Transaction {id} of an earlier run did not finish; resolving it first.")
    try:
        transaction = Transaction.load(var, id)
        found = transaction.journal(system)
    except (OSError, ValueError) as exc:
        system.say(f"Cannot resolve it: {exc}. As root, {PYTHON} -I -S <engine.py> status --root <root> "
                   "lists the transactions (docs/installation.md#a-stuck-rollback).")
        return STUCK
    if found is None:
        system.say("   It never began: nothing of Thermite was changed.")
        code = refused(system, transaction)
    elif found["phase"] in ("committed", "kept"):
        if runner.perform(transaction.command("rollback"), system, transaction.site, root_confirmed=True) != 0:
            return _stuck(system, transaction)
        code = committed(system, transaction)
    elif found["phase"] == "rolled-back":
        code = rolled_back(system, transaction)
    else:
        code = roll_back(system, transaction, f"its {transaction.verb} did not finish")
    if code in (COMMITTED, REFUSED, ROLLED_BACK, UNHEALTHY):
        system.say(f"Stopped there, without {DOING.get(verb, verb)}; run {command(site, verb)} again to go on.")
    return code


def pinned(system: System, site: Path, before: str | None, dry_run: bool,
           body: Callable[[str | None], int]) -> int:
    """A host phase the application workflow approved after committing a
    pin (HEAD, from `before`). When it ends before its install transaction
    began, that pin commit is undone: the site pins the version that runs.
    A transaction undoes it itself when it rolls back. An earlier open
    transaction is resolved first and this run stops, so the pin commit is
    undone first.

    The version 1 wrapper commits its pin without naming `before`: on a
    host it installed, its pin commit is recognized instead (wrapper_pin).
    `body` runs the phase from `before`."""
    import oh_source_upgrade
    if before is None and not dry_run and bridge.needed(system):
        before = wrapper_pin(site)
    head = oh_source_upgrade.git(site, "rev-parse", "HEAD") if before else None
    if head and not dry_run and open_transaction(Path(site) / "var") is not None:
        _unpin(system, site, head, before)
        head = None
    code = 2
    try:
        code = body(before)
    except KeyboardInterrupt:
        code = INTERRUPTED
        raise
    finally:
        if head and code in (1, 2, INTERRUPTED) and open_transaction(Path(site) / "var") is None:
            _unpin(system, site, head, before)
    return code


def wrapper_pin(site: Path) -> str | None:
    """The commit before the version 1 wrapper's pin commit, when the
    site's HEAD is one for the release the site pins (bridge.site_before)."""
    import subprocess
    from oh_paths import descriptor
    try:
        found = descriptor(site)
        return bridge.site_before(_toplevel(site), site, (found or {}).get("release"))[0]
    except (OSError, ValueError, subprocess.CalledProcessError):
        return None


def _unpin(system: System, site: Path, head: str, before: str) -> None:
    reset_site(system, site, expected=head, previous=before,
               back=f"git -C {shlex.quote(str(site))} revert --no-edit {head[:12]}")


def reset_site(system: System, site: Path, *, expected: str, previous: str, back: str, indent: str = "") -> bool:
    """Move the site from the pin commit `expected` back to `previous` and
    say so; when that cannot be done safely, say how to finish (`back`:
    the git command that pins the version that runs instead). Whether the
    site pins `previous` now."""
    import subprocess
    import oh_source_upgrade
    try:
        moved = oh_source_upgrade.reset_pin(site, expected=expected, previous=previous)
    except oh_source_upgrade.PinUnfinished as exc:
        system.say(f"{indent}The site's branch is back at {previous[:12]}, but {exc}; once nothing else uses its "
                   f"Git repository, finish with: {exc.command}")
        return False
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        system.say(f"{indent}The site does not pin the version that runs ({exc}). To pin it: {back}")
        return False
    if moved:
        system.say(f"{indent}Site: back at {previous[:12]}, which pins the version that runs.")
    return True
