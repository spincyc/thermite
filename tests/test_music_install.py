"""Music Assistant installs, upgrades, starts, stops, reports and goes away
like Thermite's other contained services, on a host with music: alone.

Fictional site and host; nothing on this host is read or changed.
"""
from __future__ import annotations

from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))

import oh_deploy as deploy  # noqa: E402
import oh_service  # noqa: E402
import oh_units  # noqa: E402
from oh_access import native_boundary  # noqa: E402
from oh_cli import status, uninstall  # noqa: E402
from oh_install import changeover, ingress, manifest, ownership, steps  # noqa: E402
from oh_install.system import Stat, System, Unit, UnitFile, UnitOrigin  # noqa: E402
from tests import txn_fakes  # noqa: E402
from tests.test_deploy import HOST, MUSIC_HOST, VERSIONS  # noqa: E402
from tests.test_install_manifest import ROOT, context  # noqa: E402

#: A fictional install transaction's id.
TRANSACTION = "20261105T101500Z-3f9a2c"
MUSIC = ROOT / "runtime/music"
QUADLET = deploy.INSTALLED[deploy.MUSIC_QUADLET]
DROPIN = deploy.INSTALLED[deploy.CONTAINER_DROPINS[oh_units.MUSIC]]


def host_system(files=None, stats=None):
    """A host with these unit files and these stats, and nothing else."""
    found = Mock(spec=System)
    files, stats = dict(files or {}), dict(stats or {})
    found.read_bytes.side_effect = lambda path: files.get(Path(path))
    found.stat.side_effect = lambda path: stats.get(Path(path))
    found.lexists.side_effect = lambda path: Path(path) in files or Path(path) in stats
    found.unit.return_value = Unit("active", "enabled", since=10.0)
    found.unit_origins.side_effect = lambda names: {name: UnitOrigin(load_state="not-found") for name in names}
    found.process_groups.return_value = {9001}
    found.directory_entries.return_value = None
    return found


class Sequence(unittest.TestCase):
    def test_its_state_and_unit_come_with_the_others_and_its_readiness_before_health(self):
        types = [type(step) for step in changeover.switching(context(base=MUSIC_HOST), TRANSACTION)]
        self.assertLess(types.index(steps.MetricsDirectories), types.index(steps.MusicDirectories))
        self.assertLess(types.index(steps.MusicDirectories), types.index(steps.Units))
        self.assertLess(types.index(ingress.NativeIngress), types.index(steps.MusicServices))
        self.assertLess(types.index(steps.MusicServices), types.index(steps.StartTarget))
        verification = [type(step) for step in changeover.verification(context(base=MUSIC_HOST))]
        self.assertLess(verification.index(steps.MusicAnswer), verification.index(steps.Health))

    def test_a_host_without_music_runs_none_of_its_steps(self):
        music = {steps.MusicDirectories, steps.MusicServices, steps.MusicAnswer}
        for sequence in (changeover.switching(context(), TRANSACTION), changeover.verification(context())):
            self.assertFalse({type(step) for step in sequence} & music)

    def test_halt_stops_it_with_the_rest_of_thermite(self):
        self.assertIn(oh_units.MUSIC, steps.Halt.UNITS)


class Context(unittest.TestCase):
    def test_a_host_without_music_has_none_of_its_units_ports_or_image(self):
        without, with_music = context(), context(base=MUSIC_HOST)
        for name in deploy.MUSIC_UNITS:
            self.assertIsNone(without.unit(name))
            self.assertEqual(with_music.unit(name), with_music.prepared[name].data)
        self.assertNotIn(oh_units.MUSIC, without.ports())
        self.assertEqual(with_music.ports()[oh_units.MUSIC], [13627, 13628])
        self.assertNotIn("music_assistant", manifest.image_names(without))
        self.assertIn(deploy.image(VERSIONS, "music_assistant"), manifest.images(with_music))


class State(unittest.TestCase):
    def owned(self, ctx, extra=None):
        """Every state directory but Music Assistant's, as Thermite made it."""
        layout = ctx.layout
        return {path: Stat(9001, 9001, mode, True, 0)
                for mode, paths in (*layout.directories(), *layout.metrics_directories()) for path in paths} | (extra or {})

    def test_it_is_made_private_and_service_owned(self):
        found = host_system()
        step = steps.MusicDirectories(context(base=MUSIC_HOST))
        self.assertEqual(step.wrong(found), [(0o700, MUSIC)])
        self.assertEqual(step.check(found).detail, "to create or fix: runtime/music")
        self.assertEqual([action.argv for action in step.actions(found)],
                         [("install", "-d", "-o", "9001", "-g", "9001", "-m", "00700", str(MUSIC))])
        self.assertTrue(step.check(host_system(stats={MUSIC: Stat(9001, 9001, 0o700, True, 0)})).done)
        self.assertEqual(step.wrong(host_system(stats={MUSIC: Stat(9001, 9001, 0o750, True, 0)})), [(0o700, MUSIC)])

    def test_made_before_the_owner_logs_in_again_it_counts_as_done(self):
        # runtime/ (0750) hides what was made until the owner has the group.
        for step in (steps.StateDirectories(context()), steps.MetricsDirectories(context()),
                     steps.MusicDirectories(context(base=MUSIC_HOST))):
            with self.subTest(step=type(step).__name__):
                found = host_system()
                found.process_groups.return_value = set()
                self.assertTrue(step.actions(found))
                check = step.check(found)
                self.assertTrue(check.done)
                self.assertIn(f"{step.made} installed for thermite; log in again for group access", check.detail)
                found.process_groups.return_value = {9001}
                self.assertFalse(step.check(found).done, "with the group, a missing one is missing")

    def test_what_a_version_with_music_left_stays_once_it_surely_is_there(self):
        def declared(found):
            return {item.path: item.cls for item in steps.StateDirectories(context()).desired(found)}

        hidden = host_system()
        hidden.lexists.return_value = True
        hidden.lexists.side_effect = None
        with patch.object(manifest, "installed", return_value=None):
            self.assertNotIn(MUSIC, declared(hidden), "runtime/ hides it before the owner logs in again")
            self.assertEqual(declared(host_system(stats={MUSIC: Stat(9001, 9001, 0o700, True, 0)}))[MUSIC], "shared")
        recorded = {"items": [{"path": str(MUSIC), "class": "shared-pinned"}]}
        with patch.object(manifest, "installed", return_value=recorded):
            self.assertEqual(declared(hidden)[MUSIC], "shared", "the installed manifest says it is there")
        with patch.object(manifest, "installed", side_effect=manifest.ManifestError("damaged")):
            self.assertNotIn(MUSIC, declared(hidden))
        self.assertNotIn(MUSIC, {item.path for item in steps.StateDirectories(context(base=MUSIC_HOST)).desired(
            host_system(stats={MUSIC: Stat(9001, 9001, 0o700, True, 0)}))}, "MusicDirectories pins it")

    def test_a_clean_install_deletes_it_and_says_so(self):
        for host, cls in ((MUSIC_HOST, "shared-pinned"), (HOST, "shared")):
            with self.subTest(music=host is MUSIC_HOST):
                ctx = context(base=host)
                with patch.object(steps, "NativeDeadline"):
                    work = changeover.install_work(ctx, TRANSACTION, kind="clean")
                declared = [manifest.Item(MUSIC, cls), manifest.Item(ctx.layout.logs, "shared")]
                with patch.object(manifest, "desired", return_value=declared):
                    items = {item.path: item.cls for item in work.items(None)}
                self.assertEqual(items, {MUSIC: "tree-move", ctx.layout.logs: "shared"})
                found = host_system(stats={MUSIC: Stat(9001, 9001, 0o700, True, 0)})
                said = []
                found.say.side_effect = said.append
                found.ask.return_value = str(ctx.layout.root)
                self.assertEqual(work.confirm.confirm(found), 0)
                self.assertIn("the deploy's records, Music Assistant's settings and library and monitoring data; it", said[0])
        with patch.object(manifest, "installed", return_value=None):
            said = []
            found = host_system()
            found.say.side_effect = said.append
            found.ask.return_value = str(context().layout.root)
            changeover.install_work(context(), TRANSACTION, kind="clean").confirm.confirm(found)
        self.assertNotIn("Music Assistant", said[0])

    def test_a_missing_music_directory_needs_the_full_repair(self):
        ctx = context(base=MUSIC_HOST)
        with patch.object(steps, "service_receipt", return_value=True), \
                patch.object(steps.Account, "missing", return_value=[]):
            self.assertTrue(steps.NativePreparation(ctx).custody(host_system(stats=self.owned(ctx))))
            self.assertFalse(steps.NativePreparation(ctx).custody(
                host_system(stats=self.owned(ctx, {MUSIC: Stat(9001, 9001, 0o700, True, 0)}))))
            self.assertFalse(steps.NativePreparation(context()).custody(host_system(stats=self.owned(context()))))

    def test_an_empty_resumed_music_directory_counts_as_thermites(self):
        runtime = context().layout.root
        found = host_system(stats={runtime: Stat(9001, 9001, 0o750, True, 0), MUSIC: Stat(9001, 9001, 0o700, True, 0)})
        entries = {runtime: ["music"], MUSIC: []}
        found.directory_entries.side_effect = lambda path: entries.get(Path(path))
        found.safe_directory.return_value = True
        with patch.object(steps, "service_receipt", return_value=False), \
                patch.object(ownership, "inspect", return_value=ownership.Owned()):
            self.assertTrue(steps.state_owned(context(base=MUSIC_HOST), found))
            entries[MUSIC] = ["settings.json"]
            self.assertFalse(steps.state_owned(context(base=MUSIC_HOST), found), "its data needs the receipt")

    def test_the_full_repair_keeps_it_private(self):
        self.assertIn("music", oh_service.NATIVE_ROOTS)
        self.assertEqual(oh_service.desired_mode("music", 0o755, True), 0o700)
        self.assertEqual(oh_service.desired_mode("music/settings.json", 0o644, False), 0o600)
        self.assertEqual(oh_service.desired_mode("music/.cache/thumbnails", 0o750, True), 0o700)


class Units(unittest.TestCase):
    def test_an_installed_unit_a_host_no_longer_has_is_removed(self):
        ctx = context()
        rendered = {deploy.INSTALLED[name]: ctx.unit(name) for name in deploy.host_units(HOST)}
        found = host_system({**rendered, QUADLET: b"music\n", DROPIN: b"dropin\n"},
                            stats={QUADLET.parent: Stat(0, 0, 0o755, True, 0), DROPIN.parent: Stat(0, 0, 0o755, True, 0)})
        step = steps.Units(ctx)
        self.assertEqual(step.stale(found), sorted(deploy.MUSIC_UNITS))
        self.assertFalse(step.check(found).done)
        argv = [action.argv for action in step.actions(found) if hasattr(action, "argv")]
        self.assertEqual(argv[0], ("rm", "-f", "--", *(str(deploy.INSTALLED[name]) for name in sorted(deploy.MUSIC_UNITS))))
        self.assertFalse([command for command in argv[1:] if command[:2] == ("install", "-m")])
        self.assertIn(("systemctl", "daemon-reload"), argv)
        self.assertEqual(step.stale(host_system(rendered)), [])

    def test_a_music_host_installs_its_unit_and_records_it(self):
        ctx = context(base=MUSIC_HOST)
        found = host_system(stats={QUADLET.parent: Stat(0, 0, 0o755, True, 0)})
        self.assertIn(deploy.MUSIC_QUADLET, steps.Units(ctx).stale(found))
        installed = [word for action in steps.Units(ctx).actions(found)
                     for word in getattr(action, "argv", ())[5:] if getattr(action, "argv", ())[:2] == ("install", "-m")]
        self.assertIn(str(ctx.configuration / deploy.MUSIC_QUADLET), installed)
        self.assertIn(QUADLET, [item.path for item in steps.Units(ctx).desired(found)])
        self.assertNotIn(QUADLET, [item.path for item in steps.Units(context()).desired(found)])

    def test_a_unit_this_host_no_longer_renders_is_proven_by_the_record(self):
        ctx = context()
        music = deploy.render_units(VERSIONS, MUSIC_HOST)[deploy.MUSIC_QUADLET].encode()

        def host(record):
            found = Mock(spec=System)
            found.safe_unit_parent.return_value = True
            found.lexists.side_effect = lambda path: Path(path) == QUADLET
            found.unit_file.side_effect = lambda path: (UnitFile(record, 0, 0o644) if Path(path) == ownership.INSTALLED
                                                        else UnitFile(music, 0, 0o644) if Path(path) == QUADLET else None)
            found.unit_origins.side_effect = lambda names: {
                name: UnitOrigin(source=str(QUADLET)) if name == oh_units.MUSIC else UnitOrigin(load_state="not-found")
                for name in names}
            return found

        owned = ownership.inspect(ctx, host(txn_fakes.manifest({QUADLET: music}, root=ctx.installation.root)))
        self.assertEqual(owned.problems, ())
        self.assertIn(oh_units.MUSIC, owned.units())
        problems = ownership.inspect(ctx, host(txn_fakes.manifest({}, root=ctx.installation.root))).problems
        self.assertIn(f"cannot prove Thermite ownership of {QUADLET}", "; ".join(problems))


class Services(unittest.TestCase):
    def test_it_starts_pulled(self):
        found = host_system()
        found.unit.return_value = Unit("inactive", "disabled")
        found.capture.return_value = (0, "--policy")
        step = steps.MusicServices(context(base=MUSIC_HOST))
        self.assertEqual(step.plan(found), ([oh_units.MUSIC], []))
        self.assertEqual([action.argv for action in step.actions(found)],
                         [("podman", "pull", "--policy=missing", deploy.image(VERSIONS, "music_assistant")),
                          ("systemctl", "start", oh_units.MUSIC)])

    def test_a_changed_unit_restarts_it(self):
        found = host_system(stats={QUADLET: Stat(0, 0, 0o644, False, 20.0)})
        self.assertEqual(steps.MusicServices(context(base=MUSIC_HOST)).plan(found), ([], [oh_units.MUSIC]))
        found = host_system(stats={QUADLET: Stat(0, 0, 0o644, False, 5.0)})
        self.assertTrue(steps.MusicServices(context(base=MUSIC_HOST)).check(found).done)


class Readiness(unittest.TestCase):
    def health(self, status="running", addresses=("127.0.0.1",)):
        return deploy.MusicHealth("http://127.0.0.1:13627", status, "2.10.12", addresses,
                                  "http://127.0.0.1:13628", 200)

    def test_it_waits_for_music_assistant_on_loopback(self):
        found = host_system()
        step = steps.MusicAnswer(context(base=MUSIC_HOST))
        for health, done in ((self.health(), True), (self.health("starting"), False),
                             (self.health(addresses=("0.0.0.0",)), False)):
            with self.subTest(health=health):
                found.music_health.return_value = health
                self.assertEqual(step.check(found).done, done)
        found.music_health.return_value = self.health()
        found.clock.return_value = 0
        self.assertEqual(step.wait(found), 0)
        self.assertTrue(step.after_actions(found).done)

    def test_a_wait_that_runs_out_says_where_to_look(self):
        found = host_system()
        found.music_health.return_value = self.health(addresses=("0.0.0.0",))
        clock = iter(range(0, 10 * steps.MUSIC_WAIT, steps.POLL))
        found.clock.side_effect = lambda: next(clock)
        said = []
        found.say.side_effect = said.append
        self.assertEqual(steps.MusicAnswer(context(base=MUSIC_HOST)).wait(found), 1)
        self.assertIn("listens beyond loopback", said[0])
        self.assertIn("journalctl -u thermite-music.service", said[-1])


class Reports(unittest.TestCase):
    def show(self, host, *, installed=True, health=None):
        report, problems, said = {}, [], []
        with patch.object(status.deploy, "music_installed", return_value=installed), \
                patch.object(status.deploy, "music_health", return_value=health):
            status.show_music(host, report, said.append, problems)
        return report, problems, said

    def test_status_shows_music_assistant_and_a_problem_when_it_is_not_on_loopback(self):
        running = deploy.MusicHealth("http://127.0.0.1:13627", "running", "2.10.12", ("127.0.0.1",),
                                     "http://127.0.0.1:13628", 200)
        report, problems, said = self.show(MUSIC_HOST, health=running)
        self.assertEqual((report["music"]["status"], report["music"]["loopback"], problems), ("running", True, []))
        self.assertEqual(said, ["music    " + running.describe()])
        wide = deploy.MusicHealth("http://127.0.0.1:13627", "running", "2.10.12", ("0.0.0.0",),
                                  "http://127.0.0.1:13628", 200)
        self.assertEqual(self.show(MUSIC_HOST, health=wide)[1], ["music: " + wide.describe()])
        report, problems, said = self.show(MUSIC_HOST, installed=False)
        self.assertEqual((report, problems), ({"music": None}, []))
        self.assertIn("thermite upgrade installs it", said[0])
        self.assertEqual(self.show(HOST), ({}, [], []))

    def test_uninstall_says_it_keeps_music_assistants_state(self):
        self.assertIn("monitoring data, Music Assistant's settings and library)",
                      uninstall.kept(context(), purge=False, music=True))
        self.assertNotIn("Music Assistant", uninstall.kept(context(), purge=False))
        self.assertTrue(steps.music_state(context(base=MUSIC_HOST), host_system()))
        with patch.object(manifest, "installed", return_value=None):
            self.assertFalse(steps.music_state(context(), host_system()))
            self.assertTrue(steps.music_state(context(), host_system(stats={MUSIC: Stat(9001, 9001, 0o700, True, 0)})))


class Boundary(unittest.TestCase):
    def test_the_native_firewall_closes_its_api_to_the_lan(self):
        ports = native_boundary.firewall_ports(MUSIC_HOST)
        self.assertEqual(ports - native_boundary.firewall_ports(HOST), {13627})
        self.assertNotIn(13628, ports, "the speakers fetch the streams")
        text = ingress.rule_text(MUSIC_HOST)
        self.assertIn(f"# Ports: {HOST['http_port']},{HOST['https_port']},13627\n", text)
        self.assertEqual(ingress.rule_ports(text.encode()), ports)
        self.assertEqual(ingress.rule_ports(ingress.rule_text(HOST).encode()), native_boundary.firewall_ports(HOST))
        moved = {**MUSIC_HOST, "music": {"port": 14627}}
        self.assertIn(14627, ingress.rule_ports(ingress.rule_text(moved).encode()))

    def test_a_port_the_header_does_not_name_is_someones_edit(self):
        plain = native_boundary.firewall_ports(HOST)
        edited = ingress.rule_text(HOST).replace(native_boundary.filter_rules(plain),
                                                 native_boundary.filter_rules({*plain, 22}))
        with self.assertRaisesRegex(ValueError, "customized"):
            ingress.rule_ports(edited.encode())
        text = ingress.rule_text(MUSIC_HOST)
        with self.assertRaisesRegex(ValueError, "customized"):
            ingress.rule_ports(text.replace(",13627\n", ",13629\n").encode())


if __name__ == "__main__":
    unittest.main()
