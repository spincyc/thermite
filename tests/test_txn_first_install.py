"""thermite install on a host Thermite never ran on, rolled back, then run
again: the rollback leaves only its own transaction under the installation
root, and the next install does not take that for an installation whose
manifest is missing. The service-account receipt is why: it is an item
like any other, so the same tests follow it through an upgrade from a
version that wrote it without declaring it.

The set the install steps declare, the directory steps' own commands, the
install engine and thermite install's own entry, all real; an in-memory disk
and host stand in for this one, so nothing runs as root or changes it. The
rest of the switch is a stand-in that writes every declared item and the
service-account receipt as lib/oh_service.py records it.
"""
from __future__ import annotations

from contextlib import ExitStack
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))

import oh_apparmor  # noqa: E402
import oh_deploy as deploy  # noqa: E402
import oh_service  # noqa: E402
from oh_cli import install as install_cli  # noqa: E402
from oh_install import bridge, changeover, engine, manifest, steps  # noqa: E402
from oh_install.model import Check, Step, Task  # noqa: E402
from oh_install.system import Stat, System, UnitFile, UnitOrigin  # noqa: E402
from tests import txn_fakes  # noqa: E402
from tests.test_deploy import HOST, MUSIC_HOST, VERSIONS  # noqa: E402

ROOT = Path(txn_fakes.ROOT)


class Fake(Step):
    """A step that is done once `outcome(system)` has returned 0; any other
    exit status stops the run there."""

    def __init__(self, title, outcome):
        self.title, self.outcome, self.done = title, outcome, False

    def check(self, system):
        return Check(self.done, "fixture")

    def actions(self, system):
        return [Task(self.title, self.perform)]

    def perform(self, system):
        code = self.outcome(system)
        self.done = code == 0
        return code


class Fixture(unittest.TestCase):
    """A host without Thermite, and thermite install run on it."""

    #: instance/host.yaml, as the fixture house has it.
    HOST = HOST

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.site = Path(temporary.name) / "site"
        (self.site / "var").mkdir(parents=True, mode=0o700)
        txn_fakes.in_scope(self, self.site, "install")
        host = {**self.HOST, "root": str(ROOT)}
        prepared = {name: deploy.Blob(text.encode()) for name, text in deploy.render_units(VERSIONS, host).items()}
        prepared["conf/services/addons.cfg"] = deploy.Blob(b"binding = http\n")
        self.context = steps.Context(self.site, VERSIONS, host, "b" * 40, "c" * 64, "fixture-host", "192.0.2.10",
                                     prepared=prepared, application={"version": "1.0", "digest": "d" * 64})
        self.disk = txn_fakes.MemoryDisk()
        for directory in (txn_fakes.UNITS, txn_fakes.WANTS, "/etc/containers/systemd", "/srv"):
            self.disk.put(directory, "dir", durable=True)
        self.disk.put(txn_fakes.APPARMOR, data=b"Y\n", durable=True)
        self.disk.settle()
        self.host = txn_fakes.FakeHost(self.disk)
        self.engine = engine.Engine(self.disk, self.host, str(ROOT), source=txn_fakes.SOURCE,
                                    clock=lambda: txn_fakes.CLOCK)
        self.log = []
        #: The exit status of the new version's health, each run's in turn.
        self.health = []
        self.receipt = self.context.layout.root / oh_service.RECEIPT
        #: Whether the version being installed declares the receipt, and
        #: the policy its ownership repair records there.
        self.declares_receipt, self.policy = True, oh_service.POLICY
        self.system = Mock(spec=System)
        self.system.interactive.return_value = True
        self.system.process_groups.return_value = {host["gid"]}
        self.system.unit_origins.side_effect = lambda names: {
            name: UnitOrigin(active="active" if name in self.host.active else "inactive") for name in names}
        self.system.fstype.return_value = "ext4"
        self.system.stat.side_effect = self.stat
        self.system.unit_file.side_effect = self.unit_file
        self.system.lexists.side_effect = lambda path: str(path) in self.disk.nodes
        self.system.directory_entries.side_effect = self.entries
        self.system.read_bytes.side_effect = self.read
        self.system.run.side_effect = self.command
        self.system.capture.side_effect = self.captured
        scope = ExitStack()
        self.addCleanup(scope.close)
        for target, name, value in ((manifest, "tools", {"return_value": txn_fakes.TOOLS}),
                                    (install_cli, "AREA", {"new": self.site}),
                                    (steps, "committed_context", {"return_value": self.context}),
                                    # The host's own preconditions: its root ones are asked below.
                                    (steps, "install_preflight", {"return_value": []}),
                                    (changeover, "install_work", {"side_effect": self.work}),
                                    # The owner command in the real home directory is not this fixture's.
                                    (changeover, "health", {"side_effect": self.reinstated})):
            scope.enter_context(patch.object(target, name, **value))

    # The host, as the operator reads it --------------------------------------

    def stat(self, path):
        node = self.disk.nodes.get(str(path))
        return None if node is None else Stat(node.uid, node.gid, node.mode, node.kind == "dir", 0.0, len(node.data))

    def unit_file(self, path):
        node = self.disk.nodes.get(str(path))
        return UnitFile(node.data, node.uid, node.mode, True, node.gid) if node and node.kind == "file" else None

    def entries(self, path):
        try:
            return self.disk.listdir(path)
        except FileNotFoundError:
            return None

    def read(self, path):
        node = self.disk.nodes.get(str(path))
        return node.data if node is not None and node.kind == "file" else None

    # Its commands ------------------------------------------------------------

    def verb(self, argv):
        """An engine verb, with the exit status the engine's own main gives it."""
        options = engine.parser().parse_args(argv)
        self.assertEqual(options.root, str(ROOT))
        self.log.append(options.verb)

        def read(path):
            return json.loads(Path(path).read_text())

        try:
            self.engine.check_root()
            if options.verb in ("plan", "begin"):
                self.assertIsNone(options.current, "no installation here is described without a manifest")
            if options.verb == "plan":
                return 0, json.dumps(self.engine.plan(read(options.desired)))
            if options.verb == "begin":
                self.engine.begin(options.id, read(options.desired), read(options.operator),
                                  release=read(options.release) if options.release else None)
                return 0, ""
            if options.verb == "status":
                return 0, json.dumps(self.engine.status())
            journal = getattr(self.engine, options.verb)(options.id)
        except engine.EngineError as exc:
            self.log.append(f"refused: {exc}")
            return engine.FAILED, ""
        if options.verb == "rollback":
            return {"kept": 0, "stuck": engine.STUCK}.get(journal.phase, engine.ROLLED_BACK), ""
        return 0, ""

    def command(self, argv, cwd):
        argv = tuple(map(str, argv))
        if argv == ("sudo", "-v"):
            return 0
        if argv[1:4] == (changeover.PYTHON, "-I", "-S"):
            return self.verb(argv[5:])[0]
        if "--_record-rollback" in argv:
            self.log.append("history")
        elif argv[:3] == ("sudo", "systemctl", "stop"):
            return self.host.run(argv[1:])
        elif argv[:2] == ("sudo", "mkdir"):
            self.disk.put(argv[-1], "dir", mode=0o755)
        elif argv[:3] == ("sudo", "install", "-d") and argv[3:9:2] == ("-o", "-g", "-m"):
            owner, group, mode = argv[4:9:2]
            for path in argv[9:]:
                self.disk.put(path, "dir", mode=int(mode, 8), uid=0 if owner == "root" else int(owner),
                              gid=0 if group == "root" else int(group))
        else:
            raise AssertionError(f"unexpected command {argv}")
        return 0

    def captured(self, argv, **kwargs):
        argv = tuple(map(str, argv))
        self.assertEqual(argv[2:5], (changeover.PYTHON, "-I", "-S"))
        return self.verb(argv[6:])

    def reinstated(self, system, transaction):
        """The health of the version a rollback put back, as its owner
        command answers."""
        self.log.append("health of the version put back")
        return True

    # The install ---------------------------------------------------------------

    def switched(self, items, id):
        """The rest of the switch: every declared item written (the gateway's
        release is the child the transaction names), the receipt whatever
        declares it, the live state loaded and Thermite started."""
        def perform(system):
            owner = {"uid": self.context.host["uid"], "gid": self.context.host["gid"]}
            for item in items(system):
                path, state = str(item.path), owner if engine.below(item.path, ROOT) else {}
                if item.cls == "link":
                    self.disk.put(path, "link", target=str(item.target))
                elif engine.CLASSES[item.cls].shape == "file":
                    self.disk.put(path, data=b"fixture\n", **state)
                else:
                    if path not in self.disk.nodes:
                        self.disk.put(path, "dir", mode=0o750, **state)
                    self.disk.put(f"{path}/{id}/config.json" if item.cls == "versioned" else f"{path}/fixture",
                                  data=b"fixture\n", **state)
            self.disk.put(self.receipt, mode=oh_service.SHARED_FILE, **owner,
                          data=(json.dumps({"version": oh_service.RECEIPT_VERSION, "policy": self.policy, **owner},
                                           sort_keys=True) + "\n").encode())
            for argv in (("systemctl", "daemon-reload"), ("systemctl", "restart", txn_fakes.BOUNDARY),
                         ("apparmor_parser", "-r", str(oh_apparmor.POLICY)), ("systemctl", "start", engine.TARGET)):
                self.assertEqual(self.host.run(argv), 0, argv)
            return 0
        return perform

    def work(self, context, id, **kind):
        """changeover.install_work's, with the real steps where the disk can
        follow them: the installation root, the state directories, and what
        every switching step declares."""
        new = changeover.switching(context, id)

        def items(system):
            return [item for item in manifest.desired(new, system, ROOT)
                    if self.declares_receipt or item.path != self.receipt]

        root = [step for step in changeover.preparation(context) if type(step) is steps.InstallationRoot]
        directories = [step for step in new if isinstance(step, (steps.StateDirectories, steps.MetricsDirectories))]
        self.assertEqual((len(root), len(directories)), (1, 3 if "music" in context.host else 2))
        return changeover.Work("install", "install", root, [*directories, Fake("the rest of the switch",
                                                                                self.switched(items, id))],
                               [changeover.Authorize(), Fake("health", lambda system: self.health.pop(0))],
                               changeover.release(context), items=items, pull=False)

    def install(self, health):
        self.health.append(health)
        self.log.clear()
        return install_cli.install(self.system, False, run_approved=True)

    def recorded(self):
        """The paths the installed manifest lists."""
        return [item["path"] for item in manifest.installed(self.system, ROOT)["items"]]

    def below_root(self):
        """Everything under the installation root but the engine's transactions."""
        return sorted(name for name in self.disk.nodes if txn_fakes.inside(name, str(ROOT)) and name != str(ROOT)
                      and not txn_fakes.inside(name, str(ROOT / steps.TRANSACTIONS)))


class FirstInstall(Fixture):
    def test_a_first_install_that_rolled_back_runs_again_and_commits(self):
        self.assertEqual(self.install(health=1), changeover.ROLLED_BACK, self.log)
        self.assertEqual(self.log, ["plan", "begin", "guard", "collect", "rollback", "history"])
        self.assertEqual(self.entries(ROOT), [steps.TRANSACTIONS], "only what the engine set aside is left")
        self.assertEqual(self.below_root(), [])
        self.assertIsNone(manifest.installed(self.system, ROOT))
        self.assertFalse(bridge.needed(self.system))
        self.assertEqual(manifest.thermite_present(self.context, self.system), [])
        # What steps.Preconditions asks of the root. Nothing is left under
        # runtime/, which an owner not yet logged in again for the thermite
        # group could not read to prove it Thermite's.
        self.assertTrue(steps.installation_owned(self.context, self.system))
        self.assertTrue(steps.state_owned(self.context, self.system))

        self.assertEqual(self.install(health=0), changeover.COMMITTED, self.log)
        self.assertEqual(self.log[-1], "commit")
        self.assertIn(str(self.receipt), self.recorded())
        self.assertTrue(self.host.enabled())
        self.assertIn(ROOT, manifest.thermite_present(self.context, self.system), "an installation is still found")


class FirstInstallWithMusic(FirstInstall):
    """The same on a host that runs Music Assistant, whose state directory
    the install makes too."""

    HOST = MUSIC_HOST


class Upgrade(Fixture):
    def test_an_upgrade_from_a_version_that_did_not_declare_the_receipt_sets_it_aside(self):
        # The installed version wrote the receipt and never listed it.
        self.declares_receipt = False
        self.assertEqual(self.install(health=0), changeover.COMMITTED, self.log)
        self.assertNotIn(str(self.receipt), self.recorded())
        before = self.disk.nodes[str(self.receipt)]

        # This version rewrites it, then fails: the old one's is put back.
        self.declares_receipt, self.policy = True, "a-later-policy"
        self.assertEqual(self.install(health=1), changeover.ROLLED_BACK, self.log)
        self.assertEqual(self.log[-3:], ["rollback", "history", "health of the version put back"])
        self.assertEqual(self.disk.nodes[str(self.receipt)], before)
        self.assertNotIn(str(self.receipt), self.recorded())
        self.assertTrue(self.host.enabled())

        self.assertEqual(self.install(health=0), changeover.COMMITTED, self.log)
        self.assertIn(str(self.receipt), self.recorded())
        self.assertIn(b"a-later-policy", self.disk.nodes[str(self.receipt)].data)


if __name__ == "__main__":
    unittest.main()
