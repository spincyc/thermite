"""thermite upgrade as one install transaction.

The upgrade first prepares everything it can while Thermite keeps running:
the configuration, packages, the boot test, the pinned images, and the room
the install engine (lib/oh_install/engine.py, run as root) needs to set the
installed version aside. Then the engine begins the transaction, guards the
boot (thermite.target stays disabled), and once Thermite is stopped collects
the installed version. The new version is switched in by the ordinary
install steps and verified by its health; only then does the engine commit
it. A failure from begin on rolls the transaction back in the same run and
the version that ran before runs again. The runner never compensates inside
a transaction: the rollback is the only undo.

The operator keeps its own record of each transaction in the site's
var/transaction/<id>/: record.json, the inputs it handed the engine, the
operator files it set aside (F3) and the gateway's attempt record.
var/transaction/open names a transaction that has not finished; the next
thermite upgrade finishes or rolls it back first, then stops and says so,
and thermite status names it.
"""
from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path
import shlex
import shutil

import oh_units
from oh_install import bridge, engine, manifest, ownership, runner, steps
from oh_install.ingress import NativeIngress, NativeIngressPrerequisites
from oh_install.model import Check, Command, Step, Task
from oh_install.system import System

#: thermite upgrade's exit statuses for the transaction's outcomes.
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
    """The id of the upgrade transaction that has not finished, if any."""
    try:
        value = (transactions(var) / OPEN).read_text().strip()
    except FileNotFoundError:
        return None
    return value or None


def upgrade_command(site: Path) -> str:
    return f"thermite upgrade --config {shlex.quote(str(site))}"


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
    """One upgrade's transaction, as the operator records it."""

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

    def argv(self, verb: str, *args) -> tuple[str, ...]:
        named = ("--id", self.id) if verb not in ("plan", "status") else ()
        return (PYTHON, "-I", "-S", str(self.engine), verb, "--root", str(self.root), *named, *map(str, args))

    def command(self, verb: str, *args) -> Command:
        return Command(self.argv(verb, *args), root=True, terminal=True)

    def save(self, **changes) -> None:
        self.record.update(changes)
        _json(self.file("record.json"), self.record)

    @classmethod
    def create(cls, context: steps.Context, id: str, *, before: str | None, after: str | None,
               previous: str | None, gid: int) -> Transaction:
        transaction = cls(context.var, id, {
            "version": 1, "id": id, "site": str(context.area), "root": str(context.installation.root),
            "state": str(context.layout.root), "gid": gid, "head_before": before, "head_after": after,
            "application": {"current": (context.application or {}).get("digest"), "previous": previous},
            "asides": {}, "phase": "preparing"})
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
        """Undo the upgrade's pin commit, so the site pins the version that
        runs; say so when that cannot be done safely."""
        import oh_source_upgrade
        before, after = self.record.get("head_before"), self.record.get("head_after")
        site = shlex.quote(str(self.site))
        if before is None:
            system.say("   The site may still pin the new release: its revision before the upgrade is not known. "
                       f"Review `git -C {site} log -2` and revert the pin commit if it is there.")
            return
        if before == after:
            return
        try:
            moved = oh_source_upgrade.reset_pin(self.site, expected=after, previous=before)
        except oh_source_upgrade.PinMoved as exc:
            system.say(f"   The site still pins the new release: {exc}. To pin the version that runs: "
                       f"git -C {site} revert --no-edit {after[:12]}")
            return
        except (OSError, ValueError) as exc:
            system.say(f"   The site still pins the new release ({exc}). To pin the version that runs: "
                       f"git -C {site} revert --no-edit {after[:12]}")
            return
        if moved:
            system.say(f"   Site: back at {before[:12]}; the pin commit {after[:12]} is no longer on its branch.")

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
    stops; too little refuses the upgrade with the table (9-Q15)."""

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


# The upgrade -----------------------------------------------------------------

def release(context: steps.Context) -> dict:
    """What commit records about this version."""
    return {"release": {"site": context.head, "configuration": context.conf_digest,
                        "hostname": context.hostname, "address": context.address},
            "pins": manifest.pins(context), "images": manifest.images(context),
            "application": (context.application or {}).get("digest")}


def preparation(context: steps.Context) -> list[Step]:
    """What the transaction needs that is the host's, never an item: done
    while Thermite runs."""
    return [*steps.configuration_steps(context), steps.Packages(), NativeIngressPrerequisites(context),
            steps.AppArmorService(), steps.BootTest(context), steps.Account(context)]


def switching(context: steps.Context, transaction: str | None) -> list[Step]:
    """The new version, installed over the collected one, and started."""
    native = steps.NativeDeadline.of(context)
    deployment = steps.Deploy(context)
    deployment.how = "upgrade"
    return [steps.StateDirectories(context), steps.MetricsDirectories(context), steps.Units(context),
            steps.NodeConfinement(context), steps.NativePreparation(context), NativeIngress(context), deployment,
            steps.Services(context), steps.Answers(context, native), steps.Token(context, native),
            *steps.gateway_steps(context, native, transaction=transaction), steps.MetricsToken(context),
            steps.GrafanaPassword(context), steps.MetricsServices(context), steps.StartTarget(context),
            steps.NativeReady(context, native)]


def verification(context: steps.Context) -> list[Step]:
    return [Authorize(), steps.MetricsAnswer(context), steps.Health(context)]


def operator_files(context: steps.Context) -> list[Path]:
    return [context.area / context.token_file, context.area / context.grafana_password_file,
            context.area / context.runtime_credentials_file, context.var / "gateway/current.json"]


def unchanged(context: steps.Context, system: System, installed: dict | None) -> bool:
    """Whether the installed version is this one, as recorded and as found:
    then the upgrade only verifies it."""
    if installed is None:
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


def preview(context: steps.Context, system: System, *, first: int) -> int:
    """What a dry run shows: the preparation's checks, then the transaction."""
    code = runner.run(preparation(context), system, context.area, dry_run=True, first=first)
    titles = ", ".join(step.title for step in switching(context, None))
    system.say(f"Then, as one install transaction: pull the pinned images, check the room to set the installed "
               f"version aside, begin, guard the boot, stop Thermite and set it aside; install and start the new "
               f"version ({titles}); verify its health and commit. Any failure from begin on rolls back in the "
               "same run and starts the installed version again.")
    return code


def upgrade(context: steps.Context, system: System, *, installed: dict | None, bridged: bool,
            before: str | None, first: int) -> int:
    """Prepare, begin, switch, verify and commit; roll back on failure."""
    after = context.head
    if before is None and bridged:
        # bridge: the version 1 wrapper committed the pin without saying so.
        before, after = bridge.site_before(_toplevel(context.area), context.area,
                                           (context.application or {}).get("digest"))
    transaction = Transaction.create(context, manifest.transaction_id(), before=before, after=after,
                                     previous=previous_application(installed, system), gid=context.host["gid"])
    prefetch = Prefetch(context)
    root = context.installation.root
    changes = switching(context, transaction.id)

    def inputs(system: System) -> None:
        tools = manifest.tools(system)
        items = manifest.desired(changes, system, root)
        _json(transaction.file("desired.json"), {
            "kind": "upgrade", "tools": tools, "items": [item.to_json() for item in items],
            "pulled": prefetch.pulled, "extra": _allocated(context.program)})
        if bridged:
            _json(transaction.file("current.json"),
                  bridge.describe(context, system, items, tools, engine._now()))
        _json(transaction.file("operator.json"), {
            "uid": os.getuid(), "site": str(context.area), "record": str(transaction.file("record.json")),
            "head_before": transaction.record["head_before"], "head_after": transaction.record["head_after"]})
        _json(transaction.file("release.json"), release(context))

    opening = [*preparation(context), prefetch, Room(transaction, inputs), Begin(transaction, operator_files(context))]
    rerun = f"Nothing of Thermite was stopped or changed. Fix that, then run {upgrade_command(context.area)} again."
    try:
        code = runner.run(opening, system, context.area, first=first, rerun=rerun, root_confirmed=True)
    except KeyboardInterrupt:
        return _interrupted(system, transaction)
    except (OSError, ValueError) as exc:
        system.say(f"Stopped: {exc}. {rerun}")
        code = REFUSED
    if code:
        return refused(system, transaction)
    rest = [Engine(transaction, "guard", "Guard the boot", f"{oh_units.TARGET} stays disabled until the commit"),
            steps.Halt(context),
            Engine(transaction, "collect", "Set the installed version aside", "copies and moves, as the plan says"),
            *changes, *verification(context),
            Engine(transaction, "commit", "Commit", "record the new version as installed",
                   "--release", transaction.file("release.json"))]
    failed = f"The upgrade rolls back transaction {transaction.id} now."
    try:
        code = runner.run(rest, system, context.area, first=first + len(opening), rerun=failed,
                          root_confirmed=True, transactional=True)
    except KeyboardInterrupt:
        return _interrupted(system, transaction)
    except Exception as exc:  # Every failure rolls back; none leaves a half-installed version.
        system.say(f"   {type(exc).__name__}: {exc}")
        code = REFUSED
    if code:
        return roll_back(system, transaction, "a step failed")
    return committed(system, transaction)


def _toplevel(site: Path) -> Path:
    from oh_source_upgrade import git
    return Path(git(site, "rev-parse", "--show-toplevel"))


def refused(system: System, transaction: Transaction) -> int:
    """Nothing began: the site goes back to pinning the version that runs."""
    transaction.unpin(system)
    transaction.close("refused")
    system.say("Upgrade refused: nothing of Thermite was stopped or changed.")
    return REFUSED


def _interrupted(system: System, transaction: Transaction) -> int:
    site = transaction.site
    if open_transaction(transaction.var) == transaction.id:
        system.say(f"\nInterrupted. Transaction {transaction.id} is open: Thermite may be stopped, with "
                   f"{oh_units.TARGET} kept from booting. Run {upgrade_command(site)} to roll it back.")
    else:
        system.say(f"\nInterrupted before the transaction began: nothing of Thermite was stopped or changed. "
                   f"Run {upgrade_command(site)} again.")
        transaction.unpin(system)
        transaction.close("refused")
    return INTERRUPTED


def committed(system: System, transaction: Transaction) -> int:
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
    system.say(f"Upgrade committed: transaction {transaction.id}. The previous version stays set aside under "
               f"{transaction.root}/.transaction/{transaction.id} until the next upgrade commits.")
    return COMMITTED


def roll_back(system: System, transaction: Transaction, reason: str) -> int:
    """Put the version that ran before back, in this run, and say how it is."""
    system.say(f"Rolling back transaction {transaction.id} ({reason}): the installed version is put back and "
               "started again.")
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
               f"{oh_units.TARGET} is kept from booting. Run {upgrade_command(transaction.site)} to try again. "
               f"To roll back by hand instead, print the commands with: sudo {PYTHON} -I -S {transaction.pinned} "
               f"rollback --root {transaction.root} --id {transaction.id} --print "
               "(docs/installation.md#a-stuck-rollback)")
    return STUCK


def rolled_back(system: System, transaction: Transaction) -> int:
    """The operator's side of a finished rollback: its own files, the site
    pin, the history; then the reinstated version's own health (9-Q10)."""
    transaction.put_back()
    transaction.unpin(system)
    record = transaction.record
    if record.get("head_after"):
        event = {"transaction": transaction.id, "commit": record["head_after"], "restored": record.get("head_before")}
        argv = _application(record, "history", "--state", record["state"], "--_record-rollback",
                            json.dumps(event, sort_keys=True))
        if runner.perform(_operator(record, system, argv), system, transaction.site, root_confirmed=True) != 0:
            system.say("   The rollback was not added to thermite history.")
    transaction.close("rolled-back")
    healthy = health(system, transaction)
    if healthy:
        system.say(f"Rolled back: transaction {transaction.id} is undone and the version that ran before runs "
                   "again, healthy.")
        return ROLLED_BACK
    system.say(f"Rolled back: transaction {transaction.id} is undone and the version that ran before is back, "
               "but its health check does not pass (above); the engine says so above when it did not even start. "
               f"It is left as it is: fix what health reports (systemctl start {oh_units.TARGET} if it did not "
               "start), then run thermite health --production --installation to check again.")
    return UNHEALTHY


def health(system: System, transaction: Transaction) -> bool:
    """The reinstated version's own health, through the owner command it
    kept: the upgrade swaps that only after a commit."""
    launcher = Path.home() / ".local/bin/thermite"
    if not os.access(launcher, os.X_OK):
        system.say(f"   Cannot check the reinstated version's health: {launcher} is not its owner command.")
        return False
    before = transaction.record.get("head_before")
    argv = (str(launcher), "--site", str(transaction.site), "health", "--production", "--installation",
            "--settle", str(SETTLE), *(("--revision", before) if before else ()))
    return runner.perform(Command(argv, show_output=True), system, transaction.site, root_confirmed=True) == 0


def resolve(site: Path, system: System) -> int | None:
    """Finish an earlier upgrade's transaction that has not finished, then
    stop (9-F8b): a committed one is finished, one that never began is
    closed and its pin commit undone, any other rolled back. None when
    there is none."""
    var = Path(site) / "var"
    id = open_transaction(var)
    if id is None:
        return None
    system.say(f"Transaction {id} of an earlier upgrade did not finish; resolving it first.")
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
        code = roll_back(system, transaction, "its upgrade did not finish")
    if code in (COMMITTED, REFUSED, ROLLED_BACK, UNHEALTHY):
        system.say(f"Stopped there, without upgrading; run {upgrade_command(transaction.site)} again to upgrade.")
    return code
