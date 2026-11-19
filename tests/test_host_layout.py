"""The host layout: one root-owned installation root (instance/host.yaml root,
/srv/thermite by default), a btrfs subvolume where its parent is btrfs, with
the service-owned runtime/ and the gateway's access/ as plain directories.

Host facts are in-memory fixtures; no command runs and nothing needs root.
"""
from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))

import oh_deploy as deploy  # noqa: E402
import oh_host  # noqa: E402
from oh_access import AccessError, staging  # noqa: E402
from oh_cli import tui  # noqa: E402
from oh_install import steps  # noqa: E402
from oh_install.access import Gateway  # noqa: E402
from oh_install.system import Group, Stat, System, User  # noqa: E402
from tests.test_deploy import VERSIONS  # noqa: E402

TOP = Path("/srv/thermite")
RUNTIME = TOP / "runtime"
ACCESS = TOP / "access"


def context(**host):
    return steps.Context(AREA, {"openhab": {"version": "5.2.3"}},
                         {"uid": 9001, "gid": 9001, "timezone": "Etc/UTC", "http_port": 13602,
                          "https_port": 13603, **host},
                         "a" * 40, "b" * 64, "fixture", None,
                         application={"version": "0.1.0.dev0", "digest": "c" * 64})


class Host(System):
    """Only the facts these steps read: stat, listings, file systems, accounts."""

    def __init__(self, stats=None, *, fstype="btrfs", entries=None, users=None):
        self.stats = dict(stats or {})
        self.filesystem = fstype
        self.entries = dict(entries or {})
        self.users = dict(users or {})

    def stat(self, path):
        return self.stats.get(Path(path))

    def lexists(self, path):
        return Path(path) in self.stats

    def directory_entries(self, path):
        return self.entries.get(Path(path))

    def fstype(self, path):
        return self.filesystem

    def process_groups(self):
        return {9001}

    def login(self):
        return "operator"

    def user(self, key):
        return self.users.get(key)

    def group(self, key):
        return Group("thermite", 9001, ("operator",)) if key in ("thermite", 9001) else None

    def which(self, name):
        return "/usr/bin/nologin" if name == "nologin" else None


def directory(uid=9001, gid=9001, mode=0o750):
    return Stat(uid, gid, mode, True, 0)


def runtime_directories(layout):
    """Every runtime directory, as its owner and mode should be."""
    return {path: directory(mode=mode) for mode, paths in layout.directories() for path in paths}


class Paths(unittest.TestCase):
    def test_default_root_holds_runtime_and_access(self):
        installation = deploy.installation({})
        self.assertEqual((installation.root, installation.runtime, installation.access), (TOP, RUNTIME, ACCESS))
        self.assertEqual(deploy.state({}).root, RUNTIME)
        self.assertEqual(deploy.state({}).conf, RUNTIME / "conf")
        # Neither child is the other's ancestor.
        self.assertNotIn(RUNTIME, ACCESS.parents)
        self.assertNotIn(ACCESS, RUNTIME.parents)

    def test_a_configured_root_moves_both_children(self):
        host = {"root": "/srv/fixture-root"}
        self.assertEqual(deploy.installation(host).access, Path("/srv/fixture-root/access"))
        self.assertEqual(deploy.state(host).userdata, Path("/srv/fixture-root/runtime/userdata"))
        quadlet = deploy.render_quadlet(VERSIONS, {**context().host, **host})
        self.assertIn("Volume=/srv/fixture-root/runtime/conf:/openhab/conf:Z", quadlet)

    def test_every_rendered_host_path_is_below_runtime(self):
        units = deploy.render_units(VERSIONS, context().host)
        for name, text in units.items():
            for line in text.splitlines():
                if str(TOP) in line:
                    with self.subTest(unit=name, line=line):
                        self.assertNotIn(f"{TOP}/", line.replace(f"{RUNTIME}/", ""))

    def test_the_root_key_is_optional_and_state_root_is_unknown(self):
        host = {key: value for key, value in context().host.items()}
        self.assertIs(oh_host.validate(host), host)
        with self.assertRaisesRegex(oh_host.HostError, "unknown setting state_root"):
            oh_host.validate({**host, "state_root": str(TOP)})


class InstallationRoot(unittest.TestCase):
    def actions(self, host):
        return [action.argv for action in steps.InstallationRoot(context()).actions(host)]

    def test_a_new_root_on_btrfs_is_a_root_owned_subvolume(self):
        host = Host({Path("/srv"): directory(0, 0, 0o755)})
        self.assertEqual(steps.InstallationRoot(context()).check(host).detail, f"{TOP} does not exist")
        self.assertEqual(self.actions(host), [("btrfs", "subvolume", "create", str(TOP)),
                                              ("install", "-d", "-o", "root", "-g", "root", "-m", "00755", str(TOP))])

    def test_a_new_root_elsewhere_is_a_directory_and_its_parent_is_made(self):
        self.assertEqual(self.actions(Host(fstype="ext4")), [
            ("mkdir", "-p", "/srv"), ("mkdir", str(TOP)),
            ("install", "-d", "-o", "root", "-g", "root", "-m", "00755", str(TOP))])

    def test_a_root_not_owned_by_root_is_repaired_in_place(self):
        host = Host({TOP: directory(9001, 9001, 0o750)})
        check = steps.InstallationRoot(context()).check(host)
        self.assertFalse(check.done)
        self.assertEqual(check.detail, f"to fix: {TOP} (root, 0755)")
        self.assertEqual(self.actions(host), [("install", "-d", "-o", "root", "-g", "root", "-m", "00755", str(TOP))])

    def test_a_root_owned_by_root_needs_nothing(self):
        check = steps.InstallationRoot(context()).check(Host({TOP: directory(0, 0, 0o755)}))
        self.assertTrue(check.done, check.detail)

    def test_it_is_made_while_preparing_before_the_room_is_planned_inside_it(self):
        from oh_install import changeover
        self.assertIsInstance(changeover.preparation(context())[-1], steps.InstallationRoot)


class StateDirectories(unittest.TestCase):
    def actions(self, host):
        return [action.argv for action in steps.StateDirectories(context()).actions(host)]

    def test_a_new_root_gets_its_runtime_directories_but_no_subvolume(self):
        host = Host({TOP: directory(0, 0, 0o755)})
        argv = self.actions(host)
        created = [word for command in argv for word in command]
        self.assertIn(str(RUNTIME), created)
        self.assertEqual({command[:5] for command in argv}, {("install", "-d", "-o", "9001", "-g")})
        # runtime/ is a plain directory; access/ belongs to the gateway step.
        self.assertNotIn(str(TOP), created)
        self.assertFalse([word for word in created if word.startswith(str(ACCESS))])

    def test_a_current_layout_needs_nothing(self):
        layout = deploy.state({})
        host = Host({TOP: directory(0, 0, 0o755), **runtime_directories(layout)})
        check = steps.StateDirectories(context()).check(host)
        self.assertTrue(check.done, check.detail)
        self.assertIn(f"runtime directories in {TOP}", check.detail)
        self.assertEqual(self.actions(host), [])

    def test_an_install_without_logs_gets_the_setgid_group_directory(self):
        layout = deploy.state({})
        stats = {TOP: directory(0, 0, 0o755), **runtime_directories(layout)}
        del stats[layout.logs]
        check = steps.StateDirectories(context()).check(Host(stats))
        self.assertEqual(check.detail, "to create or fix: runtime/logs")
        self.assertEqual(self.actions(Host(stats)),
                         [("install", "-d", "-o", "9001", "-g", "9001", "-m", "02750", str(layout.logs))])

    def test_wrong_runtime_directories_are_named_below_the_root(self):
        layout = deploy.state({})
        stats = {TOP: directory(0, 0, 0o755), **runtime_directories(layout)}
        stats[layout.conf] = directory(mode=0o755)
        check = steps.StateDirectories(context()).check(Host(stats))
        self.assertEqual(check.detail, "to create or fix: runtime/conf")


class Mounts(unittest.TestCase):
    def test_openhab_bind_mounts_exist_before_its_quadlet_is_installed(self):
        # A failed run starts openHAB again, and systemd restarts it, on
        # whatever Quadlet is installed: never one whose source is missing.
        layout = deploy.state(context().host)
        made = {path for _, paths in layout.directories() for path in paths}
        sources = {Path(line.split("=", 1)[1].split(":", 1)[0])
                   for line in deploy.render_quadlet(VERSIONS, context().host).splitlines() if line.startswith("Volume=")}
        self.assertIn(layout.logs, sources)
        self.assertLessEqual(sources, made)
        from oh_install import changeover
        types = [type(step) for step in (*changeover.preparation(context()),
                                         *changeover.switching(context(), "20261105T101500Z-3f9a2c"))]
        for maker in (steps.Account, steps.StateDirectories, steps.MetricsDirectories):
            with self.subTest(step=maker.__name__):
                self.assertLess(types.index(maker), types.index(steps.Units))


class Ownership(unittest.TestCase):
    def test_the_root_may_hold_only_runtime_and_access(self):
        for names, owned in ((["access", "runtime"], True), (["runtime"], True), ([], True),
                             (["access", "conf", "runtime"], False), (None, False)):
            with self.subTest(names=names):
                host = Host({TOP: directory(0, 0, 0o755)}, entries={TOP: names})
                self.assertEqual(steps.installation_owned(context(), host), owned)
        self.assertTrue(steps.installation_owned(context(), Host()))


class Account(unittest.TestCase):
    def test_the_service_home_is_the_runtime_state(self):
        argv = [action.argv for action in steps.Account(context()).actions(Host())]
        self.assertIn(("useradd", "-r", "-u", "9001", "-g", "9001", "-d", str(RUNTIME), "-s", "/usr/bin/nologin",
                       "thermite"), argv)

    def test_another_home_is_moved_to_the_runtime_state(self):
        elsewhere = User("thermite", 9001, 9001, "/srv/elsewhere", "/usr/bin/nologin")
        host = Host(users={"thermite": elsewhere, 9001: elsewhere})
        self.assertEqual(steps.Account(context()).missing(host), ["settings"])
        self.assertEqual([action.argv for action in steps.Account(context()).actions(host)][:1],
                         [("usermod", "-d", str(RUNTIME), "-s", "/usr/bin/nologin", "thermite")])
        home = User("thermite", 9001, 9001, str(RUNTIME), "/usr/bin/nologin")
        self.assertEqual(steps.Account(context()).missing(Host(users={"thermite": home, 9001: home})), [])


class NodeExporterAccount(unittest.TestCase):
    NODE = User("thermite-node", 9000, 9000, "/nonexistent", "/usr/bin/nologin")
    SERVICE = User("thermite", 9001, 9001, str(RUNTIME), "/usr/bin/nologin")

    def host(self, users, groups=()):
        host = Host(users={**{key: self.SERVICE for key in ("thermite", 9001)}, **users})
        known = {**{key: Group("thermite", 9001, ("operator",)) for key in ("thermite", 9001)},
                 **{key: group for group in groups for key in (group.name, group.gid)}}
        host.group = known.get
        return host

    def test_it_is_made_one_below_the_service_account_owning_nothing(self):
        step = steps.Account(context())
        host = self.host({})
        self.assertEqual(step.missing(host), [], "the service account's own custody does not depend on it")
        self.assertFalse(step.check(host).done)
        self.assertIn("group thermite-node (9000), user thermite-node (9000)", step.check(host).detail)
        self.assertEqual([action.argv for action in step.actions(host)],
                         [("groupadd", "-g", "9000", "thermite-node"),
                          ("useradd", "-r", "-u", "9000", "-g", "9000", "-d", "/nonexistent", "-s", "/usr/bin/nologin",
                           "thermite-node")])
        made = self.host({"thermite-node": self.NODE, 9000: self.NODE}, [Group("thermite-node", 9000)])
        self.assertEqual(step.actions(made), [])
        self.assertEqual(step.check(made), steps.Check(True, "thermite 9001:9001, operator a member; thermite-node 9000:9000"))

    def test_host_yaml_may_choose_its_number(self):
        step = steps.Account(context(metrics={"node_exporter_uid": 61000}))
        self.assertEqual([action.argv[:3] for action in step.actions(self.host({}))],
                         [("groupadd", "-g", "61000"), ("useradd", "-r", "-u")])
        self.assertIn("61000", step.actions(self.host({}))[1].argv)

    def test_a_number_or_a_name_that_is_taken_stops_before_anything_is_made(self):
        step = steps.Account(context())
        other = User("alice", 9000, 1000, "/home/alice", "/bin/sh")
        for host, said in ((self.host({9000: other}),
                            "uid 9000, for node_exporter's own account thermite-node, belongs to alice"),
                           (self.host({}, [Group("staff", 9000)]),
                            "gid 9000, for node_exporter's own account thermite-node, belongs to staff"),
                           (self.host({"thermite-node": User("thermite-node", 990, 9000)}),
                            "thermite-node exists with another uid or gid than 9000"),
                           (self.host({"thermite-node": User("thermite-node", 9000, 100), 9000: User("thermite-node", 9000, 100)}),
                            "thermite-node exists with another uid or gid than 9000")):
            with self.subTest(said=said):
                check = step.check(host)
                self.assertFalse(check.done)
                self.assertIn(said, check.problem)
                self.assertIn("metrics: node_exporter_uid in instance/host.yaml", check.problem)


class TextfileDirectory(unittest.TestCase):
    def test_textfiles_an_older_release_left_unreadable_are_removed_never_changed(self):
        ctx = context()
        layout, step = ctx.layout, steps.MetricsDirectories(ctx)
        stats = {path: Stat(9001, 9001, mode, True, 0.0) for mode, paths in layout.metrics_directories() for path in paths}
        self.assertEqual(stats[layout.textfile].mode, 0o2775)
        files = {"thermite-access.prom": 0o644, "thermite-deploy.prom": 0o660, "thermite-access-callbacks.prom": 0o640,
                 ".thermite-access.prom.7.tmp": 0o600}
        stats.update({layout.textfile / name: Stat(9001, 9001, mode, False, 0.0) for name, mode in files.items()})
        host = Host(stats, entries={layout.textfile: sorted(files)})
        check = step.check(host)
        self.assertFalse(check.done)
        self.assertEqual(check.detail, "to remove, unreadable to node_exporter: textfile/thermite-access-callbacks.prom, "
                                       "textfile/thermite-deploy.prom")
        self.assertEqual([action.argv for action in step.actions(host)],
                         [("rm", "-f", "--", str(layout.textfile / "thermite-access-callbacks.prom"),
                           str(layout.textfile / "thermite-deploy.prom"))])
        for name in ("thermite-deploy.prom", "thermite-access-callbacks.prom"):
            del host.stats[layout.textfile / name]
        self.assertTrue(step.check(host).done)
        self.assertEqual(step.actions(host), [])
        # The directory as the release before made it is made again with its new mode.
        host.stats[layout.textfile] = Stat(9001, 9001, 0o2770, True, 0.0)
        self.assertEqual([action.argv for action in step.actions(host)],
                         [("install", "-d", "-o", "9001", "-g", "9001", "-m", "02775", str(layout.textfile))])


class GatewayRoot(unittest.TestCase):
    def test_the_gateway_root_is_access_in_the_installation_root(self):
        self.assertEqual(Gateway(context()).root, ACCESS)
        self.assertEqual(Gateway(context(root="/srv/fixture-root")).root, Path("/srv/fixture-root/access"))

    def test_terminal_trust_names_the_default_gateway_root(self):
        self.assertEqual(Path(tui.EXAMPLE_AUTHORITY), ACCESS / tui.AUTHORITY)
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as temporary:
            access = Path(temporary) / "thermite/access"
            release = access / "releases/fixture-release"
            release.mkdir(parents=True)
            (access / tui.AUTHORITY).write_text("fixture authority\n")
            unit = Path(temporary) / "thermite-access.service"
            unit.write_text(f"[Service]\nUser=thermite\nWorkingDirectory={release}\n")
            with mock.patch.object(tui, "UNIT", unit), mock.patch.object(tui, "installed_profile", return_value=None):
                self.assertEqual(tui.installed_authority(), str(access / tui.AUTHORITY))

    def test_access_parents_pass_custody_only_outside_a_writable_runtime(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as temporary:
            top = Path(temporary) / "thermite"
            (top / "runtime").mkdir(parents=True, mode=0o750)
            (top / "access/releases").mkdir(parents=True)
            (top / "access/state").mkdir(mode=0o700)
            for path in (top, top / "access", top / "access/releases"):
                path.chmod(0o755)
            for parent in (top / "access/releases", top / "access"):
                staging._trusted_ancestors(parent, "Release and state")
            staging._service_parents(top / "access/releases/next", top / "access/state", os.geteuid(), os.getegid())
            # Below a group-writable service directory the gateway is refused,
            # which is why access/ is runtime/'s sibling, not its child.
            nested = top / "runtime/access/releases"
            nested.mkdir(parents=True)
            (top / "runtime").chmod(0o2770)
            with self.assertRaisesRegex(AccessError, "ancestors must be owned by root or the operator"):
                staging._trusted_ancestors(nested, "Release and state")


if __name__ == "__main__":
    unittest.main()
