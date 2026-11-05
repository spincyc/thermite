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
            "never recorded": manifest(items=[recorded(f"{ROOT}/runtime", "tree-move")]),
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


if __name__ == "__main__":
    unittest.main()
