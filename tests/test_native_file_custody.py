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

    def test_a_reused_inode_number_is_not_the_file_that_was_read(self):
        # ext4 can give a planted file the inode number just freed; btrfs and
        # tmpfs never do, so the reuse is simulated here.
        with oh_ports.native_etc(self.userdata) as etc:
            _, info = oh_ports.read_regular(etc, "custom.properties")
        def reused(index=None, value=None, ctime_ns=info.st_ctime_ns):
            fields = list(info)
            if index is not None:
                fields[index] = value
            return os.stat_result(fields, {"st_ctime_ns": ctime_ns})
        self.assertTrue(oh_ports.unchanged(info, reused()))
        self.assertFalse(oh_ports.unchanged(info, reused(stat.ST_MODE, stat.S_IFLNK | 0o777)))
        self.assertFalse(oh_ports.unchanged(info, reused(stat.ST_NLINK, 2)))
        self.assertFalse(oh_ports.unchanged(info, reused(ctime_ns=info.st_ctime_ns + 1)))

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


#: The shape of the pinned openHAB template: a file, a console and an events
#: route, and the non-additive event logger.
TEMPLATE = b"""<?xml version="1.0" encoding="UTF-8"?><Configuration monitorInterval="10">
\t<Appenders>
\t\t<Console name="STDOUT"><PatternLayout pattern="%m%n"/></Console>
\t\t<RollingFile fileName="openhab.log" filePattern="openhab.log.%i.gz" name="LOGFILE"><PatternLayout pattern="%m%n"/></RollingFile>
\t\t<RollingRandomAccessFile fileName="events.log" filePattern="events.log.%i.gz" name="EVENT"><PatternLayout pattern="%m%n"/></RollingRandomAccessFile>
\t\t<PaxOsgi filter="*" name="OSGI"/>
\t</Appenders>
\t<Loggers>
\t\t<Root level="WARN">
\t\t\t<AppenderRef ref="LOGFILE"/>
\t\t\t<AppenderRef ref="OSGI"/>
\t\t</Root>
\t\t<Logger level="OFF" name="org.apache.karaf.shell.support"><AppenderRef ref="STDOUT"/></Logger>
\t\t<Logger level="INFO" name="org.openhab"/>
\t\t<Logger additivity="false" level="INFO" name="openhab.event"><AppenderRef ref="EVENT"/><AppenderRef ref="OSGI"/></Logger>
\t</Loggers>
</Configuration>
"""


class JournalRoute(unittest.TestCase):
    """openhab.log also reaches the journal, through a guarded route only."""

    def journal(self, data):
        root = oh_logging.document(data)
        appenders = [appender for container in root if container.tag == "Appenders" for appender in container
                     if appender.get("name") == oh_logging.JOURNAL]
        loggers = {element.get("name", element.tag): [ref.get("ref") for ref in element if ref.tag == "AppenderRef"]
                   for container in root if container.tag == "Loggers" for element in container}
        return appenders, loggers

    def test_template_gains_one_guarded_console_route_on_root_only(self):
        self.assertFalse(oh_logging.journaled(TEMPLATE))
        data = oh_logging.guarded(TEMPLATE)
        self.assertTrue(oh_logging.configured(data))
        self.assertEqual(oh_logging.guarded(data), data, "idempotent")
        appenders, loggers = self.journal(data)
        self.assertEqual(len(appenders), 1)
        self.assertEqual((appenders[0].tag, appenders[0].attrib), ("Console", {"name": oh_logging.JOURNAL, "target": "SYSTEM_OUT"}))
        self.assertTrue(oh_logging.is_guard(list(appenders[0])[0]), "the route drops pairing payloads itself")
        self.assertEqual(loggers["Root"], ["LOGFILE", "OSGI", oh_logging.JOURNAL])
        self.assertEqual(loggers["openhab.event"], ["EVENT", "OSGI"], "events.log stays file-only")
        self.assertIn(b"\n\t\t\t<AppenderRef ref=\"THERMITE_JOURNAL\" />\n\t\t</Root>", data)

    def test_a_configuration_without_appenders_gets_them(self):
        data = oh_logging.guarded(LOG4J)
        self.assertTrue(oh_logging.configured(data))
        self.assertEqual([child.tag for child in oh_logging.document(data)], ["Filters", "Appenders", "Loggers"])

    def test_sandbox_keeps_pairing_protection_without_the_route(self):
        data = oh_logging.guarded(TEMPLATE, journal=False)
        self.assertTrue(oh_logging.protected(data))
        self.assertFalse(oh_logging.journaled(data))
        self.assertTrue(oh_logging.configured(oh_logging.guarded(data)))

    def test_an_altered_route_is_not_journaled_and_is_replaced_whole(self):
        data = oh_logging.guarded(TEMPLATE).decode()
        appender = data[data.index('<Console name="THERMITE_JOURNAL"'):data.index("</Console>", data.index("THERMITE_JOURNAL")) + len("</Console>")]
        guard = appender[appender.index("<RegexFilter"):appender.index("/>") + 2]
        reference = '<AppenderRef ref="THERMITE_JOURNAL" />'
        for case, altered in (
                ("unguarded", data.replace(appender, appender.replace(guard, ""))),
                ("accepting first", data.replace(appender, appender.replace(
                    guard, '<ThresholdFilter level="INFO" onMatch="ACCEPT"/>' + guard))),
                ("stderr", data.replace('target="SYSTEM_OUT"', 'target="SYSTEM_ERR"')),
                ("duplicate", data.replace(appender, appender + appender)),
                ("silenced", data.replace(reference, '<AppenderRef ref="THERMITE_JOURNAL" level="OFF" />')),
                ("unreferenced", data.replace(reference, "")),
                ("referenced twice", data.replace(reference, reference + reference))):
            with self.subTest(case=case):
                self.assertTrue(oh_logging.protected(altered.encode()))
                self.assertFalse(oh_logging.journaled(altered.encode()))
                repaired = oh_logging.guarded(altered.encode())
                self.assertTrue(oh_logging.configured(repaired))
                appenders, loggers = self.journal(repaired)
                self.assertEqual(len(appenders), 1)
                self.assertEqual(loggers["Root"].count(oh_logging.JOURNAL), 1)

    def test_the_route_never_stands_in_for_a_logger_guard(self):
        data = oh_logging.guarded(TEMPLATE).decode()
        logger = data.index('name="org.openhab"')
        unguarded = data[:logger] + data[logger:].replace("<Filters>", "<Filters><ThresholdFilter level=\"INFO\"/>", 1)
        self.assertTrue(oh_logging.journaled(unguarded.encode()))
        self.assertFalse(oh_logging.protected(unguarded.encode()))
        self.assertFalse(oh_logging.configured(unguarded.encode()))


class FilePermissions(unittest.TestCase):
    """Every file appender writes group-readable logs, rotations included."""

    def permissions(self, data):
        root = oh_logging.document(data)
        return {element.get("name"): [value for key, value in element.attrib.items() if key.lower() == "filepermissions"]
                for container in root if container.tag == "Appenders" for element in container.iter()
                if element.get("name")}

    def test_both_template_files_become_0640_and_nothing_else_changes(self):
        self.assertFalse(oh_logging.readable(TEMPLATE))
        for journal in (True, False):
            with self.subTest(journal=journal):
                data = oh_logging.guarded(TEMPLATE, journal=journal)
                self.assertTrue(oh_logging.readable(data))
                self.assertEqual(oh_logging.guarded(data, journal=journal), data, "idempotent")
                found = self.permissions(data)
                self.assertEqual((found["LOGFILE"], found["EVENT"]), (["rw-r-----"], ["rw-r-----"]))
                self.assertEqual((found["STDOUT"], found["OSGI"]), ([], []))
        self.assertTrue(oh_logging.configured(oh_logging.guarded(TEMPLATE)))

    def test_a_wider_duplicate_or_missing_mode_is_replaced_and_not_configured(self):
        data = oh_logging.guarded(TEMPLATE).decode()
        mode = 'filePermissions="rw-r-----"'
        for case, altered in (("world-readable", data.replace(mode, 'filePermissions="rw-r--r--"', 1)),
                              ("missing", data.replace(" " + mode, "", 1)),
                              ("second spelling", data.replace(mode, mode + ' filepermissions="rw-rw-rw-"', 1))):
            with self.subTest(case=case):
                self.assertTrue(oh_logging.protected(altered.encode()))
                self.assertTrue(oh_logging.journaled(altered.encode()))
                self.assertFalse(oh_logging.readable(altered.encode()))
                self.assertFalse(oh_logging.configured(altered.encode()))
                repaired = oh_logging.guarded(altered.encode())
                self.assertTrue(oh_logging.configured(repaired))
                self.assertEqual(self.permissions(repaired)["LOGFILE"], ["rw-r-----"])

    def test_an_appender_that_rejects_the_mode_is_never_reported_readable(self):
        # log4j-core 2.25.4 refuses filePermissions on these and writes 0600.
        for kind in ("RandomAccessFile", "MemoryMappedFile", "randomaccessfile"):
            with self.subTest(kind=kind):
                extra = f'<{kind} fileName="other.log" name="OTHER"><PatternLayout pattern="%m%n"/></{kind}><PaxOsgi'
                data = oh_logging.guarded(TEMPLATE.replace(b"<PaxOsgi", extra.encode()))
                self.assertTrue(oh_logging.protected(data))
                self.assertFalse(oh_logging.readable(data))
                self.assertFalse(oh_logging.configured(data))
                found = self.permissions(data)
                self.assertEqual((found["LOGFILE"], found["EVENT"], found["OTHER"]), (["rw-r-----"], ["rw-r-----"], []))

    def test_a_nested_file_appender_is_covered(self):
        routing = (b'<Routing name="ROUTED"><Routes pattern="$${ctx:x}"><Route>'
                   b'<File fileName="routed.log" name="ROUTED_FILE"><PatternLayout pattern="%m%n"/></File>'
                   b"</Route></Routes></Routing><PaxOsgi")
        unreadable = oh_logging.guarded(TEMPLATE).replace(b"<PaxOsgi", routing)
        self.assertFalse(oh_logging.readable(unreadable))
        data = oh_logging.guarded(unreadable)
        self.assertTrue(oh_logging.configured(data))
        self.assertEqual((self.permissions(data)["ROUTED_FILE"], self.permissions(data)["ROUTED"]), (["rw-r-----"], []))


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
