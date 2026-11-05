"""Each install step declares what it installs; together they are the set
an install transaction collects and the engine records.

Fictional site and host; nothing on this host is read or changed.
"""
from __future__ import annotations

from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))

import oh_apparmor  # noqa: E402
import oh_deploy as deploy  # noqa: E402
import oh_units  # noqa: E402
from oh_install import changeover, engine, ingress, manifest, ownership, steps  # noqa: E402
from oh_install.model import Step  # noqa: E402
from oh_install.system import Stat, System  # noqa: E402
from tests.test_deploy import HOST, VERSIONS  # noqa: E402

ROOT = Path("/srv/thermite-fixture")
UNITS = Path("/etc/systemd/system")


def context(root=ROOT):
    host = {**HOST, "root": str(root)}
    prepared = {name: deploy.Blob(text.encode()) for name, text in deploy.render_units(VERSIONS, host).items()}
    prepared["conf/services/addons.cfg"] = deploy.Blob(b"binding = http\n")
    return steps.Context(Path("/home/owner/fixture-site"), VERSIONS, host, "b" * 40, "c" * 64, "house",
                         "192.0.2.10", prepared=prepared, application={"version": "1.0", "digest": "d" * 64})


def system(*, apparmor=True, present=()):
    found = Mock(spec=System)
    found.read_bytes.side_effect = lambda path: (b"Y\n" if apparmor else b"N\n") if path == oh_apparmor.MODULE_ENABLED else None
    found.lexists.side_effect = lambda path: Path(path) in present
    return found


def declared(apparmor=True, present=()):
    ctx = context()
    return {item.path: item for item in manifest.desired(changeover.switching(ctx, None),
                                                         system(apparmor=apparmor, present=present), ROOT)}


class Inventory(unittest.TestCase):
    def test_every_installed_unit_drop_in_and_wants_link_is_declared(self):
        items = declared()
        for target in deploy.INSTALLED.values():
            with self.subTest(target=target):
                self.assertEqual(items[target].cls, "unit")
        self.assertEqual(items[Path("/etc/systemd/system/thermite-openhab.service.d/50-container-lifecycle.conf")].unit,
                         oh_units.RUNTIME, "a drop-in names the unit it extends")
        self.assertEqual(items[Path("/etc/containers/systemd/thermite-openhab.container")].unit, oh_units.RUNTIME)
        links = {path: item.target for path, item in items.items() if item.cls == "link"}
        self.assertEqual(links, {
            **{UNITS / "thermite.target.wants" / name: UNITS / name
               for name in (oh_units.ATV_RESTART_PATH, oh_units.METRICS_RESTART_PATH, *oh_units.ACCESS_UNITS[:2])},
            UNITS / "multi-user.target.wants" / ingress.UNIT: ingress.UNIT_FILE,
        }, "the boot target's own link is the guard's, never an item")

    def test_the_version_1_unit_record_stays_an_installed_file(self):
        self.assertEqual(declared()[ownership.MANIFEST].cls, "file")

    def test_boundary_firewall_and_confinement_carry_their_live_classes(self):
        items = declared()
        self.assertEqual((items[ingress.RULE_FILE].cls, items[ingress.RULE_FILE].apply_unit), ("nft", ingress.UNIT))
        self.assertEqual(items[ingress.UNIT_FILE].cls, "unit")
        self.assertEqual({path: items[path].cls for path in (oh_apparmor.POLICY, oh_apparmor.CONFIG, oh_apparmor.LICENSE)},
                         {oh_apparmor.POLICY: "apparmor", oh_apparmor.CONFIG: "file", oh_apparmor.LICENSE: "file"})
        without = declared(apparmor=False)
        self.assertEqual(without[oh_apparmor.CONFIG].cls, "file")
        self.assertNotIn(oh_apparmor.POLICY, without, "a disabled AppArmor installs no policy")

    def test_state_is_migrated_trees_records_and_shared_logs(self):
        runtime, access = ROOT / "runtime", ROOT / "access"
        classes = {path: item.cls for path, item in declared().items() if engine.below(path, ROOT)}
        self.assertEqual(classes, {
            **{runtime / name: "tree" for name in ("conf", "userdata", "addons", "metrics", "exporter/app", "atv/app",
                                                   "atv/venv", "atv/secrets", "secrets", "monitoring/grafana",
                                                   "monitoring/alertmanager", "monitoring/ntfy", "monitoring/secrets")},
            **{runtime / "deploy" / name: "file" for name in ("deployed.json", "deployed.pending.json", "atv-restart",
                                                               "metrics-restart")},
            **{runtime / name: "shared" for name in ("logs", "backups", "deploy/history", "textfile")},
            runtime / "monitoring/prometheus": "shared-pinned",
            access / "releases": "versioned", access / "state": "tree", access / "inputs": "tree",
            access / "tls-cert.pem": "file", access / "backups": "shared",
        })
        prometheus = declared()[runtime / "monitoring/prometheus"]
        self.assertEqual((prometheus.image, prometheus.value), ("prometheus", VERSIONS["prometheus"]["image"]["digest"]))

    def test_the_callback_relay_is_declared_once_its_map_exists(self):
        relay = UNITS / oh_units.ACCESS_CALLBACKS
        self.assertNotIn(relay, declared())
        for present in (ROOT / "access/inputs/callbacks.json", relay):
            with self.subTest(present=present):
                items = declared(present=(present,))
                self.assertEqual(items[relay].cls, "unit")
                self.assertIn(UNITS / "thermite.target.wants" / oh_units.ACCESS_CALLBACKS, items)

    def test_the_set_is_what_the_engine_accepts(self):
        items = [item.to_json() for item in declared().values()]
        tools = {name: f"/usr/bin/{name}" for name in engine.TOOLS}
        self.assertEqual(engine.validate_desired({"kind": "upgrade", "tools": tools, "items": items}, str(ROOT))["items"],
                         sorted(items, key=lambda item: item["path"]))

    def test_conflicting_or_nested_declarations_are_refused(self):
        class Declares(Step):
            def __init__(self, *items):
                self.items = items

            def desired(self, system):
                return list(self.items)

        tree = manifest.Item(ROOT / "runtime/conf", "tree")
        cases = ((Declares(tree), Declares(manifest.Item(tree.path, "shared")), "both tree and shared"),
                 (Declares(tree), Declares(manifest.Item(tree.path / "x", "file")), "items never nest"),
                 (Declares(manifest.Item(Path("/etc/passwd"), "file")), "outside what Thermite may own"))
        for *declaring, words in cases:
            with self.subTest(words=words), self.assertRaisesRegex(manifest.ManifestError, words):
                manifest.desired(declaring, system(), ROOT)
        self.assertEqual(manifest.desired([Declares(tree), Declares(tree)], system(), ROOT), [tree])


class Links(unittest.TestCase):
    def test_wants_links_come_from_our_own_install_section(self):
        text = deploy.render_atv_restart_path(HOST)
        self.assertEqual(manifest.wants_links(text, oh_units.ATV_RESTART_PATH), [manifest.link(oh_units.ATV_RESTART_PATH)])
        self.assertEqual(manifest.wants_links(deploy.render_target(), oh_units.TARGET), [],
                         "the target is wanted by multi-user.target: the boot guard")
        self.assertEqual(manifest.wants_links("[Unit]\nWantedBy=thermite.target\n", "x"), [],
                         "only [Install] says what enabling links")


class Installed(unittest.TestCase):
    def test_a_missing_manifest_is_none_and_a_damaged_one_refuses(self):
        found = system()
        self.assertIsNone(manifest.installed(found, ROOT))
        found.read_bytes.side_effect = lambda path: b"{}"
        with self.assertRaisesRegex(manifest.ManifestError, "is damaged"):
            manifest.installed(found, ROOT)
        found.read_bytes.side_effect = lambda path: None
        found.lexists.side_effect = lambda path: True
        with self.assertRaisesRegex(manifest.ManifestError, "cannot be read"):
            manifest.installed(found, ROOT)

    def test_presence_probes_only_fixed_paths(self):
        found = system(present=(ingress.RULE_FILE,))
        found.directory_entries.return_value = []
        self.assertEqual(manifest.thermite_present(context(), found), [ingress.RULE_FILE])
        probed = {Path(call.args[0]) for call in found.lexists.call_args_list}
        self.assertTrue(all(path.is_absolute() and "*" not in str(path) for path in probed))
        found.directory_entries.assert_called_once_with(ROOT)
        found.listdir.assert_not_called()

    def test_recovery_names_the_stop_the_moves_and_the_install(self):
        text = manifest.missing_recovery(context())
        for words in ("systemctl stop thermite.target thermite.slice", "/etc/thermite-native.nft",
                      "thermite install --config /home/owner/fixture-site", "Nothing was changed",
                      "#a-missing-manifest"):
            self.assertIn(words, text)

    def test_tools_are_found_only_in_the_engines_program_directories(self):
        found = system()
        found.stat.side_effect = lambda path: Stat(0, 0, 0o755, False, 0) if str(path).startswith("/usr/sbin/") else None
        found.which.return_value = "/usr/local/bin/podman"
        self.assertEqual(manifest.tools(found), {name: f"/usr/sbin/{name}" for name in engine.TOOLS})
        engine.check_tools(manifest.tools(found))
        found.stat.side_effect = lambda path: (Stat(0, 0, 0o755, False, 0) if str(path).startswith("/usr/bin/")
                                               and path.name != "podman" else None)
        self.assertIsNone(manifest.tools(found)["podman"], "never from PATH outside the program directories")
        found.stat.side_effect = lambda path: None
        with self.assertRaisesRegex(manifest.ManifestError, "no systemctl program"):
            manifest.tools(found)

    def test_transaction_ids_are_the_engines(self):
        self.assertRegex(manifest.transaction_id(lambda: 1793873700), r"^20261105T\d{6}Z-[0-9a-f]{6}$")
        self.assertTrue(engine.ID.fullmatch(manifest.transaction_id()))


if __name__ == "__main__":
    unittest.main()
