"""Root native helpers never write, read or re-own through planted links.

The service account can write native userdata. The listener and logging
helpers run as root inside it, so a symbolic link or hard link planted there
must be refused rather than followed to a file such as /etc/passwd. These
tests use an unprivileged scratch tree: refusal needs no real root.
"""
import io
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest import mock

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))

import oh_logging  # noqa: E402
import oh_ports  # noqa: E402

LOG4J = b"<Configuration><Loggers><Root/></Loggers></Configuration>"
VICTIM = b"root:x:0:0:root:/root:/bin/sh\n"


class Scratch(unittest.TestCase):
    def setUp(self):
        scratch = Path(os.environ.get("TMPDIR") or AREA / ".scratch/native-file-custody")
        scratch.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(temporary.cleanup)
        # The anchored walk refuses any symlinked ancestor, including one in TMPDIR.
        self.root = Path(os.path.realpath(temporary.name))
        self.userdata = self.root / "userdata"
        (self.userdata / "etc").mkdir(parents=True)
        self.outside = self.root / "outside"
        self.outside.mkdir()
        self.victim = self.outside / "passwd"
        self.victim.write_bytes(VICTIM)
        self.victim.chmod(0o644)
        self.etc = self.userdata / "etc"
        (self.etc / "org.apache.karaf.shell.cfg").write_text("sshPort = 8101\n")
        (self.etc / "custom.properties").write_text("karaf.pid.file=${karaf.data}/tmp/karaf.pid\n")
        (self.etc / "log4j2.xml").write_bytes(LOG4J)
        (self.etc / "org.ops4j.pax.logging.cfg").write_bytes(oh_logging.DEFAULT_SELECTOR)

    def assertVictimUntouched(self):
        info = self.victim.stat()
        self.assertEqual(self.victim.read_bytes(), VICTIM)
        self.assertEqual(stat.S_IMODE(info.st_mode), 0o644)

    def plant(self, name, kind):
        path = self.etc / name
        path.unlink()
        if kind == "symlink":
            path.symlink_to(self.victim)
        elif kind == "hardlink":
            os.link(self.victim, path)
        elif kind == "fifo":
            os.mkfifo(path)
        return path


class Listeners(Scratch):
    def test_configuration_refuses_planted_links_and_special_files(self):
        for name in (oh_ports.SHELL_CFG.name, oh_ports.CUSTOM_PROPERTIES.name):
            for kind in ("symlink", "hardlink", "fifo"):
                for anchored in (False, True):
                    with self.subTest(name=name, kind=kind, anchored=anchored):
                        self.setUp()
                        self.plant(name, kind)
                        other = next(path for path in (self.etc / oh_ports.SHELL_CFG.name,
                                                       self.etc / oh_ports.CUSTOM_PROPERTIES.name)
                                     if path.name != name)
                        before = other.read_bytes()
                        with self.assertRaises((OSError, ValueError)):
                            oh_ports.configure_native(self.userdata, anchored=anchored,
                                                      uid=os.getuid(), gid=os.getgid())
                        self.assertVictimUntouched()
                        # Both files are validated before either is replaced.
                        self.assertEqual(other.read_bytes(), before)

    def test_shutdown_writer_refuses_planted_links(self):
        for kind in ("symlink", "hardlink"):
            with self.subTest(kind=kind):
                self.setUp()
                self.plant(oh_ports.CUSTOM_PROPERTIES.name, kind)
                with self.assertRaises((OSError, ValueError)):
                    oh_ports.configure_shutdown(self.userdata, oh_ports.SHUTDOWN_PORT)
                self.assertVictimUntouched()

    def test_symlinked_etc_or_anchored_userdata_ancestor_is_refused(self):
        for case in ("etc", "userdata"):
            with self.subTest(case=case):
                self.setUp()
                target = self.outside / "etc"
                target.mkdir()
                for name in (oh_ports.SHELL_CFG.name, oh_ports.CUSTOM_PROPERTIES.name):
                    (target / name).write_bytes(b"sshPort = 8101\n")
                if case == "etc":
                    for child in self.etc.iterdir():
                        child.unlink()
                    self.etc.rmdir()
                    self.etc.symlink_to(target, target_is_directory=True)
                    selected = self.userdata
                else:
                    (self.root / "linked").symlink_to(self.userdata, target_is_directory=True)
                    selected = self.root / "linked"
                    target = self.etc
                before = {path.name: path.read_bytes() for path in target.iterdir()}
                with self.assertRaises(OSError):
                    oh_ports.configure_native(selected, anchored=True)
                self.assertEqual({path.name: path.read_bytes() for path in target.iterdir()}, before)

    def test_replacement_never_writes_through_a_link_swapped_in_after_reading(self):
        with oh_ports.native_etc(self.userdata) as etc:
            data, info = oh_ports.read_regular(etc, "custom.properties")
            self.plant("custom.properties", "symlink")
            with self.assertRaisesRegex(ValueError, "changed while"):
                oh_ports.replace_regular(etc, "custom.properties", b"replaced\n", info)
        self.assertVictimUntouched()
        self.assertEqual(sorted(path.name for path in self.etc.iterdir() if path.name.startswith(".")), [])

    def test_replacement_owns_new_inodes_by_descriptor_without_set_id_bits(self):
        console = self.etc / oh_ports.SHELL_CFG.name
        console.chmod(0o4640)
        oh_ports.configure_native(self.userdata, anchored=True, uid=os.getuid(), gid=os.getgid())
        info = console.stat()
        self.assertEqual(stat.S_IMODE(info.st_mode), 0o640)
        self.assertEqual((info.st_uid, info.st_gid, info.st_nlink), (os.getuid(), os.getgid(), 1))
        self.assertTrue(oh_ports.native_configured_bytes(console.read_bytes()))

    def test_root_helper_reports_refusal_without_touching_the_target(self):
        self.plant(oh_ports.CUSTOM_PROPERTIES.name, "symlink")
        with mock.patch.object(oh_ports.socket, "create_connection", side_effect=ConnectionRefusedError), \
                mock.patch("sys.stderr", new_callable=io.StringIO) as errors, \
                self.assertRaises(SystemExit) as stopped:
            oh_ports.main([str(self.userdata), "--uid", str(os.getuid()), "--gid", str(os.getgid())])
        self.assertEqual(stopped.exception.code, 2)
        self.assertIn("could not configure", errors.getvalue())
        self.assertVictimUntouched()
        self.assertTrue((self.etc / oh_ports.CUSTOM_PROPERTIES.name).is_symlink())


class Logging(Scratch):
    def test_install_refuses_planted_links_for_configuration_and_selector(self):
        for name in (oh_logging.RELATIVE.name, oh_logging.SELECTOR.name):
            for kind in ("symlink", "hardlink", "fifo"):
                with self.subTest(name=name, kind=kind):
                    self.setUp()
                    self.plant(name, kind)
                    with self.assertRaises((OSError, ValueError)):
                        oh_logging.install(self.userdata, anchored=True)
                    self.assertVictimUntouched()
                    self.assertFalse(oh_logging.ready(self.userdata))

    def test_install_protects_regular_files_and_keeps_owner_and_mode(self):
        path = self.etc / oh_logging.RELATIVE.name
        path.chmod(0o640)
        self.assertTrue(oh_logging.install(self.userdata, anchored=True))
        self.assertTrue(oh_logging.ready(self.userdata))
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)
        self.assertFalse(oh_logging.install(self.userdata, anchored=True))

    def test_root_helper_refuses_a_symlinked_logging_configuration(self):
        self.plant(oh_logging.RELATIVE.name, "symlink")
        with mock.patch.object(oh_logging.socket, "create_connection", side_effect=ConnectionRefusedError), \
                mock.patch("sys.stderr", new_callable=io.StringIO), self.assertRaises(SystemExit) as stopped:
            oh_logging.main([str(self.userdata)])
        self.assertEqual(stopped.exception.code, 2)
        self.assertVictimUntouched()


class Seeding(Scratch):
    def template(self):
        template = self.root / "template"
        (template / "etc").mkdir(parents=True)
        (template / "etc/log4j2.xml").write_bytes(LOG4J)
        (template / "etc/org.ops4j.pax.logging.cfg").write_bytes(oh_logging.DEFAULT_SELECTOR)
        (template / "etc/org.apache.karaf.shell.cfg").write_text("sshPort = 8101\n")
        (template / "etc/custom.properties").write_text("")
        (template / "etc/run.sh").write_text("#!/bin/sh\n")
        (template / "etc/run.sh").chmod(0o4755)
        (template / "etc/link").symlink_to("custom.properties")
        return template

    def test_seed_creates_regular_inodes_and_preserves_template_links(self):
        for child in self.etc.iterdir():
            child.unlink()
        self.etc.rmdir()
        template = self.template()
        self.assertTrue(oh_logging.initialize(self.userdata, template, os.getuid(), os.getgid(), anchored=True))
        self.assertTrue(oh_logging.ready(self.userdata))
        self.assertTrue((self.etc / "link").is_symlink())
        self.assertEqual(os.readlink(self.etc / "link"), "custom.properties")
        self.assertEqual(stat.S_IMODE((self.etc / "run.sh").stat().st_mode), 0o755)
        self.assertFalse(oh_logging.initialize(self.userdata, template, os.getuid(), os.getgid(), anchored=True))

    def test_seed_refuses_existing_state_and_symlinked_userdata(self):
        template = self.template()
        for child in self.etc.iterdir():
            child.unlink()
        self.etc.rmdir()
        self.etc.symlink_to(self.outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "not empty"):
            oh_logging.initialize(self.userdata, template, os.getuid(), os.getgid(), anchored=True)
        self.assertEqual(sorted(path.name for path in self.outside.iterdir()), ["passwd"])
        self.etc.unlink()
        (self.root / "linked").symlink_to(self.userdata, target_is_directory=True)
        with self.assertRaises(OSError):
            oh_logging.initialize(self.root / "linked", template, os.getuid(), os.getgid(), anchored=True)
        self.assertEqual(list(self.userdata.iterdir()), [])
        self.assertVictimUntouched()

    def test_seed_creates_missing_userdata_with_its_former_mode(self):
        missing = self.root / "fresh"
        self.assertTrue(oh_logging.initialize(missing, self.template(), os.getuid(), os.getgid(), anchored=True))
        self.assertEqual(stat.S_IMODE(missing.stat().st_mode), 0o755)
        self.assertTrue(oh_logging.ready(missing))


if __name__ == "__main__":
    unittest.main()
