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
        self.assertIs(deploy.validate_host(host), host)
        with self.assertRaisesRegex(deploy.HostError, "unknown setting state_root"):
            deploy.validate_host({**host, "state_root": str(TOP)})


class StateDirectories(unittest.TestCase):
    def actions(self, host):
        return [action.argv for action in steps.StateDirectories(context()).actions(host)]

    def test_a_new_root_on_btrfs_is_a_root_owned_subvolume(self):
        host = Host({Path("/srv"): directory(0, 0, 0o755)})
        self.assertEqual(steps.StateDirectories(context()).check(host).detail, f"{TOP} does not exist")
        argv = self.actions(host)
        self.assertEqual(argv[:2], [("btrfs", "subvolume", "create", str(TOP)),
                                    ("install", "-d", "-o", "root", "-g", "root", "-m", "00755", str(TOP))])
        created = [word for command in argv[2:] for word in command]
        self.assertIn(str(RUNTIME), created)
        self.assertEqual({command[:5] for command in argv[2:]}, {("install", "-d", "-o", "9001", "-g")})
        # runtime/ is a plain directory; access/ belongs to the gateway step.
        self.assertFalse([command for command in argv if "subvolume" in command and str(TOP) not in command])
        self.assertFalse([word for word in created if word.startswith(str(ACCESS))])

    def test_a_new_root_elsewhere_is_a_directory_and_its_parent_is_made(self):
        argv = self.actions(Host(fstype="ext4"))
        self.assertEqual(argv[:3], [("mkdir", "-p", "/srv"), ("mkdir", str(TOP)),
                                    ("install", "-d", "-o", "root", "-g", "root", "-m", "00755", str(TOP))])

    def test_a_current_layout_needs_nothing(self):
        layout = deploy.state({})
        host = Host({TOP: directory(0, 0, 0o755), **runtime_directories(layout)})
        check = steps.StateDirectories(context()).check(host)
        self.assertTrue(check.done, check.detail)
        self.assertIn(f"{TOP}, owned by root", check.detail)
        self.assertEqual(self.actions(host), [])

    def test_a_root_not_owned_by_root_is_repaired_in_place(self):
        layout = deploy.state({})
        host = Host({TOP: directory(9001, 9001, 0o750), **runtime_directories(layout)})
        check = steps.StateDirectories(context()).check(host)
        self.assertFalse(check.done)
        self.assertEqual(check.detail, f"to create or fix: {TOP} (root, 0755)")
        self.assertEqual(self.actions(host), [("install", "-d", "-o", "root", "-g", "root", "-m", "00755", str(TOP))])

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
        plan = steps.Plan((), "5.2.3", "5.2.3", None, "fixture", ())
        for name, sequence in (("install", steps.install(context())),
                               ("upgrade", steps.upgrade(context(), plan, "fixture"))):
            types = [type(step) for step in sequence]
            for maker in (steps.Account, steps.StateDirectories, steps.MetricsDirectories):
                with self.subTest(sequence=name, step=maker.__name__):
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
        self.assertEqual([action.argv for action in steps.Account(context()).actions(host)],
                         [("usermod", "-d", str(RUNTIME), "-s", "/usr/bin/nologin", "thermite")])
        home = User("thermite", 9001, 9001, str(RUNTIME), "/usr/bin/nologin")
        self.assertEqual(steps.Account(context()).missing(Host(users={"thermite": home, 9001: home})), [])


class Purge(unittest.TestCase):
    def test_purge_deletes_only_the_runtime_directory_even_on_btrfs(self):
        for fstype in ("btrfs", "ext4"):
            with self.subTest(fstype=fstype):
                actions = steps.Purge(context()).actions(Host(fstype=fstype))
                self.assertEqual(actions[1].argv, ("rm", "-rf", "--one-file-system", "--", str(RUNTIME)))
                self.assertTrue(actions[1].root)
                self.assertNotIn("subvolume", " ".join(word for action in actions
                                                       for word in getattr(action, "argv", ())))


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
