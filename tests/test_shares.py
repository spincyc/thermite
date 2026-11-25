"""Network shares (lib/oh_shares): their declaration, the units and logins
they render to, and how the install owns, rolls back and removes them.

Fictional site, servers and logins; nothing on this host is read or changed.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))

import oh_deploy as deploy  # noqa: E402
import oh_host  # noqa: E402
import oh_iso  # noqa: E402
import oh_shares as shares  # noqa: E402
import oh_units  # noqa: E402
from oh_install import changeover, engine, manifest, ownership, secrets, steps  # noqa: E402
from oh_install.model import Command, Task  # noqa: E402
from oh_install.system import Stat, System, Unit, UnitFile, UnitOrigin  # noqa: E402
from tests import txn_fakes as fakes  # noqa: E402
from tests.test_deploy import HOST, VERSIONS  # noqa: E402
from tests.test_install_manifest import ROOT, context, system as installing  # noqa: E402

UNITS = Path("/etc/systemd/system")
#: An SMB share on a named NAS, and an NFS export on an address.
MUSIC = {"type": "cifs", "server": "nas.example", "share": "music", "where": "/mnt/nas-music", "version": "3.1.1"}
PHOTOS = {"type": "nfs", "server": "192.0.2.20", "export": "/volume1/photos", "where": "/media/photos",
          "writable": True}
SHARE_HOST = {**HOST, "shares": {"music": MUSIC, "photos": PHOTOS},
              "music": {"libraries": {"flac": "/mnt/nas-music/flac"}}}
#: systemd's name for /mnt/nas-music: a hyphen in a path is escaped.
MUSIC_UNIT = "mnt-nas\\x2dmusic.mount"
GATE = UNITS / oh_units.SHARE_GATE


def declared(**share) -> dict:
    return shares.settings({"shares": {"music": {**MUSIC, **share}}})


class Declaration(unittest.TestCase):
    def test_a_share_is_read_with_its_defaults(self):
        found = shares.settings(SHARE_HOST)
        self.assertEqual(found["music"], shares.Share("music", "cifs", "nas.example", "music", "/mnt/nas-music",
                                                       False, "3.1.1"))
        self.assertEqual((found["music"].what, found["photos"].what),
                         ("//nas.example/music", "192.0.2.20:/volume1/photos"))
        self.assertEqual(shares.settings(HOST), {})
        self.assertEqual(shares.settings({**HOST, "shares": None}), {})
        self.assertEqual(oh_host.validate(SHARE_HOST), SHARE_HOST)

    def test_bad_declarations_are_refused_with_what_is_wrong(self):
        cases = [
            ({"shares": ["music"]}, "shares must map a name"),
            ({"shares": {"Music": MUSIC}}, "'Music' is not a name"),
            ({"shares": {"music": "//nas.example/music"}}, "shares.music must be a mapping"),
            ({"shares": {"music": {**MUSIC, "options": "rw"}}}, "unknown shares.music setting options"),
            ({"shares": {"music": {**MUSIC, "type": "smbfs"}}}, "type must be one of cifs, nfs"),
            ({"shares": {"music": {**MUSIC, "export": "/music"}}}, "is cifs: name what the server exports as share"),
            ({"shares": {"music": {**PHOTOS, "share": "photos"}}}, "is nfs: name what the server exports as export"),
            ({"shares": {"music": {**MUSIC, "server": "nas_1.example"}}}, "server must be a host name"),
            ({"shares": {"music": {**MUSIC, "server": "fd00::20"}}}, "server must be a host name"),
            ({"shares": {"music": {**MUSIC, "server": "192.0.2"}}}, "server must be a host name"),
            ({"shares": {"music": {**MUSIC, "server": "nas.example;reboot"}}}, "server must be a host name"),
            ({"shares": {"music": {**MUSIC, "share": "my music"}}}, "share must be the share's name"),
            ({"shares": {"music": {**MUSIC, "share": "music/../other"}}}, "share must be the share's name"),
            ({"shares": {"music": {**PHOTOS, "export": "volume1/photos"}}}, "export must be the exported directory"),
            ({"shares": {"music": {**MUSIC, "where": "/srv/music"}}}, "where must be .* below /mnt or /media"),
            ({"shares": {"music": {**MUSIC, "where": "/mnt"}}}, "where must be"),
            ({"shares": {"music": {**MUSIC, "where": "/mnt/nas/../music"}}}, "where must be"),
            ({"shares": {"music": {**MUSIC, "where": "/mnt/nas/"}}}, "where must be"),
            ({"shares": {"music": {**MUSIC, "where": "/mnt/a,b"}}}, "where must be"),
            ({"shares": {"music": {**MUSIC, "writable": "yes"}}}, "writable must be true or false"),
            ({"shares": {"music": {**MUSIC, "version": 3.0}}}, 'version must be a quoted protocol version such as "3.1.1"'),
            ({"shares": {"music": {**MUSIC, "version": "3,sec=none"}}}, "version must be"),
            ({"shares": {"music": MUSIC, "other": {**PHOTOS, "where": MUSIC["where"]}}},
             "shares.other.where is shares.music.where"),
            ({"shares": {"music": MUSIC, "other": {**PHOTOS, "where": MUSIC["where"] + "/flac"}}},
             "shares.other.where is inside shares.music"),
        ]
        for host, expected in cases:
            with self.subTest(expected):
                with self.assertRaisesRegex(ValueError, expected):
                    shares.settings(host)
                with self.assertRaisesRegex(oh_host.HostError, "instance/host.yaml: "):
                    oh_host.validate({**HOST, **host})

    def test_a_share_never_overlaps_the_installation_root(self):
        for where in ("/mnt/thermite", "/mnt/thermite/music", "/mnt"):
            host = {**HOST, "root": "/mnt/thermite", "shares": {"music": {**MUSIC, "where": where}}}
            with self.subTest(where), self.assertRaises(oh_host.HostError):
                oh_host.validate(host)
        oh_host.validate({**HOST, "root": "/mnt/thermite", "shares": {"music": {**MUSIC, "where": "/mnt/music"}}})

    def test_the_unit_name_is_systemds_for_the_mount_point(self):
        expected = {"/mnt/nas/music": "mnt-nas-music", "/mnt/nas-music": "mnt-nas\\x2dmusic",
                    "/media/a+b@c.d/x_y": "media-a\\x2bb\\x40c.d-x_y", "/mnt/.hidden": "mnt-.hidden"}
        for path, name in expected.items():
            self.assertEqual(shares.escape_path(path), name)
        escape = shutil.which("systemd-escape")
        if escape:
            for path, name in expected.items():
                self.assertEqual(subprocess.run([escape, "--path", path], capture_output=True, text=True,
                                                check=True).stdout.strip(), name)
        self.assertEqual(declared()["music"].unit, MUSIC_UNIT)
        # The install engine owns mount units of these names alone.
        self.assertTrue(engine.MOUNT.fullmatch(MUSIC_UNIT))
        self.assertTrue(engine.UNIT.fullmatch(MUSIC_UNIT))

    def test_an_address_needs_no_gate(self):
        found = shares.settings(SHARE_HOST)
        self.assertEqual(found["music"].gate, "thermite-share-gate@nas.example.service")
        self.assertIsNone(found["photos"].gate)
        self.assertTrue(shares.gated(SHARE_HOST))
        self.assertFalse(shares.gated({**HOST, "shares": {"photos": PHOTOS}}))

    def test_packages_follow_the_types_declared(self):
        self.assertEqual(shares.packages(SHARE_HOST, "pacman"), ("cifs-utils", "nfs-utils"))
        self.assertEqual(shares.packages(SHARE_HOST, "apt-get"), ("cifs-utils", "nfs-common"))
        self.assertEqual(shares.packages({**HOST, "shares": {"photos": PHOTOS}}, "apt-get"), ("nfs-common",))
        self.assertEqual(shares.packages(HOST, "pacman"), ())


class Rendered(unittest.TestCase):
    def setUp(self):
        self.units = deploy.render_units(VERSIONS, SHARE_HOST)

    def unit(self, name: str) -> list[str]:
        return [line for line in self.units[name].splitlines() if line and not line.startswith("#")]

    def test_each_share_is_a_mount_unit_installed_under_its_mount_points_name(self):
        wanted = deploy.host_units(SHARE_HOST)
        self.assertEqual(wanted["deploy/share-music.mount"], UNITS / MUSIC_UNIT)
        self.assertEqual(wanted["deploy/share-photos.mount"], UNITS / "media-photos.mount")
        self.assertEqual(wanted[deploy.SHARE_GATE], GATE)
        self.assertTrue(set(wanted) <= set(self.units))
        for name in ("deploy/share-music.mount", "deploy/share-photos.mount", deploy.SHARE_GATE):
            self.assertIn("\n".join(deploy.HEADER), self.units[name])

    def test_an_smb_mount_waits_for_its_server_and_reads_its_login(self):
        self.assertEqual(self.unit("deploy/share-music.mount"), [
            "[Unit]",
            "Description=Thermite share music (//nas.example/music)",
            "PartOf=thermite.target",
            "Requires=thermite-share-gate@nas.example.service",
            "After=thermite-share-gate@nas.example.service",
            "[Mount]",
            "What=//nas.example/music",
            "Where=/mnt/nas-music",
            "Type=cifs",
            "Options=ro,nosuid,nodev,_netdev,nofail,credentials=/etc/thermite/share-music.credentials,"
            "uid=9001,gid=9001,file_mode=0640,dir_mode=0750,vers=3.1.1",
            "TimeoutSec=30",
            "Slice=thermite.slice",
            "ForceUnmount=yes",
            "LazyUnmount=yes",
        ])
        # The login is root's file, outside the site and the application.
        self.assertEqual(shares.settings(SHARE_HOST)["music"].credentials,
                         shares.CREDENTIALS / "share-music.credentials")
        self.assertEqual(Path(secrets.SHARES), Path(shares.CREDENTIALS))

    def test_an_nfs_mount_on_an_address_has_no_gate_or_login(self):
        lines = self.unit("deploy/share-photos.mount")
        self.assertIn("Options=rw,nosuid,nodev,_netdev,nofail", lines)
        self.assertIn("What=192.0.2.20:/volume1/photos", lines)
        self.assertFalse([line for line in lines if line.startswith(("Requires=", "After="))])
        nfs = shares.Share("music", "nfs", "nas.example", "/music", "/mnt/music", version="4.2")
        self.assertEqual(nfs.options(9001, 9001), "ro,nosuid,nodev,_netdev,nofail,nfsvers=4.2")

    def test_the_gate_waits_a_bounded_time_for_the_name_after_the_network(self):
        lines = self.unit(deploy.SHARE_GATE)
        for line in ("PartOf=thermite.target", "Wants=network-online.target",
                     "After=network-online.target nss-lookup.target", "Slice=thermite.slice", "Type=oneshot",
                     "RemainAfterExit=yes", "TimeoutStartSec=60",
                     "ExecStart=/usr/bin/sh -c 'until /usr/bin/getent ahosts \"$1\" >/dev/null; "
                     "do /usr/bin/sleep 1; done' thermite-share-gate %i"):
            self.assertIn(line, lines)
        self.assertFalse([line for line in lines if line.startswith("WantedBy=")])

    def test_the_target_wants_the_shares_and_nothing_more_without_them(self):
        lines = self.unit(deploy.TARGET)
        self.assertEqual([line for line in lines if line.startswith("Wants=")],
                         [f"Wants={MUSIC_UNIT}", "Wants=media-photos.mount"])
        self.assertEqual(deploy.render_units(VERSIONS, HOST)[deploy.TARGET], deploy.render_target())
        self.assertNotIn(deploy.SHARE_GATE, deploy.render_units(VERSIONS, HOST))
        self.assertNotIn(deploy.SHARE_GATE, deploy.host_units(HOST))
        only_addresses = {**HOST, "shares": {"photos": PHOTOS}}
        self.assertNotIn(deploy.SHARE_GATE, deploy.render_units(VERSIONS, only_addresses))

    def test_music_orders_after_the_mount_and_the_mount_after_the_gate(self):
        # Quadlet turns each Volume= source into RequiresMountsFor=, which
        # requires and orders after every mount unit above the path.
        music = self.units[deploy.MUSIC_QUADLET]
        self.assertIn("Volume=/mnt/nas-music/flac:/media/flac:ro", music)
        where = shares.settings(SHARE_HOST)["music"].where
        self.assertTrue("/mnt/nas-music/flac".startswith(where + "/"))
        mount = self.unit("deploy/share-music.mount")
        self.assertLess(mount.index("Requires=thermite-share-gate@nas.example.service"), mount.index("[Mount]"))
        self.assertIn("After=thermite-share-gate@nas.example.service", mount)
        self.assertIn("After=network-online.target nss-lookup.target", self.unit(deploy.SHARE_GATE))

    def test_status_reports_each_mount(self):
        self.assertEqual(deploy.services(SHARE_HOST)[-2:], (MUSIC_UNIT, "media-photos.mount"))
        self.assertFalse([name for name in deploy.services(HOST) if name.endswith(".mount")])


def mock_system(files=None, stats=None, origins=None):
    found = Mock(spec=System)
    files, stats, origins = dict(files or {}), dict(stats or {}), dict(origins or {})
    found.read_bytes.side_effect = lambda path: files.get(Path(path))
    found.stat.side_effect = lambda path: stats.get(Path(path))
    found.lexists.side_effect = lambda path: Path(path) in files
    found.safe_unit_parent.return_value = True
    found.unit_file.side_effect = lambda path: UnitFile(files[Path(path)], 0, 0o644) if Path(path) in files else None
    found.unit_origins.side_effect = lambda names: {name: origins.get(name, UnitOrigin(load_state="not-found"))
                                                    for name in names}
    found.unit.return_value = Unit("active", "enabled", since=10.0)
    return found


class Install(unittest.TestCase):
    def setUp(self):
        self.ctx = context(base=SHARE_HOST)

    def test_the_units_logins_and_their_items_are_declared(self):
        items = {item.path: item for item in manifest.desired(changeover.switching(self.ctx, None), installing(), ROOT)}
        self.assertEqual(items[UNITS / MUSIC_UNIT], manifest.Item(UNITS / MUSIC_UNIT, "unit", unit=MUSIC_UNIT))
        self.assertEqual(items[UNITS / "media-photos.mount"].cls, "unit")
        self.assertEqual(items[GATE], manifest.Item(GATE, "unit", unit=oh_units.SHARE_GATE))
        self.assertEqual(items[Path("/etc/thermite/share-music.credentials")].cls, "file")
        self.assertNotIn(Path("/etc/thermite/share-photos.credentials"), items)
        # Mount units have no [Install]: no link of theirs is an item.
        self.assertFalse([path for path, item in items.items() if item.cls == "link" and ".mount" in path.name])
        engine.check_items([item.to_json() for item in items.values()], str(ROOT), recorded=False)
        without = {item.path for item in manifest.desired(changeover.switching(context(), None), installing(), ROOT)}
        self.assertFalse({path for path in without if path.suffix in (".mount", ".credentials")
                          or path == GATE})

    def test_units_install_a_mount_unit_under_its_own_name_and_record_it(self):
        found = mock_system(stats={UNITS: Stat(0, 0, 0o755, True, 0), Path("/etc/containers/systemd"):
                                   Stat(0, 0, 0o755, True, 0)})
        step = steps.Units(self.ctx)
        stale = step.stale(found)
        self.assertIn("deploy/share-music.mount", stale)
        self.assertIn(deploy.SHARE_GATE, stale)
        argv = [action.argv for action in step.actions(found) if isinstance(action, Command)]
        self.assertIn(("install", "-m", "0644", "-T", str(self.ctx.configuration / "deploy/share-music.mount"),
                       str(UNITS / MUSIC_UNIT)), argv)
        bulk = next(command for command in argv if command[:4] == ("install", "-m", "0644", "-t")
                    and command[4] == str(UNITS))
        self.assertIn(str(self.ctx.configuration / deploy.SHARE_GATE), bulk)
        self.assertFalse([word for word in bulk if word.endswith(".mount")])
        declared = [item.path for item in step.desired(found)]
        self.assertIn(UNITS / MUSIC_UNIT, declared)
        self.assertIn(GATE, declared)
        self.assertNotIn(UNITS / MUSIC_UNIT, [item.path for item in steps.Units(context()).desired(found)])

    def test_a_mount_point_another_unit_mounts_is_refused(self):
        fstab = UnitOrigin(source="/etc/fstab", fragment=f"/run/systemd/generator/{MUSIC_UNIT}")
        found = mock_system(origins={MUSIC_UNIT: fstab})
        problems = "; ".join(ownership.inspect(self.ctx, found).problems)
        self.assertIn(f"{MUSIC_UNIT} already resolves to another unit", problems)
        # Mounted by hand: systemd shows the mount, with no unit file.
        mounted = UnitOrigin(load_state="loaded", active="active")
        problems = "; ".join(ownership.inspect(self.ctx, mock_system(origins={MUSIC_UNIT: mounted})).problems)
        self.assertIn(f"{MUSIC_UNIT} already resolves to another unit", problems)
        self.assertEqual(ownership.inspect(self.ctx, mock_system()).problems, ())

    def test_a_template_is_proven_by_its_file_and_never_asked_of_systemd(self):
        found = mock_system()
        ownership.inspect(self.ctx, found)
        names = found.unit_origins.call_args.args[0]
        self.assertIn(MUSIC_UNIT, names)
        self.assertNotIn(oh_units.SHARE_GATE, names)
        gate = self.ctx.unit(deploy.SHARE_GATE)
        owned = ownership.inspect(self.ctx, mock_system(files={GATE: gate}))
        self.assertEqual(owned.problems, ())
        self.assertIn(oh_units.SHARE_GATE, owned.units())
        problems = ownership.inspect(self.ctx, mock_system(files={GATE: b"[Service]\n"})).problems
        self.assertIn(f"cannot prove Thermite ownership of {GATE}", "; ".join(problems))

    def test_a_mount_unit_recorded_as_thermites_is_its_own(self):
        text = self.ctx.unit("deploy/share-music.mount")
        mine = UnitOrigin(fragment=str(UNITS / MUSIC_UNIT), active="active")
        owned = ownership.inspect(self.ctx, mock_system(files={UNITS / MUSIC_UNIT: text}, origins={MUSIC_UNIT: mine}))
        self.assertEqual(owned.problems, ())
        self.assertIn(MUSIC_UNIT, owned.units())

    def test_halt_unmounts_the_shares_installed_and_those_installed_before(self):
        record = fakes.manifest({UNITS / "mnt-old.mount": b"[Mount]\n"}, root=self.ctx.installation.root)
        found = mock_system(files={ownership.INSTALLED: record},
                            origins={"mnt-old.mount": UnitOrigin(active="active")})
        running = steps.Halt(self.ctx).running(found)
        self.assertEqual(running, ["mnt-old.mount (active)"])
        names = found.unit_origins.call_args.args[0]
        self.assertTrue({oh_units.TARGET, oh_units.SLICE, MUSIC_UNIT, "media-photos.mount", "mnt-old.mount"} <= set(names))
        # The target's stop unmounts them: each is part of it.
        self.assertIn("PartOf=thermite.target", self.ctx.unit("deploy/share-music.mount").decode())

    def test_the_share_steps_come_where_they_are_needed(self):
        preparation = [type(step) for step in changeover.preparation(self.ctx)]
        self.assertLess(preparation.index(steps.ShareLogins), preparation.index(steps.Packages))
        switching = [type(step) for step in changeover.switching(self.ctx, None)]
        self.assertEqual(switching.index(steps.ShareCredentials), switching.index(steps.Units) + 1)
        self.assertLess(switching.index(steps.ShareCredentials), switching.index(steps.MusicServices))
        self.assertLess(switching.index(steps.ShareCredentials), switching.index(steps.StartTarget))

    def test_packages_add_the_mount_helpers_for_a_host_with_shares(self):
        found = Mock(spec=System)
        found.read_bytes.return_value = b"N\n"
        self.assertEqual(steps.Packages(self.ctx).names(found, "pacman")[-2:], ("cifs-utils", "nfs-utils"))
        self.assertEqual(steps.Packages(self.ctx).names(found, "apt-get")[-2:], ("cifs-utils", "nfs-common"))
        self.assertEqual(steps.Packages(context()).names(found, "pacman"), steps.PACKAGES["pacman"])
        self.assertEqual(steps.Packages().names(found, "apt-get"), steps.PACKAGES["apt-get"])
        found.capture.return_value = (1, "cifs-utils nfs-utils\n")
        self.assertEqual(steps.Packages(self.ctx).check(found).detail, "to install: cifs-utils nfs-utils")


class Logins(unittest.TestCase):
    def setUp(self):
        self.ctx = context(base=SHARE_HOST)
        self.copy = self.ctx.area / "var/house/share-music.credentials"

    def test_the_owners_copy_lives_in_var_beside_the_token(self):
        self.assertEqual(self.ctx.share_login("music"), Path("var/house/share-music.credentials"))
        self.assertEqual(steps.ShareLogins(self.ctx).shares(), [shares.settings(SHARE_HOST)["music"]])

    def test_a_missing_copy_is_asked_for_once_and_kept_private(self):
        found = Mock(spec=System)
        found.read_private.side_effect = FileNotFoundError
        step = steps.ShareLogins(self.ctx)
        check = step.check(found)
        self.assertFalse(check.done)
        self.assertIsNone(check.problem)
        self.assertEqual(check.detail, "to ask for: var/house/share-music.credentials")
        [task] = step.actions(found)
        self.assertIsInstance(task, Task)
        self.assertTrue(task.interactive)
        found.ask.return_value = "alice\n"
        found.secret.return_value = "correct horse"
        self.assertEqual(task.perform(found), 0)
        found.write_private.assert_called_once_with(self.copy, "username=alice\npassword=correct horse\n")
        found.ask.assert_called_once_with("   User name for //nas.example/music: ")

    def test_no_answer_writes_nothing(self):
        found = Mock(spec=System)
        found.read_private.side_effect = FileNotFoundError
        found.ask.return_value, found.secret.return_value = "alice", ""
        self.assertEqual(steps.ShareLogins(self.ctx).ask(found, steps.ShareLogins(self.ctx).shares()[0]), 1)
        found.write_private.assert_not_called()
        self.assertNotIn("alice", " ".join(str(call) for call in found.say.call_args_list))

    def test_a_usable_copy_is_done_and_a_bad_one_stops_before_anything(self):
        found = Mock(spec=System)
        found.read_private.return_value = "username=alice\npassword=correct horse\ndomain=HOUSE\n"
        self.assertTrue(steps.ShareLogins(self.ctx).check(found).done)
        found.read_private.return_value = "username=alice\npass word=x\n"
        problem = steps.ShareLogins(self.ctx).check(found).problem
        self.assertIn("var/house/share-music.credentials: a share login holds", problem)
        self.assertNotIn("pass word", problem)
        found.read_private.side_effect = ValueError("credential must be owned, private, bounded and regular")
        self.assertIn("must be a regular file of yours with mode 0600", steps.ShareLogins(self.ctx).check(found).problem)
        self.assertTrue(steps.ShareLogins(context()).check(found).done)

    def test_logins_are_parsed_as_mount_cifs_reads_them(self):
        login = secrets.share_login(b"# a note\n\nusername=alice\npassword=a=b, c\n")
        self.assertEqual((sorted(login), login["username"], login["password"]),
                         (["password", "username"], "alice", "a=b, c"))
        for bad in (b"username=alice\n", b"password=x\n", b"username=alice\npassword=x\nusername=bob\n",
                    b"username=alice\npassword=x\nsec=none\n", b"username=alice\npassword=x\r\n",
                    b"username=alice\npassword=\n", b"username=alice\npassword=\xff\n"):
            with self.subTest(bad), self.assertRaises(secrets.SecretError):
                secrets.share_login(bad)


class Credentials(unittest.TestCase):
    """The administrative helper, with the test user standing in for root."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.directory = self.root / "etc-thermite"
        self.source = self.root / "share-music.credentials"
        self.source.write_bytes(b"username=alice\npassword=correct horse\n")
        self.source.chmod(0o600)
        self.uid, self.gid = os.geteuid(), os.getegid()
        environment = patch.dict(os.environ, {"SUDO_UID": str(os.getuid()), "SUDO_GID": str(os.getgid())})
        environment.start()
        self.addCleanup(environment.stop)

    def install(self, name="music"):
        return secrets.install_share(name, source=self.source, directory=self.directory, uid=self.uid, gid=self.gid)

    def test_a_login_is_installed_private_salted_and_kept_while_it_stays_the_same(self):
        self.assertEqual(self.install(), {"installed": True, "share": "music", "changed": True})
        target = self.directory / "share-music.credentials"
        info = target.stat()
        self.assertEqual((info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)), (self.uid, self.gid, 0o600))
        self.assertEqual(stat.S_IMODE(self.directory.stat().st_mode), 0o755)
        first = target.read_bytes()
        lines = first.decode().splitlines()
        self.assertRegex(lines[0], r"^# thermite share music [0-9a-f]{32}$")
        self.assertNotIn("=", lines[0], "mount.cifs skips a line without =")
        self.assertEqual(lines[1:], ["username=alice", "password=correct horse"])
        self.assertEqual(secrets.share_login(first), secrets.share_login(self.source.read_bytes()))
        # The same login keeps the file and its salt, newer than the copy.
        os.utime(target, (1, 1))
        target.chmod(0o640)
        self.assertFalse(self.install()["changed"])
        self.assertEqual(target.read_bytes(), first)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertGreaterEqual(target.stat().st_mtime, self.source.stat().st_mtime)
        # A new password gets a new salt too.
        self.source.write_bytes(b"username=alice\npassword=battery staple\n")
        self.assertTrue(self.install()["changed"])
        second = target.read_bytes().decode().splitlines()
        self.assertNotEqual(second[0], lines[0])
        self.assertEqual(second[1:], ["username=alice", "password=battery staple"])
        self.assertEqual(sorted(path.name for path in self.directory.iterdir()), ["share-music.credentials"])

    def test_bad_names_copies_and_directories_are_refused_without_writes(self):
        for name in ("Music", "../music", "music.credentials", ""):
            with self.subTest(name), self.assertRaises(secrets.SecretError):
                self.install(name)
        self.source.chmod(0o640)
        with self.assertRaises(secrets.SecretError):
            self.install()
        self.source.chmod(0o600)
        self.source.write_bytes(b"username=alice\n")
        with self.assertRaises(secrets.SecretError):
            self.install()
        self.assertFalse(self.directory.exists())
        self.source.write_bytes(b"username=alice\npassword=x\n")
        self.directory.mkdir(mode=0o777)
        self.directory.chmod(0o777)
        with self.assertRaises(secrets.SecretError):
            self.install()
        self.assertEqual(list(self.directory.iterdir()), [])
        self.directory.chmod(0o755)
        (self.directory / "share-music.credentials").symlink_to(self.source)
        with self.assertRaises(OSError):
            self.install()

    def test_the_command_installs_as_root_alone_and_says_nothing_secret(self):
        result = subprocess.run([sys.executable, str(AREA / "lib/oh_install/secrets.py"), "--share", "music",
                                 "--source", str(self.source)], capture_output=True, text=True, timeout=5)
        if os.geteuid() != 0:
            self.assertEqual(result.returncode, 2)
        self.assertNotIn("correct horse", result.stdout + result.stderr)
        for argv in (["--share", "music"], ["--share", "music", "--source", str(self.source), "--uid", "9001"],
                     [str(self.root), "--share", "music", "--source", str(self.source)], []):
            with self.subTest(argv):
                done = subprocess.run([sys.executable, str(AREA / "lib/oh_install/secrets.py"), *argv],
                                      capture_output=True, text=True, timeout=5)
                self.assertEqual(done.returncode, 2)

    def test_the_step_installs_a_missing_or_newer_login(self):
        ctx = context(base=SHARE_HOST)
        target = Path("/etc/thermite/share-music.credentials")
        copy = ctx.area / ctx.share_login("music")
        step = steps.ShareCredentials(ctx)
        found = mock_system(stats={copy: Stat(1000, 1000, 0o600, False, 10.0)})
        self.assertEqual(step.check(found).detail, f"to install: {target}")
        self.assertEqual([action.argv for action in step.actions(found)], [
            ("/usr/bin/python3", "-I", "-B", str(ctx.program / "lib/oh_install/secrets.py"), "--share", "music", "--source", str(copy))])
        self.assertTrue(all(action.root for action in step.actions(found)))
        found = mock_system(stats={copy: Stat(1000, 1000, 0o600, False, 10.0), target: Stat(0, 0, 0o600, False, 20.0)})
        self.assertTrue(step.check(found).done)
        found = mock_system(stats={copy: Stat(1000, 1000, 0o600, False, 30.0), target: Stat(0, 0, 0o600, False, 20.0)})
        self.assertFalse(step.check(found).done)
        found = mock_system(stats={copy: Stat(1000, 1000, 0o600, False, 10.0), target: Stat(0, 0, 0o644, False, 20.0)})
        self.assertFalse(step.check(found).done)
        self.assertEqual(step.desired(found), [manifest.Item(target, "file")])
        self.assertTrue(steps.ShareCredentials(context()).check(found).done)


class Transactions(unittest.TestCase):
    """The engine owns the share's units and login like Thermite's other
    root files: a removal sets them aside, a rollback puts them back."""

    MOUNT = f"{fakes.UNITS}/{MUSIC_UNIT}"
    GATE = f"{fakes.UNITS}/{oh_units.SHARE_GATE}"
    LOGIN = "/etc/thermite/share-music.credentials"

    def with_share(self, label):
        version = fakes.version(label)
        version.files[self.MOUNT] = ("unit", fakes.unit_text(MUSIC_UNIT, label), {"unit": MUSIC_UNIT})
        version.files[self.GATE] = ("unit", fakes.unit_text(oh_units.SHARE_GATE, label),
                                    {"unit": oh_units.SHARE_GATE})
        version.files[self.LOGIN] = ("file", b"# thermite share music " + label.encode() + b"\n", {})
        return version

    def run_engine(self, old, new):
        disk = fakes.MemoryDisk()
        host = fakes.FakeHost(disk)
        fakes.install(disk, host, old)
        run = engine.Engine(disk, host, fakes.ROOT, source=fakes.SOURCE, clock=lambda: fakes.CLOCK)
        desired = {"kind": "upgrade", "tools": fakes.TOOLS, "items": fakes.desired_items(new), "pulled": new.images}
        run.begin(fakes.ID, desired, fakes.OPERATOR, release=fakes.release(new))
        run.guard(fakes.ID)
        host.run(("systemctl", "stop", engine.TARGET, engine.SLICE))
        run.collect(fakes.ID)
        fakes.switch(disk, host, new)
        return disk, host, run

    def test_a_removed_share_leaves_nothing_and_its_rollback_puts_it_back(self):
        old, new = self.with_share("old"), fakes.version("new")
        disk, host, run = self.run_engine(old, new)
        for path in (self.MOUNT, self.GATE, self.LOGIN):
            self.assertNotIn(path, disk.nodes, "set aside before the new version starts")
        before = old.files
        run.rollback(fakes.ID)
        for path in (self.MOUNT, self.GATE, self.LOGIN):
            self.assertEqual(disk.nodes[path].data, before[path][1])
        disk, host, run = self.run_engine(old, new)
        run.commit(fakes.ID)
        data = engine.parse_manifest(disk.nodes["/etc/thermite/installed.json"].data, root=fakes.ROOT)
        self.assertFalse({self.MOUNT, self.GATE, self.LOGIN} & {item["path"] for item in data["items"]})
        for path in (self.MOUNT, self.GATE, self.LOGIN):
            self.assertNotIn(path, disk.nodes)

    def test_an_added_share_is_taken_away_by_its_rollback(self):
        disk, host, run = self.run_engine(fakes.version("old"), self.with_share("new"))
        self.assertIn(self.MOUNT, disk.nodes)
        run.rollback(fakes.ID)
        for path in (self.MOUNT, self.GATE, self.LOGIN):
            self.assertNotIn(path, disk.nodes)

    def test_an_uninstall_sets_them_aside(self):
        old = self.with_share("old")
        disk = fakes.MemoryDisk()
        host = fakes.FakeHost(disk)
        fakes.install(disk, host, old)
        run = engine.Engine(disk, host, fakes.ROOT, source=fakes.SOURCE, clock=lambda: fakes.CLOCK)
        run.begin(fakes.ID, {"kind": "uninstall", "tools": fakes.TOOLS, "items": [], "pulled": []}, fakes.OPERATOR)
        run.guard(fakes.ID)
        host.run(("systemctl", "stop", engine.TARGET, engine.SLICE))
        run.collect(fakes.ID)
        run.commit(fakes.ID)
        for path in (self.MOUNT, self.GATE, self.LOGIN):
            self.assertNotIn(path, disk.nodes)

    def test_the_engine_owns_mount_units_below_mnt_and_media_alone(self):
        cases = [(f"/etc/systemd/system/{MUSIC_UNIT}", "unit", True),
                 ("/etc/systemd/system/media-photos.mount", "unit", True),
                 ("/etc/systemd/system/media-photos.mount", "file", False),
                 ("/etc/systemd/system/home.mount", "unit", False),
                 ("/etc/systemd/system/srv-thermite.mount", "unit", False),
                 ("/etc/systemd/system/-.mount", "unit", False),
                 ("/etc/systemd/system/mnt-x.mount.d/override.conf", "unit", False),
                 ("/etc/systemd/system/thermite.target.wants/mnt-x.mount", "link", False),
                 ("/etc/thermite/share-music.credentials", "file", True)]
        for path, cls, expected in cases:
            with self.subTest(path=path, cls=cls):
                self.assertIs(engine.allowed(fakes.ROOT, path, cls), expected)
        engine.check_item({"path": f"/etc/systemd/system/{MUSIC_UNIT}", "class": "unit", "unit": MUSIC_UNIT},
                          fakes.ROOT, recorded=False)
        with self.assertRaises(engine.EngineError):
            engine.check_item({"path": "/etc/systemd/system/mnt-x.mount", "class": "unit", "unit": "sshd.service"},
                              fakes.ROOT, recorded=False)


class Media(unittest.TestCase):
    def test_both_images_ship_the_mount_helpers(self):
        document = oh_iso.packages()
        names = oh_iso.package_names(document, "ext4")
        self.assertTrue(set(shares.PACKAGES["pacman"].values()) <= set(names))
        import yaml
        commands = yaml.safe_load(oh_iso.configuration())["autoinstall"]["late-commands"]
        script = commands[-2][6]
        self.assertIn("apt-get install -y " + " ".join(sorted(shares.PACKAGES["apt-get"].values())) + " ||", script)
        # Apart from python3-venv and git, so that one missing offline keeps neither from installing.
        self.assertIn("apt-get install -y python3-venv git ||", script)


if __name__ == "__main__":
    unittest.main()
