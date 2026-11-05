"""The one-time bridge describes an installation without an installed
manifest as the engine's current version, or refuses before anything changes.

DELETE with lib/oh_install/bridge.py. Fictional site, host and fixture
paths; nothing on this host is read or changed but temporary Git repos.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))

import oh_apparmor  # noqa: E402
import oh_deploy as deploy  # noqa: E402
from oh_install import bridge, engine, ingress, manifest, ownership, steps  # noqa: E402
from oh_install.system import System, UnitFile  # noqa: E402
from tests import txn_fakes  # noqa: E402
from tests.test_install_manifest import ROOT, context  # noqa: E402

RELEASE = "d" * 64
TOOLS = txn_fakes.TOOLS


def installed_world(ctx, *, skip=()):
    """Today's installation: path -> (kind, bytes or link target, mode, uid).
    The units are the ones this context renders."""
    world = {}
    for source, target in deploy.INSTALLED.items():
        world[target] = ("file", ctx.unit(source), 0o644, 0)
    for path in (ingress.UNIT_FILE, ingress.RULE_FILE, oh_apparmor.CONFIG, oh_apparmor.POLICY, oh_apparmor.LICENSE,
                 *(manifest.UNITS / name for name in ("thermite-access.service", "thermite-access-worker.service"))):
        world[path] = ("file", f"fixture {path.name}\n".encode(), 0o644, 0)
    world[ownership.MANIFEST] = ("file", json.dumps({"version": 1, "release": RELEASE, "units": {}}).encode(), 0o644, 0)
    for path in (*(manifest.WANTS / name for name in ("thermite-atv-restart.path", "thermite-metrics-restart.path",
                                                      "thermite-access.service", "thermite-access-worker.service")),
                 manifest.UNITS / "multi-user.target.wants" / ingress.UNIT):
        world[path] = ("link", str(manifest.UNITS / path.name).encode(), 0o777, 0)
    for name in ("runtime/conf", "runtime/userdata", "runtime/logs", "runtime/monitoring/prometheus",
                 "access/state", "access/releases"):
        world[ROOT / name] = ("dir", b"", 0o755, 0)
    return {path: value for path, value in world.items() if path not in skip}


def host_system(world, *, record=b'{"commit": "' + b"a" * 40 + b'"}'):
    found = Mock(spec=System)
    found.lexists.side_effect = lambda path: Path(path) in world
    found.readlink.side_effect = lambda path: (world[Path(path)][1].decode() if world.get(Path(path), ("",))[0] == "link"
                                               else None)

    def unit_file(path):
        kind, data, mode, uid = world.get(Path(path), (None, b"", 0, 0))
        return UnitFile(data, uid, mode, True, 0) if kind == "file" else None

    def read_bytes(path):
        if path == oh_apparmor.MODULE_ENABLED:
            return b"Y\n"
        if str(path).endswith("deployed.json"):
            return record
        return None

    found.unit_file.side_effect = unit_file
    found.read_bytes.side_effect = read_bytes
    return found


def describe(ctx, world, **kwargs):
    system = host_system(world, **kwargs)
    items = manifest.desired(steps.install(ctx, preflight=False), system, ROOT)
    with patch.object(bridge, "_prove"):
        return bridge.describe(ctx, system, items, TOOLS, txn_fakes.CLOCK), items


class Described(unittest.TestCase):
    def test_needed_only_with_the_version_1_record_and_no_manifest(self):
        for present, expected in (({ownership.MANIFEST}, True), ({ownership.MANIFEST, manifest.INSTALLED}, False),
                                  (set(), False)):
            with self.subTest(present=present):
                system = Mock(spec=System)
                system.lexists.side_effect = lambda path: Path(path) in present
                self.assertEqual(bridge.needed(system), expected)

    def test_the_installation_becomes_a_manifest_the_engine_accepts(self):
        ctx = context()
        world = installed_world(ctx)
        current, _ = describe(ctx, world)
        items = {item["path"]: item for item in current["items"]}
        unit = deploy.INSTALLED["thermite-openhab.container"] if "thermite-openhab.container" in deploy.INSTALLED else None
        for path, (kind, data, mode, uid) in world.items():
            if not engine.managed(ROOT, path):
                continue
            with self.subTest(path=path):
                if kind == "link":
                    self.assertEqual(items[str(path)], {"path": str(path), "class": "link", "target": data.decode()})
                else:
                    self.assertEqual((items[str(path)]["sha256"], items[str(path)]["mode"], items[str(path)]["uid"]),
                                     (hashlib.sha256(data).hexdigest(), mode, uid))
        if unit is not None:
            self.assertEqual(items[str(unit)]["unit"], "thermite-openhab.service")
        self.assertEqual(items[str(ingress.RULE_FILE)]["apply_unit"], ingress.UNIT)
        self.assertEqual(items[str(ownership.MANIFEST)]["class"], "file")
        samples = items[str(ROOT / "runtime/monitoring/prometheus")]
        self.assertEqual((samples["class"], samples["pin"]), ("shared-pinned", "prometheus"))
        self.assertNotIn(str(ROOT / "access/releases"), items, "older releases stay where they are")
        self.assertNotIn(str(ROOT / "runtime/metrics"), items, "only what exists is described")
        self.assertEqual((current["transaction"], current["kept"]), (bridge.DESCRIBED, bridge.DESCRIBED))
        self.assertEqual(current["application"], {"current": RELEASE, "previous": None})
        self.assertEqual(current["release"]["site"], "a" * 40)
        self.assertEqual(current["pins"], {name: digest for name, digest in manifest.pins(ctx).items()
                                           if name in current["pins"]})
        self.assertIn("openhab", current["pins"])
        self.assertTrue(set(current["images"]["current"]) <= set(manifest.images(ctx)))

    def test_the_engine_plans_over_the_description_without_drift(self):
        ctx = context()
        world = installed_world(ctx)
        current, items = describe(ctx, world)
        disk = txn_fakes.MemoryDisk()
        disk.put(ROOT, "dir")
        for path, (kind, data, mode, uid) in world.items():
            if kind == "link":
                disk.put(path, "link", target=data.decode(), mode=mode, uid=uid)
            else:
                disk.put(path, kind, data=data, mode=mode, uid=uid)
        runner = engine.Engine(disk, txn_fakes.FakeHost(disk), ROOT, source=txn_fakes.SOURCE)
        desired = {"kind": "upgrade", "tools": TOOLS, "items": [item.to_json() for item in items]}
        entries = {entry.path: entry.action for entry in runner.entries(current, desired["items"], txn_fakes.ID)}
        self.assertEqual(entries[str(ingress.RULE_FILE)], "copy")
        self.assertEqual(entries[str(ROOT / "runtime/monitoring/prometheus")], "leave", "the same Prometheus image")
        self.assertEqual(entries[str(ROOT / "access/releases") + "/" + txn_fakes.ID], "add")
        self.assertEqual(entries[str(ROOT / "runtime/metrics")], "add")
        runner.plan(desired, current)
        # A file changed after it was described is drift the engine refuses.
        disk.put(ingress.RULE_FILE, data=b"changed\n")
        with self.assertRaisesRegex(engine.EngineError, "changed since it was installed"):
            runner.plan(desired, current)

    def test_a_new_prometheus_image_copies_its_samples_aside(self):
        ctx = context()
        world = installed_world(ctx)
        quadlet = next(path for path, (kind, data, *_) in world.items()
                       if path.suffix == ".container" and b"prometheus" in data and b"Image=" in data)
        kind, data, mode, uid = world[quadlet]
        digest = manifest.pins(ctx)["prometheus"]
        world[quadlet] = (kind, data.replace(digest.encode(), b"sha256:" + b"0" * 64), mode, uid)
        current, items = describe(ctx, world)
        self.assertEqual(current["pins"]["prometheus"], "sha256:" + "0" * 64)
        changed = [item for item in items if item.cls == "shared-pinned"][0]
        self.assertEqual(engine.collect_action("shared-pinned", "shared-pinned", True,
                                               current["pins"][changed.image] != changed.value), "copy")

    def test_what_cannot_be_proved_refuses_before_anything_changes(self):
        ctx = context()
        link = manifest.WANTS / "thermite-atv-restart.path"
        cases = ((ingress.RULE_FILE, ("file", b"x", 0o664, 0), "not a root-owned file only root can change"),
                 (oh_apparmor.POLICY, ("file", b"x", 0o644, 1000), "not a root-owned file only root can change"),
                 (link, ("link", b"/etc/elsewhere", 0o777, 0), "does not link to"))
        for path, value, words in cases:
            with self.subTest(path=path), self.assertRaisesRegex(bridge.BridgeError, words + ".*Nothing was changed"):
                describe(ctx, {**installed_world(ctx), path: value})


class Proof(unittest.TestCase):
    def prove(self, **changes):
        owned = ownership.Owned(recorded=True)
        values = {"inspect": owned, "boundary": True, "rules": b"", "release": None, "installation": True, "state": True,
                  **changes}
        system = Mock(spec=System)
        system.lexists.return_value = True
        system.unit_file.return_value = UnitFile(values["rules"], 0, 0o644)
        with patch.object(ownership, "inspect", return_value=values["inspect"]), \
                patch.object(steps, "boundary_owned", return_value=values["boundary"]), \
                patch.object(ingress, "rule_ports", side_effect=None if values["rules"] == b""
                             else ValueError("native boundary rules have been customized; preserve them for review")), \
                patch("oh_install.access.installed_release", return_value=values["release"]), \
                patch.object(steps, "installation_owned", return_value=values["installation"]), \
                patch.object(steps, "state_owned", return_value=values["state"]):
            bridge._prove(context(), system)

    def test_every_fixed_path_must_be_the_installers(self):
        self.prove()
        for changes, words in (({"inspect": ownership.Owned(problems=("cannot prove Thermite ownership of x",))},
                                "cannot prove Thermite ownership of x"),
                               ({"inspect": ownership.Owned()}, "does not record exactly"),
                               ({"boundary": False}, "is not the installer's"),
                               ({"rules": b"custom"}, "customized"),
                               ({"state": False}, "holds more than this installer created")):
            with self.subTest(changes=changes), self.assertRaisesRegex(bridge.BridgeError, words):
                self.prove(**changes)


class SiteBefore(unittest.TestCase):
    """The version 1 wrapper commits the site pin before the host phase runs."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.site = self.root / "house"
        self.site.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Fixture Owner")
        self.git("config", "user.email", "owner@example.test")
        self.pin("c" * 64, "Pin the installed application")
        self.before = self.git("rev-parse", "HEAD")

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.root), *args], check=True, capture_output=True, text=True).stdout.strip()

    def pin(self, digest, subject, *extra):
        (self.site / "thermite-site.json").write_text(json.dumps({"version": 2, "application": "x", "release": digest}))
        for name in extra:
            (self.root / name).write_text("changed\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", subject)

    def test_the_wrappers_pin_commit_is_recognized(self):
        self.pin(RELEASE, bridge.PIN_SUBJECT)
        self.assertEqual(bridge.site_before(self.root, self.site, RELEASE), (self.before, self.git("rev-parse", "HEAD")))

    def test_anything_else_is_not_guessed(self):
        cases = (("another subject", (), RELEASE), (bridge.PIN_SUBJECT, ("notes.txt",), RELEASE),
                 (bridge.PIN_SUBJECT, (), "e" * 64))
        for subject, extra, digest in cases:
            with self.subTest(subject=subject, extra=extra):
                self.git("reset", "-q", "--hard", self.before)
                self.pin(RELEASE, subject, *extra)
                self.assertEqual(bridge.site_before(self.root, self.site, digest), (None, None))
        self.git("reset", "-q", "--hard", self.before)
        self.assertEqual(bridge.site_before(self.root, self.site, "c" * 64), (None, None),
                         "HEAD is not a pin commit")
        self.assertEqual(bridge.site_before(self.root, self.site, None), (None, None))


if __name__ == "__main__":
    unittest.main()
