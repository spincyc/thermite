"""The install engine's manifest, class table and journal, and its verbs over fakes."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import sys
import unittest

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))

from oh_install import engine
from tests import txn_fakes as fakes

ROOT = "/srv/thermite"
ID = "20261105T101500Z-3f9a2c"
EARLIER = "20261020T080000Z-77ab01"
HASH = "a" * 64
#: The pin a shared-pinned item follows (a versions.json service, not a secret).
PROMETHEUS = "prometheus"
TOOLS = {"systemctl": "/usr/bin/systemctl", "cp": "/usr/bin/cp", "sync": "/usr/bin/sync",
         "nft": "/usr/bin/nft", "apparmor_parser": None, "podman": "/usr/bin/podman"}


def recorded(path, cls, **fields):
    meta = {"sha256": HASH, "mode": 0o644, "uid": 0, "gid": 0} if engine.CLASSES[cls].shape == "file" else {}
    return {"path": path, "class": cls, **meta, **fields}


def manifest(**changes):
    data = {
        "format": 2, "installation": ROOT, "transaction": ID, "committed_at": "2026-11-05T10:31:12Z",
        "installed": True, "kept": ID,
        "release": {"application": {"version": "0.1.0", "digest": "sha256:" + "b" * 64}, "site": "c" * 40},
        "pins": {"openhab": "sha256:" + "d" * 64, "prometheus": "sha256:" + "e" * 64},
        "tools": dict(TOOLS),
        "items": [
            recorded("/etc/containers/systemd/thermite-openhab.container", "unit", unit="thermite-openhab.service"),
            recorded("/etc/thermite-native.nft", "nft", apply_unit="thermite-native-boundary.service"),
            recorded("/etc/systemd/system/multi-user.target.wants/thermite-native-boundary.service", "link",
                     target="/etc/systemd/system/thermite-native-boundary.service"),
            recorded("/etc/apparmor.d/thermite-node-exporter", "apparmor"),
            recorded(f"{ROOT}/runtime/userdata", "tree"),
            recorded(f"{ROOT}/runtime/monitoring/prometheus", "shared-pinned", pin=PROMETHEUS),
            recorded(f"{ROOT}/access/releases", "versioned", current=ID, previous=EARLIER),
            recorded(f"{ROOT}/runtime/deploy/deployed.json", "file"),
        ],
        "images": {"current": ["docker.io/openhab/openhab@sha256:" + "d" * 64], "previous": []},
        "application": {"current": "sha256:" + "b" * 64, "previous": None},
    }
    data.update(changes)
    return data


class Manifest(unittest.TestCase):
    def test_parse_render_round_trip(self):
        data = manifest()
        raw = engine.render_manifest(engine.validate_manifest(data, root=ROOT))
        self.assertTrue(raw.endswith(b"}\n"))
        again = engine.parse_manifest(raw, root=ROOT)
        self.assertEqual(engine.render_manifest(again), raw)
        self.assertEqual([item["path"] for item in again["items"]],
                         sorted(item["path"] for item in data["items"]))

    def test_refusals(self):
        cases = {
            "format": manifest(format=1),
            "is for /srv/other": manifest(installation="/srv/other"),
            "unknown extra": {**manifest(), "extra": 1},
            "is recorded as a tree": manifest(items=[recorded(f"{ROOT}/runtime", "tree-move")]),
            "unknown item class": manifest(items=[{"path": f"{ROOT}/runtime/x", "class": "script"}]),
            "outside what Thermite may own": manifest(items=[recorded("/etc/passwd", "file")]),
            "normalised": manifest(items=[recorded(f"{ROOT}/runtime/../access", "tree")]),
            "never nest": manifest(items=[recorded(f"{ROOT}/runtime/deploy", "tree"),
                                          recorded(f"{ROOT}/runtime/deploy/deployed.json", "file")]),
            "declared twice": manifest(items=[recorded(f"{ROOT}/runtime/conf", "tree"),
                                              recorded(f"{ROOT}/runtime/conf", "tree")]),
            "sha256 is not valid": manifest(items=[recorded("/etc/thermite/x.conf", "file", sha256="0")]),
            "unit is not valid": manifest(items=[recorded("/etc/systemd/system/thermite-x.service", "unit",
                                                          unit="sshd.service")]),
            "missing target": manifest(items=[{"path": "/etc/systemd/system/thermite.target.wants/thermite-x.path",
                                                   "class": "link"}]),
            "tool podman": manifest(tools={**TOOLS, "podman": "podman"}),
            "tool cp": manifest(tools={**TOOLS, "cp": None}),
        }
        for expected, data in cases.items():
            with self.subTest(expected):
                with self.assertRaisesRegex(engine.EngineError, expected):
                    engine.validate_manifest(data, root=ROOT)

    def test_not_json(self):
        with self.assertRaisesRegex(engine.EngineError, "not JSON"):
            engine.parse_manifest(b"{", root=ROOT)

    def test_allowlist(self):
        cases = [
            ("/etc/systemd/system/thermite-openhab.service.d/50-container-lifecycle.conf", "unit", True),
            ("/etc/systemd/system/thermite.target", "unit", True),
            ("/etc/systemd/system/sshd.service", "unit", False),
            ("/etc/systemd/system/thermite.target.wants/thermite-atv-restart.path", "link", True),
            ("/etc/systemd/system/multi-user.target.wants/thermite-native-boundary.service", "link", True),
            ("/etc/systemd/system/multi-user.target.wants/sshd.service", "link", False),
            ("/etc/systemd/system/thermite.target.wants/thermite-atv-restart.path", "unit", False),
            ("/etc/containers/systemd/thermite-openhab.container", "unit", True),
            ("/etc/containers/systemd/other.container", "unit", False),
            ("/etc/thermite-native.nft", "nft", True),
            ("/etc/thermite/node-exporter-containers.conf", "file", True),
            ("/etc/thermite/units.json", "file", True),
            ("/etc/thermite/installed.json", "file", False),
            ("/etc/thermite/.installed.json." + ID, "file", False),
            ("/etc/apparmor.d/thermite-node-exporter", "apparmor", True),
            ("/etc/apparmor.d/usr.bin.other", "apparmor", False),
            ("/usr/share/doc/thermite/licenses/node-exporter-apparmor.LICENSE", "file", True),
            (f"{ROOT}/runtime/userdata", "tree", True),
            (f"{ROOT}/runtime", "tree-move", True),
            (f"{ROOT}/access/state", "tree", True),
            (f"{ROOT}/.transaction/x", "tree", False),
            (ROOT, "tree", False),
            (f"{ROOT}/runtime/userdata", "unit", False),
            ("/etc/thermite/state", "tree", False),
        ]
        for path, cls, expected in cases:
            with self.subTest(path=path, cls=cls):
                self.assertIs(engine.allowed(ROOT, path, cls), expected)

    def test_a_system_prefix_moves_every_system_path(self):
        self.assertTrue(engine.allowed(ROOT, "/scratch/etc/thermite-native.nft", "nft", Path("/scratch")))
        self.assertFalse(engine.allowed(ROOT, "/etc/thermite-native.nft", "nft", Path("/scratch")))

    def test_managed_is_outside_the_root(self):
        self.assertTrue(engine.managed(ROOT, "/etc/thermite-native.nft"))
        self.assertFalse(engine.managed(ROOT, f"{ROOT}/runtime/deploy/deployed.json"))


class Desired(unittest.TestCase):
    def test_desired_items_carry_declarations_only(self):
        data = {"kind": "upgrade", "tools": TOOLS, "items": [
            {"path": "/etc/thermite-native.nft", "class": "nft", "apply_unit": "thermite-native-boundary.service"},
            {"path": f"{ROOT}/runtime/monitoring/prometheus", "class": "shared-pinned", "pin": PROMETHEUS,
             "value": "sha256:" + "f" * 64}]}
        checked = engine.validate_desired(data, ROOT)
        self.assertEqual((checked["pulled"], checked["extra"]), ([], 0))
        with self.assertRaisesRegex(engine.EngineError, "unknown sha256"):
            engine.validate_desired({**data, "items": [{**data["items"][0], "sha256": HASH}]}, ROOT)
        with self.assertRaisesRegex(engine.EngineError, "missing value"):
            engine.validate_desired({**data, "items": [{"path": f"{ROOT}/runtime/monitoring/prometheus",
                                                        "class": "shared-pinned", "pin": PROMETHEUS}]}, ROOT)
        with self.assertRaisesRegex(engine.EngineError, "unknown transaction kind"):
            engine.validate_desired({**data, "kind": "recover"}, ROOT)
        with self.assertRaisesRegex(engine.EngineError, "pulled images"):
            engine.validate_desired({**data, "pulled": ["--all"]}, ROOT)
        # Restarting apply_unit reloads the table: never the target or slice.
        for unit in (engine.TARGET, engine.SLICE, "thermite-native-boundary.path"):
            with self.assertRaisesRegex(engine.EngineError, "apply_unit must be a Thermite service"):
                engine.validate_desired({**data, "items": [{**data["items"][0], "apply_unit": unit}]}, ROOT)
        with self.assertRaisesRegex(engine.EngineError, "tool systemctl must be a program named systemctl"):
            engine.validate_desired({**data, "tools": {**TOOLS, "systemctl": "/usr/bin/sh"}}, ROOT)

    def test_release_and_operator(self):
        release = engine.validate_release({"release": {"site": "c" * 40}, "pins": {"openhab": "x"},
                                           "images": ["quay.io/prometheus/prometheus@sha256:" + "1" * 64]})
        self.assertIsNone(release["application"])
        operator = engine.validate_operator({"uid": 1000, "site": "/home/owner/site",
                                             "record": "/home/owner/site/var/transaction/x/record.json"})
        self.assertEqual((operator["head_before"], operator["head_after"]), (None, None))
        with self.assertRaisesRegex(engine.EngineError, "head_before"):
            engine.validate_operator({**operator, "head_before": "main"})


class CollectRule(unittest.TestCase):
    """The collect column of the class table, for each (installed, desired)
    pair and whether the path is on disk."""

    def test_every_class(self):
        cases = [
            # current, desired, exists, pin changed -> action
            ("unit", "unit", True, False, "copy"),
            ("unit", None, True, False, "move"),
            (None, "unit", False, False, "add"),
            ("link", "link", True, False, "copy"),
            ("link", None, True, False, "move"),
            ("nft", "nft", True, False, "copy"),
            ("apparmor", None, True, False, "move"),
            ("file", "file", False, False, "add"),
            ("tree", "tree", True, False, "copy"),
            ("tree", None, True, False, "move"),
            (None, "tree", False, False, "add"),
            ("tree", "tree-move", True, False, "move"),
            (None, "tree-move", False, False, "add"),
            ("versioned", "versioned", True, False, "leave"),
            ("versioned", None, True, False, "move"),
            (None, "versioned", False, False, "add"),
            ("shared", "shared", True, False, "leave"),
            ("shared", None, True, False, "leave"),
            ("tree", "shared", True, False, "leave"),
            ("shared-pinned", "shared-pinned", True, False, "leave"),
            ("shared-pinned", "shared-pinned", True, True, "copy"),
            (None, "shared-pinned", True, True, "leave"),
            ("shared-pinned", None, True, False, "move"),
            ("tree", None, False, False, "leave"),
        ]
        for current, desired, exists, changed, expected in cases:
            with self.subTest(current=current, desired=desired, exists=exists, changed=changed):
                self.assertEqual(engine.collect_action(current, desired, exists, changed), expected)


def journal(**changes):
    entry = engine.Entry(0, "/etc/thermite-native.nft", "nft", "copy", True, aside="files/0",
                         apply_unit="thermite-native-boundary.service")
    fields = dict(id=ID, kind="upgrade", installation=ROOT, engine_sha256=HASH,
                  engine_path=f"{ROOT}/.transaction/{ID}/engine.py", tools=dict(TOOLS),
                  before={"manifest": None, "source": "none", "target": {"enabled": True, "active": True},
                          "apparmor": False},
                  current=None, desired=[], operator={}, space={}, entries=[entry], pulled=[])
    fields.update(changes)
    return engine.Journal(**fields)


class JournalModel(unittest.TestCase):
    def test_round_trip(self):
        value = journal()
        value.advance("guarded", "2026-11-05T10:15:03Z")
        again = engine.Journal.parse(value.render())
        self.assertEqual(again, value)
        self.assertEqual(again.phases, [["guarded", "2026-11-05T10:15:03Z"]])
        self.assertEqual(json.loads(value.render())["entries"][0]["class"], "nft")

    def test_phases_move_only_forward_or_into_a_rollback(self):
        value = journal()
        for phase in ("guarded", "collecting", "switching", "committed", "kept"):
            value.advance(phase, "t")
        self.assertFalse(value.open)
        with self.assertRaisesRegex(engine.EngineError, "is kept; it cannot become rolling-back"):
            value.advance("rolling-back", "t")
        value = journal()
        value.advance("rolling-back", "t")
        value.advance("stuck", "t")
        value.advance("rolling-back", "t")
        value.advance("rolled-back", "t")
        self.assertFalse(value.open)
        committed = journal(phase="committed")
        with self.assertRaisesRegex(engine.EngineError, "cannot become rolling-back"):
            committed.advance("rolling-back", "t")

    def test_every_phase_has_its_successors(self):
        self.assertEqual(set(engine.NEXT), set(engine.PHASES))
        for phase, following in engine.NEXT.items():
            self.assertLessEqual(set(following), set(engine.PHASES), phase)

    def test_a_foreign_format_names_the_engine_that_wrote_it(self):
        data = json.loads(journal().render())
        data["format"] = 2
        with self.assertRaisesRegex(engine.EngineError, f"run the engine that wrote it: {ROOT}/.transaction/{ID}"):
            engine.Journal.parse(json.dumps(data).encode())

    def test_damaged_journals_refuse(self):
        data = json.loads(journal().render())
        for change, expected in (
                (lambda d: d.update(phase="verifying"), "phase is not valid"),
                (lambda d: d["entries"][0].update(aside="../../etc"), "aside is not valid"),
                (lambda d: d["entries"][0].update(n=3), "numbered in order"),
                (lambda d: d.pop("live"), "missing live")):
            damaged = copy.deepcopy(data)
            change(damaged)
            with self.subTest(expected):
                with self.assertRaisesRegex(engine.EngineError, expected):
                    engine.Journal.parse(json.dumps(damaged).encode())


class Verbs(unittest.TestCase):
    """begin, guard, collect, commit, rollback, status and prune on fakes."""

    def setUp(self):
        self.disk = fakes.MemoryDisk()
        self.host = fakes.FakeHost(self.disk)
        self.old = fakes.version("old")
        self.new = fakes.version("new")
        self.installed = fakes.install(self.disk, self.host, self.old)
        self.messages = []
        self.engine = self.make()

    def make(self, source=fakes.SOURCE):
        return engine.Engine(self.disk, self.host, ROOT, source=source, clock=lambda: fakes.CLOCK,
                             say=self.messages.append)

    def desired(self, new=None, kind="upgrade"):
        return {"kind": kind, "tools": fakes.TOOLS, "items": fakes.desired_items(new or self.new),
                "pulled": (new or self.new).images}

    def begin(self, new=None, kind="upgrade", id=ID):
        return self.engine.begin(id, self.desired(new, kind), fakes.OPERATOR)

    def through_switch(self, new=None, kind="upgrade", id=ID):
        self.begin(new, kind, id)
        self.engine.guard(id)
        self.host.run(("systemctl", "stop", engine.TARGET, engine.SLICE))
        self.engine.collect(id)
        fakes.switch(self.disk, self.host, new or self.new, id)

    def test_begin_records_the_collect_rule_and_pins_the_engine(self):
        journal = self.begin()
        directory = f"{ROOT}/.transaction/{ID}"
        actions = {entry.path: entry.action for entry in journal.entries}
        self.assertEqual(actions[f"{fakes.UNITS}/thermite.target"], "copy")
        self.assertEqual(actions[f"{ROOT}/runtime/userdata"], "copy")
        self.assertEqual(actions[f"{ROOT}/runtime/logs"], "leave")
        self.assertEqual(actions[f"{ROOT}/runtime/monitoring/prometheus"], "leave")
        self.assertEqual(actions[f"{ROOT}/access/releases/{ID}"], "add")
        self.assertEqual(self.disk.nodes[f"{directory}/engine.py"].data, fakes.SOURCE)
        self.assertEqual(self.disk.nodes[f"{directory}/engine.py"].mode, 0o600)
        self.assertEqual(self.disk.nodes[f"{directory}/manifest"].data,
                         self.disk.nodes["/etc/thermite/installed.json"].data)
        self.assertEqual(journal.engine_path, f"{directory}/engine.py")
        self.assertEqual(journal.before["target"], {"enabled": True, "active": True})
        self.assertEqual((journal.phase, journal.phases), ("planned", [["planned", fakes.CLOCK]]))
        self.assertEqual(self.engine.read(ID), journal)
        self.assertEqual(self.host.log, [], "begin changes nothing live")

    def test_a_pin_change_copies_shared_samples(self):
        journal = self.begin(fakes.version("new", prometheus="two"))
        entry = next(entry for entry in journal.entries if entry.path.endswith("prometheus"))
        self.assertEqual((entry.action, entry.aside), ("copy", f"trees/{entry.n}"))

    def test_begin_refuses_drifted_missing_and_foreign_root_files(self):
        unit = f"{fakes.UNITS}/{fakes.BOUNDARY}"
        cases = {
            "changed since it was installed \\(sha256\\)": lambda: setattr(self.disk.nodes[unit], "data", b"edited"),
            "changed since it was installed \\(mode\\)": lambda: setattr(self.disk.nodes[unit], "mode", 0o664),
            "is installed but missing": lambda: self.disk.drop(unit),
            "the installed manifest does not list it": lambda: self.disk.put(
                f"{fakes.UNITS}/thermite-stray.service", data=b"stray"),
        }
        for expected, damage in cases.items():
            with self.subTest(expected):
                self.setUp()
                damage()
                new = fakes.version("new")
                if "list it" in expected:
                    new.files[f"{fakes.UNITS}/thermite-stray.service"] = ("unit", b"x", {"unit": "thermite-stray.service"})
                with self.assertRaisesRegex(engine.EngineError, expected):
                    self.begin(new)
                self.assertNotIn(f"{ROOT}/.transaction", self.disk.nodes)

    def test_state_files_are_set_aside_as_found(self):
        self.disk.nodes[f"{ROOT}/runtime/deploy/deployed.json"].data = b'{"redeployed": true}'
        journal = self.begin()
        self.assertEqual(next(entry.action for entry in journal.entries if entry.path.endswith("deployed.json")),
                         "copy")

    def test_a_symlinked_ancestor_refuses(self):
        self.disk.drop(f"{ROOT}/access/state")
        self.disk.put(f"{ROOT}/elsewhere", "dir")
        self.disk.put(f"{ROOT}/access/state", "link", target=f"{ROOT}/elsewhere")
        with self.assertRaisesRegex(engine.EngineError, "is not a dir as its tree class needs"):
            self.begin()
        self.disk.drop(f"{ROOT}/access")
        self.disk.put(f"{ROOT}/access", "link", target=f"{ROOT}/elsewhere")
        with self.assertRaisesRegex(engine.EngineError, "is not a directory \\(a symlink is never followed\\)"):
            self.begin()

    def test_one_transaction_at_a_time(self):
        self.begin()
        with self.assertRaisesRegex(engine.EngineError, f"transaction {ID} is planned; finish or roll it back"):
            self.begin(id="20261105T111500Z-000001")

    def test_plan_reports_room_for_a_full_copy(self):
        self.disk.put(f"{ROOT}/runtime/userdata/cache.bin", data=b"u" * (40 << 20))
        self.disk.budget["root"] = self.disk.used("root") + (300 << 20)
        space = self.engine.plan(self.desired())
        trees = dict(space["items"])
        self.assertEqual(trees["runtime/userdata"],
                         (40 << 20) + sum(map(len, self.old.trees[f"{ROOT}/runtime/userdata"][1].values())))
        self.assertEqual(space["required"], sum(trees.values()) + engine.MARGIN)
        self.assertEqual(engine.describe_space(space, ROOT).splitlines(), [
            "Room to set aside the current version, on /srv/thermite (free 300.0 MiB):",
            "  access/state                            <1 MiB",
            "  runtime/deploy/deployed.json            <1 MiB",
            "  runtime/userdata                      40.0 MiB",
            "  8 root files and links                  <1 MiB",
            "  margin                               256.0 MiB",
            "  needed                               296.0 MiB  (a full copy; cloning may use less)",
        ])
        self.disk.budget["root"] -= 10 << 20
        self.assertEqual(engine.describe_space(self.engine.plan(self.desired()), ROOT).splitlines()[-1],
                         "Not enough space: nothing was stopped or changed. Free 6.0 MiB, then rerun.")

    def test_every_later_verb_refuses_another_engine(self):
        self.begin()
        other = self.make(source=b"# a different release's engine\n")
        for verb in ("guard", "collect", "commit", "rollback"):
            with self.subTest(verb):
                with self.assertRaisesRegex(engine.EngineError,
                                            f"begun by another engine; run {ROOT}/.transaction/{ID}/engine.py"):
                    getattr(other, verb)(ID)

    def test_guard_on_a_host_without_the_boot_target(self):
        """systemctl refuses to disable a unit whose file does not exist: a
        fresh install, and a reinstall after an uninstall, have none."""
        gone = fakes.version("gone", shared=True)
        gone.files, gone.links = {}, {}
        uninstall = "20261101T000000Z-000001"
        self.begin(gone, "uninstall", id=uninstall)
        self.engine.guard(uninstall)
        self.host.run(("systemctl", "stop", engine.TARGET, engine.SLICE))
        self.engine.collect(uninstall)
        self.engine.commit(uninstall)
        self.assertNotIn(f"{fakes.UNITS}/{engine.TARGET}", self.disk.nodes)
        self.assertEqual(self.host.run(("systemctl", "disable", engine.TARGET)), 1)
        self.begin(kind="install")
        start = len(self.host.log)
        self.assertEqual(self.engine.guard(ID).phase, "guarded")
        self.assertEqual(self.host.log[start:], [], "nothing to disable")
        self.host.run(("systemctl", "stop", engine.TARGET, engine.SLICE))
        self.engine.collect(ID)
        fakes.switch(self.disk, self.host, self.new)
        self.assertEqual(self.engine.commit(ID, fakes.release(self.new)).phase, "kept")
        self.assertTrue(self.host.enabled())

    def test_begin_refuses_a_null_tool_an_item_needs(self):
        """A rollback that needs a tool the journal left null could never
        finish; begin refuses instead."""
        for tool, expected in (("apparmor_parser", fakes.POLICY), ("nft", fakes.RULES)):
            with self.subTest(tool):
                desired = {**self.desired(), "tools": {**fakes.TOOLS, tool: None}}
                with self.assertRaisesRegex(engine.EngineError,
                                            f"{expected} needs the {tool} tool, which the desired set leaves null"):
                    self.engine.begin(ID, desired, fakes.OPERATOR)
                self.assertNotIn(f"{ROOT}/.transaction", self.disk.nodes)

    def test_without_apparmor_a_null_apparmor_parser_is_never_run(self):
        self.disk.drop(fakes.APPARMOR)
        self.host.loaded = {}
        before = fakes.world(self.disk, self.host, fakes.managed_paths(self.old, self.new))
        tools = {**fakes.TOOLS, "apparmor_parser": None}
        self.host.tools = tools
        self.engine.begin(ID, {**self.desired(), "tools": tools}, fakes.OPERATOR)
        self.engine.guard(ID)
        self.host.run(("systemctl", "stop", engine.TARGET, engine.SLICE))
        self.engine.collect(ID)
        fakes.switch(self.disk, self.host, self.new)
        self.assertEqual(self.engine.rollback(ID).phase, "rolled-back")
        self.assertEqual(fakes.world(self.disk, self.host, fakes.managed_paths(self.old, self.new)), before)

    def test_collect_needs_thermite_stopped_and_unguarded_boot(self):
        self.begin()
        self.engine.guard(ID)
        with self.assertRaisesRegex(engine.EngineError, "must be disabled and thermite.slice stopped"):
            self.engine.collect(ID)
        self.assertEqual(self.engine.read(ID).phase, "guarded")

    def test_commit_records_what_is_on_disk_and_lifts_the_guard(self):
        self.through_switch()
        journal = self.engine.commit(ID, fakes.release(self.new))
        self.assertEqual(journal.phase, "kept")
        data = engine.parse_manifest(self.disk.nodes["/etc/thermite/installed.json"].data, root=ROOT)
        items = {item["path"]: item for item in data["items"]}
        rules = self.new.files[fakes.RULES][1]
        self.assertEqual(items[fakes.RULES], {"path": fakes.RULES, "class": "nft", "sha256": fakes.digest(rules),
                                              "mode": 0o644, "uid": 0, "gid": 0, "apply_unit": fakes.BOUNDARY})
        self.assertEqual(items[f"{ROOT}/access/releases"]["current"], ID)
        self.assertEqual(items[f"{ROOT}/access/releases"]["previous"], fakes.OLD)
        self.assertEqual(items[f"{ROOT}/runtime/monitoring/prometheus"],
                         {"path": f"{ROOT}/runtime/monitoring/prometheus", "class": "shared-pinned",
                          "pin": PROMETHEUS})
        self.assertEqual(data["application"], {"current": "sha256:new", "previous": "sha256:old"})
        self.assertTrue(data["installed"])
        self.assertTrue(self.host.enabled())
        self.assertEqual(self.engine.status(), [{"id": ID, "kind": "upgrade", "phase": "kept", "open": False,
                                                 "engine": f"{ROOT}/.transaction/{ID}/engine.py", "outcome": None,
                                                 "bytes": self.disk.allocated(f"{ROOT}/.transaction/{ID}")}])

    def test_commit_prunes_beyond_the_previous_version(self):
        two = fakes.version("new", prometheus="two")
        self.through_switch(two)
        self.engine.commit(ID, fakes.release(two))
        failed_id, third = "20261105T120000Z-0000aa", "20261105T130000Z-0000bb"
        newer = fakes.version("newer", prometheus="three")
        # A rolled-back attempt that pulled an image, then a committed one.
        self.through_switch(newer, id=failed_id)
        self.engine.rollback(failed_id)
        self.assertTrue(self.disk.lstat(f"{ROOT}/.transaction/{failed_id}/failed"))
        self.host.images |= {*newer.images, "docker.io/fictional/kept@sha256:1"}
        stuck = "20261101T000000Z-0000cc"
        self.disk.put(f"{ROOT}/.transaction/{stuck}/journal.json", data=b"{}", mode=0o600)
        self.disk.nodes[f"{ROOT}/.transaction/{stuck}"].mode = 0o700
        newest = fakes.version("newest", prometheus="four")
        try:
            self.through_switch(newest, id=third)
        except engine.EngineError as error:
            self.assertIn(f"transaction {stuck} cannot be read", str(error))
        else:
            self.fail("an unreadable transaction must refuse begin")
        self.disk.drop(f"{ROOT}/.transaction/{stuck}")
        self.through_switch(newest, id=third)
        self.engine.commit(third, fakes.release(newest))
        self.assertEqual(self.engine.ids(), [third])
        self.assertEqual(sorted(self.disk.listdir(f"{ROOT}/access/releases")), [ID, third])
        # The first version's image is now beyond the previous one; the
        # rolled-back attempt's pull was kept for a retry until now.
        removed = [argv[-1] for argv in self.host.log if argv[:3] == ("podman", "image", "rm")]
        self.assertEqual(removed, sorted({newer.images[0], self.old.images[0]}))
        self.assertEqual(self.messages, [])

    def test_pruning_keeps_every_current_and_previous_image(self):
        """An image the new manifest still lists as current or previous stays,
        whether it was the old previous one or a rolled-back attempt's pull."""
        first, second = "quay.io/fictional/app@sha256:aaaa", "quay.io/fictional/app@sha256:bbbb"
        for label, images in (("a configuration-only upgrade", [second]),
                              ("installing the previous site commit again", [first])):
            with self.subTest(label):
                self.setUp()
                data = json.loads(self.disk.nodes["/etc/thermite/installed.json"].data)
                data["images"] = {"current": [second], "previous": [first]}
                self.disk.nodes["/etc/thermite/installed.json"].data = engine.render_manifest(
                    engine.validate_manifest(data, root=ROOT))
                self.host.images = {first, second}
                new = fakes.version("new")
                new.images = images
                self.through_switch(new)
                journal = self.engine.commit(ID, fakes.release(new))
                self.assertEqual(set(journal.manifest["images"]["current"] + journal.manifest["images"]["previous"]),
                                 {first, second})
                self.assertEqual([argv for argv in self.host.log if argv[:2] == ("podman", "image")], [])
                self.assertEqual(self.host.images, {first, second})

    def test_rollback_puts_the_old_version_back(self):
        before = fakes.world(self.disk, self.host, fakes.managed_paths(self.old, self.new))
        self.through_switch()
        journal = self.engine.rollback(ID)
        self.assertEqual((journal.phase, journal.rollback_from), ("rolled-back", "switching"))
        self.assertEqual(journal.outcome, {"at": fakes.CLOCK, "result": "rolled-back", "started": True, "error": None})
        self.assertEqual(fakes.world(self.disk, self.host, fakes.managed_paths(self.old, self.new)), before)
        self.assertEqual(self.disk.listdir(f"{ROOT}/.transaction/{ID}/failed"),
                         sorted(str(entry.n) for entry in journal.entries if entry.state == "restored"))
        calls = len(self.host.log)
        self.assertEqual(self.engine.rollback(ID).phase, "rolled-back")
        self.assertEqual(len(self.host.log), calls, "a finished rollback runs nothing")

    def test_rollback_reloads_the_table_with_the_old_versions_unit(self):
        """A release that renames the boundary unit: the old rules go back
        with the old unit, which is what loads them."""
        before = fakes.world(self.disk, self.host, fakes.managed_paths(self.old, self.new))
        renamed = "thermite-boundary.service"
        new = fakes.version("new")
        del new.files[f"{fakes.UNITS}/{fakes.BOUNDARY}"]
        new.files[f"{fakes.UNITS}/{renamed}"] = ("unit", fakes.unit_text(renamed, "new"), {"unit": renamed})
        new.files[fakes.RULES] = ("nft", new.files[fakes.RULES][1], {"apply_unit": renamed})
        new.links = {f"{fakes.WANTS}/{renamed}": f"{fakes.UNITS}/{renamed}"}
        self.through_switch(new)
        self.host.run(("systemctl", "restart", renamed))
        self.assertEqual(self.host.table, new.files[fakes.RULES][1].decode())
        journal = self.engine.rollback(ID)
        self.assertEqual((journal.phase, journal.outcome["error"]), ("rolled-back", None))
        self.assertIn(f"systemctl restart {fakes.BOUNDARY}", self.engine.manual(journal))
        self.assertEqual(fakes.world(self.disk, self.host, fakes.managed_paths(self.old, new)) | {"host": None},
                         before | {"host": None})
        self.assertEqual(self.host.table, self.old.files[fakes.RULES][1].decode())

    def test_a_table_no_rule_file_loaded_is_put_back_from_its_text(self):
        disk = fakes.MemoryDisk()
        host = fakes.FakeHost(disk)
        fakes.empty(disk)
        host.table = "table inet thermite_native { # loaded by hand }\n"
        run = engine.Engine(disk, host, ROOT, source=fakes.SOURCE, clock=lambda: fakes.CLOCK)
        run.begin(ID, self.desired(kind="install"), fakes.OPERATOR)
        run.guard(ID)
        run.collect(ID)
        fakes.switch(disk, host, self.new)
        self.assertNotEqual(host.table, "table inet thermite_native { # loaded by hand }\n")
        journal = run.rollback(ID)
        self.assertEqual((journal.phase, journal.outcome["error"]), ("rolled-back", None))
        self.assertEqual(host.table, "table inet thermite_native { # loaded by hand }\n")
        self.assertIn(f"nft -f {ROOT}/.transaction/{ID}/live/thermite_native.nft", run.manual(journal))

    def test_a_committed_transaction_is_finished_not_undone(self):
        self.through_switch()
        commit = self.engine.commit
        journal = self.engine.read(ID)
        # Stop right after the commit point.
        self.engine._finish = lambda journal: journal
        commit(ID, fakes.release(self.new))
        self.assertEqual(self.engine.read(ID).phase, "committed")
        self.assertFalse(self.host.enabled())
        del self.engine._finish
        self.assertEqual(self.engine.rollback(ID).phase, "kept")
        self.assertTrue(self.host.enabled())
        self.assertEqual(engine.parse_manifest(self.disk.nodes["/etc/thermite/installed.json"].data,
                                               root=ROOT)["transaction"], ID)
        self.assertNotEqual(journal.phase, "kept")

    def test_uninstall_records_kept_state_and_leaves_the_boot_target_off(self):
        gone = fakes.version("gone", shared=True)
        gone.files, gone.links = {}, {}
        self.begin(gone, "uninstall")
        self.engine.guard(ID)
        self.host.run(("systemctl", "stop", engine.TARGET, engine.SLICE))
        self.engine.collect(ID)
        self.engine.commit(ID)
        data = engine.parse_manifest(self.disk.nodes["/etc/thermite/installed.json"].data, root=ROOT)
        self.assertFalse(data["installed"])
        self.assertEqual({item["class"] for item in data["items"]}, {"shared"})
        self.assertFalse(self.host.enabled())
        self.assertNotIn(fakes.RULES, self.disk.nodes)
        self.assertIsNone(self.host.table)
        self.assertEqual(self.host.loaded, {})

    def test_a_clean_install_records_its_new_trees(self):
        clean = fakes.version("new", tree_class="tree-move")
        self.through_switch(clean, "clean")
        journal = self.engine.commit(ID, fakes.release(clean))
        moved = [entry for entry in journal.entries if entry.cls == "tree-move"]
        self.assertEqual([entry.action for entry in moved], ["move", "move"])
        for entry in moved:
            self.assertIsNone(self.disk.lstat(f"{ROOT}/.transaction/{ID}/{entry.aside}"))
        data = engine.parse_manifest(self.disk.nodes["/etc/thermite/installed.json"].data, root=ROOT)
        self.assertEqual({item["class"] for item in data["items"] if item["path"].endswith(("userdata", "state"))},
                         {"tree"})

    def test_rollback_print_lists_the_manual_commands(self):
        self.through_switch()
        journal = self.engine.read(ID)
        lines = self.engine.manual(journal)
        unit = next(entry for entry in journal.entries if entry.path.endswith("thermite.target"))
        directory = f"{ROOT}/.transaction/{ID}"
        self.assertEqual(lines[:2], [f"# Roll back transaction {ID} (upgrade, switching) by hand, as root.",
                                     "systemctl stop thermite.target thermite.slice"])
        self.assertIn(f"test ! -e {unit.path} || mv -T {unit.path} {directory}/failed/{unit.n}", lines)
        self.assertIn(f"mv -T {directory}/{unit.aside} {unit.path}", lines)
        self.assertIn(f"test ! -e {ROOT}/access/releases/{ID} || mv -T {ROOT}/access/releases/{ID} "
                      f"{directory}/failed/{next(e.n for e in journal.entries if e.path.endswith(ID))}", lines)
        self.assertEqual(lines[-6:], ["systemctl daemon-reload", f"systemctl restart {fakes.BOUNDARY}",
                                      f"apparmor_parser -r {fakes.POLICY}", "systemctl reset-failed 'thermite*'",
                                      "systemctl enable thermite.target", "systemctl start thermite.target"])


if __name__ == "__main__":
    unittest.main()
