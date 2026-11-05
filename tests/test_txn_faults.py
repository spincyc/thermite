"""Kill an install transaction anywhere, rerun it, and find Thermite whole.

Each scenario runs begin, guard, the operator's stop, collect, the new
version's install and commit (or a failed gate's rollback) on a MemoryDisk
and FakeHost. The run is then killed before each of its mutating calls in
turn: every journal write, every disk change or sync, every host command and
every write of the new version. After a kill the process is simply gone;
after a power cut, only what was synced survives and the host boots; after a
torn power cut, any of the unsynced changes may survive too, copies and
removals in part. The
rerun is what changeover's recovery does: `rollback`, which finishes a
committed transaction and undoes any other. The managed set must then be
byte for byte the version before or the committed one, never a mix.
"""
from __future__ import annotations

import copy
import dataclasses
import errno
import random
from pathlib import Path
import sys
import unittest

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))

from oh_install import engine
from tests import txn_fakes as fakes
from tests.txn_fakes import ID, ROOT, FakeHost, MemoryDisk, inside


@dataclasses.dataclass
class Case:
    old: fakes.Version | None
    new: fakes.Version
    kind: str = "upgrade"
    apparmor: bool = True
    mounts: dict = dataclasses.field(default_factory=dict)
    bridge: bool = False
    purge: bool = False
    switch: bool = True

    def prepare(self, disk, host):
        if self.old is None:
            fakes.empty(disk, apparmor=self.apparmor)
            self.current = None
            return
        self.current = fakes.install(disk, host, self.old, apparmor=self.apparmor, manifest=not self.bridge)

    def desired(self):
        return {"kind": self.kind, "tools": fakes.TOOLS, "items": fakes.desired_items(self.new, purge=self.purge),
                "pulled": self.new.images}

    def paths(self):
        versions = [version for version in (self.old, self.new) if version]
        return fakes.managed_paths(*versions, pinned=len({version.pins.get(fakes.PROMETHEUS) for version in versions} - {None}) > 1)


def bridged():
    old = fakes.version("old")
    old.files["/etc/thermite/units.json"] = ("file", b'{"version": 1}\n', {})
    return old


def gone():
    return dataclasses.replace(fakes.version("gone", shared=True), files={}, links={})


def nothing():
    return fakes.Version({}, {}, {}, {}, [], "nothing")


CASES = {
    "fresh_install": Case(None, fakes.version("new"), kind="install"),
    "upgrade_same_pins": Case(fakes.version("old"), fakes.version("new")),
    "upgrade_prometheus_pin": Case(fakes.version("old"), fakes.version("new", prometheus="two")),
    "upgrade_removes_unit_and_adds_unit": Case(
        fakes.version("old", extra_units=("thermite-old.service", "thermite-old.path")),
        fakes.version("new", extra_units=("thermite-fresh.service",),
                      dropins=("thermite-openhab.service.d/50-lifecycle.conf",))),
    "upgrade_apparmor_disabled": Case(fakes.version("old"), fakes.version("new"), apparmor=False),
    "upgrade_across_filesystems": Case(
        fakes.version("old", extra_units=("thermite-old.service", "thermite-old.path"), apparmor=True),
        fakes.version("new", apparmor=False), mounts={"/etc": "etc", "/srv": "srv"}),
    "uninstall": Case(fakes.version("old"), gone(), kind="uninstall", switch=False),
    # The desired set lists nothing: the engine keeps all state itself.
    "uninstall_listing_nothing": Case(fakes.version("old"), nothing(), kind="uninstall", switch=False),
    "purge": Case(fakes.version("old"), gone(), kind="purge", purge=True, switch=False),
    "clean": Case(fakes.version("old"), fakes.version("new", tree_class="tree-move"), kind="clean"),
    "bridge_first_upgrade": Case(bridged(), fakes.version("new"), bridge=True),
}


PREPARED = {}


def make(case):
    """A fresh copy of the case's starting host."""
    if id(case) not in PREPARED:
        disk = MemoryDisk(mounts=case.mounts)
        host = FakeHost(disk)
        case.prepare(disk, host)
        PREPARED[id(case)] = (disk, host)
    return copy.deepcopy(PREPARED[id(case)])


def engine_on(disk, host, messages=None):
    return engine.Engine(disk, host, ROOT, source=fakes.SOURCE, clock=lambda: fakes.CLOCK,
                         say=(messages.append if messages is not None else None))


def transaction(disk, host, case, *, gate=True):
    """One transaction as changeover runs it; `gate` False fails the gate."""
    run = engine_on(disk, host)
    run.begin(ID, case.desired(), fakes.OPERATOR, current=case.current if case.bridge else None)
    run.guard(ID)
    assert host.run(("systemctl", "stop", engine.TARGET, engine.SLICE)) == 0  # Quiesce
    run.collect(ID)
    if case.switch:
        fakes.switch(disk, host, case.new)
    if gate:
        run.commit(ID, fakes.release(case.new) if case.switch else None)
    else:
        run.rollback(ID)


def durable_phase(disk, host, case=None):
    journal = engine_on(disk, host).read(ID)
    if journal is None and case is not None and case.kind == "purge" and "/etc/thermite/installed.json" not in (
            disk.nodes):
        return "kept"  # Past the commit point, removing its own transaction.
    return journal.phase if journal else None


def recover(disk, host):
    """What a rerun does with the open transaction."""
    engine_on(disk, host).sweep()
    if engine_on(disk, host).read(ID) is None:
        return None
    return engine_on(disk, host).rollback(ID)


class Harness(unittest.TestCase):
    def expect(self, case, *, gate=True):
        """The world before and after an uninterrupted run, and its call count."""
        disk, host = make(case)
        before = fakes.world(disk, host, case.paths())
        transaction(disk, host, case, gate=gate)
        after = fakes.world(disk, host, case.paths())
        return before, (after if gate else before), disk.count

    def assert_whole(self, case, disk, host, phase, before, after, what):
        expected = after if phase in ("committed", "kept") else before
        found = fakes.world(disk, host, case.paths())
        if found != expected:
            files = sorted(set(found["files"]) ^ set(expected["files"]) | {
                path for path in set(found["files"]) & set(expected["files"])
                if found["files"][path] != expected["files"][path]})
            layout = sorted(set(found["layout"].items()) ^ set(expected["layout"].items()))
            self.fail(f"{what}: durable phase {phase}: differs in {files[:6]} layout {layout[:6]} "
                      f"host {found['host'] if found['host'] != expected['host'] else 'same'}")
        self.assertEqual([path for path in disk.nodes if path.endswith(".partial")], [], what)
        journal = engine_on(disk, host).read(ID)
        if journal is not None:
            self.assertIn(journal.phase, engine.TERMINAL, what)
            self.assertTrue(engine_on(disk, host).finished(ID), what)
        if case.kind == "purge" and phase in ("committed", "kept"):
            self.assertEqual([path for path in disk.nodes if inside(path, f"{ROOT}/.transaction") or inside(
                path, "/etc/thermite")], [], f"{what}: a purge leaves nothing")


class CrashAnywhere(Harness):
    def run_case(self, name, *, gate=True, nested=False):
        """Kill the run before each of its calls, with and without a power
        loss, recover, and check; with `nested`, also kill the recovery from
        the first kill point of each phase that reinstates or finishes, at
        each of the recovery's own calls."""
        case = CASES[name]
        before, after, total = self.expect(case, gate=gate)
        if gate:
            self.assertNotEqual(before, after)
        disk, host = make(case)
        points = fakes.kill_points(disk, host, lambda disk, host: transaction(disk, host, case, gate=gate))
        self.assertEqual(len(points), total)
        first = {}
        for k, (call, killed, killed_host) in enumerate(points, 1):
            for power in (False, True, "torn"):
                disk, host = fakes.fork(killed, killed_host)
                what = f"{name}: killed before call {k} {call}{' and power lost' if power else ''}"
                if power:
                    # Torn: unsynced changes survive at random (seeded by k).
                    disk.crash(random.Random(k) if power == "torn" else None)
                    host.boot()
                phase = durable_phase(disk, host, case)
                if power and phase in ("guarded", "collecting", "switching"):
                    self.assertFalse(host.state()["target"] or host.state()["members"],
                                     f"{what}: Thermite booted with the guard on")
                if not power:
                    first.setdefault(phase, (k, *fakes.fork(disk, host)))
                recover(disk, host)
                self.assert_whole(case, disk, host, phase, before, after, what)
        if nested:
            for phase, (k, disk, host) in sorted(first.items(), key=lambda item: item[1][0]):
                if phase not in ("collecting", "switching", "committed", "rolling-back"):
                    continue  # Recovering these only re-enables and starts the target.
                inner = fakes.kill_points(disk, host, recover)
                for j, (call, killed, killed_host) in enumerate(inner, 1):
                    for again in (False, True):
                        disk, host = fakes.fork(killed, killed_host)
                        if again:
                            disk.crash()
                            host.boot()
                        recover(disk, host)
                        self.assert_whole(case, disk, host, phase, before, after,
                                          f"{name}: killed at {k}, then in recovery "
                                          f"before {j} {call}{' (power)' if again else ''}")
        return total

    def test_fresh_install(self):
        self.run_case("fresh_install")

    def test_upgrade_same_pins(self):
        self.run_case("upgrade_same_pins", nested=True)

    def test_upgrade_prometheus_pin(self):
        self.run_case("upgrade_prometheus_pin")

    def test_upgrade_removes_unit_and_adds_unit(self):
        self.run_case("upgrade_removes_unit_and_adds_unit")

    def test_upgrade_apparmor_disabled(self):
        self.run_case("upgrade_apparmor_disabled")

    def test_upgrade_across_filesystems(self):
        self.run_case("upgrade_across_filesystems")

    def test_uninstall(self):
        self.run_case("uninstall")

    def test_uninstall_listing_nothing(self):
        self.run_case("uninstall_listing_nothing")

    def test_purge(self):
        self.run_case("purge")

    def test_clean(self):
        self.run_case("clean")

    def test_bridge_first_upgrade(self):
        self.run_case("bridge_first_upgrade")

    def test_a_failed_gate_rolls_back_and_survives_a_crash_anywhere_in_it(self):
        for name in ("upgrade_same_pins", "fresh_install", "upgrade_across_filesystems"):
            with self.subTest(name):
                self.run_case(name, gate=False, nested=name == "upgrade_same_pins")


class Faults(Harness):
    def test_moves_across_filesystems_copy_then_remove(self):
        case = CASES["upgrade_across_filesystems"]
        disk, host = make(case)
        before = fakes.world(disk, host, case.paths())
        run = engine_on(disk, host)
        run.begin(ID, case.desired(), fakes.OPERATOR)
        run.guard(ID)
        host.run(("systemctl", "stop", engine.TARGET, engine.SLICE))
        run.collect(ID)
        directory = f"{ROOT}/.transaction/{ID}"
        moved = [entry for entry in run.read(ID).entries if entry.action == "move"]
        self.assertEqual(sorted(entry.path for entry in moved),
                         ["/etc/apparmor.d/thermite-node-exporter", "/etc/systemd/system/thermite-old.path",
                          "/etc/systemd/system/thermite-old.service",
                          "/etc/systemd/system/thermite.target.wants/thermite-old.path"])
        for entry in moved:
            aside = f"{directory}/{entry.aside}"
            # The rename fails with EXDEV; a durable copy, then the removal.
            calls = [("rename", entry.path, aside), ("copy", entry.path, str(engine.partial(aside))),
                     ("sync_fs", str(engine.partial(aside))), ("rename", str(engine.partial(aside)), aside),
                     ("remove_tree", entry.path)]
            found = [disk.calls.index(call) for call in calls]
            self.assertEqual(found, sorted(found), entry.path)
        fakes.switch(disk, host, case.new)
        self.assertEqual(run.rollback(ID).phase, "rolled-back")
        self.assertEqual(fakes.world(disk, host, case.paths()), before)

    def test_a_full_disk_during_collect_moves_nothing_live(self):
        case = CASES["upgrade_prometheus_pin"]
        big = {"userdata": 4 << 20, "state": 2 << 20, "prometheus": 6 << 20}
        for victim in big:
            with self.subTest(victim):
                disk, host = make(case)
                disk.put(f"{ROOT}/runtime/userdata/cache.bin", data=b"u" * big["userdata"], durable=True)
                disk.put(f"{ROOT}/access/state/blob", data=b"s" * big["state"], durable=True)
                disk.put(f"{ROOT}/runtime/monitoring/prometheus/chunks/big", data=b"p" * big["prometheus"],
                         durable=True)
                before = fakes.world(disk, host, case.paths())
                run = engine_on(disk, host)
                journal = run.begin(ID, case.desired(), fakes.OPERATOR)
                run.guard(ID)
                host.run(("systemctl", "stop", engine.TARGET, engine.SLICE))
                # Another writer fills the disk after the up-front check.
                copies = [entry for entry in journal.entries if entry.action == "copy"]
                index = next(n for n, entry in enumerate(copies) if entry.path.endswith(victim))
                room = sum(disk.allocated(entry.path) for entry in copies[:index]) + disk.allocated(
                    copies[index].path) // 2
                disk.budget["root"] = disk.used("root") + room + (1 << 20)
                with self.assertRaises(OSError) as raised:
                    run.collect(ID)
                self.assertEqual(raised.exception.errno, errno.ENOSPC)
                journal = run.read(ID)
                self.assertEqual(journal.phase, "collecting")
                self.assertEqual({entry.state for entry in journal.entries if entry.action == "move"} - {"planned"},
                                 set())
                self.assertEqual([entry.state for entry in journal.entries if entry.path.endswith(victim)],
                                 ["partial"])
                del disk.budget["root"]
                self.assertEqual(run.rollback(ID).phase, "rolled-back")
                self.assertEqual(fakes.world(disk, host, case.paths()), before)
                self.assertEqual([path for path in disk.nodes if path.endswith(".partial")], [])

    def test_too_little_room_refuses_before_anything_changes(self):
        case = CASES["upgrade_same_pins"]
        disk, host = make(case)
        disk.budget["root"] = disk.used("root") + 1024
        with self.assertRaises(engine.SpaceShort) as raised:
            engine_on(disk, host).begin(ID, case.desired(), fakes.OPERATOR)
        self.assertNotIn(f"{ROOT}/.transaction", disk.nodes)
        self.assertEqual(disk.count, 0)
        self.assertIn("Not enough space: nothing was stopped or changed.", str(raised.exception))

    def test_a_failed_reinstatement_is_stuck_and_starts_nothing(self):
        case = CASES["upgrade_same_pins"]
        disk, host = make(case)
        before = fakes.world(disk, host, case.paths())
        run = engine_on(disk, host)
        run.begin(ID, case.desired(), fakes.OPERATOR)
        run.guard(ID)
        host.run(("systemctl", "stop", engine.TARGET, engine.SLICE))
        run.collect(ID)
        fakes.switch(disk, host, case.new)
        host.failures[("systemctl", "daemon-reload")] = 1
        start = len(host.log)
        journal = run.rollback(ID)
        self.assertEqual(journal.phase, "stuck")
        self.assertEqual(journal.outcome["error"], "systemctl daemon-reload failed")
        self.assertFalse(host.enabled())
        self.assertFalse(host.state()["target"] or host.state()["members"])
        self.assertNotIn(("systemctl", "start", engine.TARGET), host.log[start:])
        del host.failures[("systemctl", "daemon-reload")]
        self.assertEqual(run.rollback(ID).phase, "rolled-back")
        self.assertEqual(fakes.world(disk, host, case.paths()), before)

    def test_a_tampered_reinstatement_is_stuck(self):
        case = CASES["upgrade_same_pins"]
        disk, host = make(case)
        run = engine_on(disk, host)
        run.begin(ID, case.desired(), fakes.OPERATOR)
        run.guard(ID)
        host.run(("systemctl", "stop", engine.TARGET, engine.SLICE))
        run.collect(ID)
        fakes.switch(disk, host, case.new)
        unit = f"{fakes.UNITS}/{fakes.BOUNDARY}"
        number = next(entry.n for entry in run.read(ID).entries if entry.path == unit)
        disk.nodes[f"{ROOT}/.transaction/{ID}/files/{number}"].data = b"not what was collected"
        journal = run.rollback(ID)
        self.assertEqual(journal.phase, "stuck")
        self.assertIn(f"{unit} is not as it was collected", journal.outcome["error"])
        self.assertFalse(host.enabled())


if __name__ == "__main__":
    unittest.main()
