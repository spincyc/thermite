"""thermite logs: the runtime's files and the journal, merged, filtered and redacted.

No real journal: a fake journalctl, first on PATH, prints a fixture's JSON
entries and records how it was called. Every log line here is fictional.
"""

import contextlib
import gzip
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))

import oh_cli  # noqa: E402
import oh_logs  # noqa: E402
import oh_secrets  # noqa: E402
from oh_cli import logs  # noqa: E402

REAL_FOLLOW = oh_logs.follow

ZONE = timezone(timedelta(hours=-6))
NOW = datetime(2026, 11, 21, 15, 0, tzinfo=ZONE)
TOKEN = "oh.fixture." + "A1b2C3d4E5f6G7h8I9j0K1"  # lint: allow (fictional)
PASSWORD = "fixture-" + "pass-sentinel"  # lint: allow (fictional)
# Assembled, so that the package's privacy check finds no key block or MAC here.
KEY_BEGIN, KEY_END = "-----BEGIN RSA " + "PRIVATE KEY-----", "-----END RSA " + "PRIVATE KEY-----"
MAC = ":".join(("00", "11", "22", "33", "44", "55"))

OLDER = """\
2026-11-20 09:00:00.000 [INFO ] [org.openhab.core.Start] - started the day before
2026-11-20 23:59:59.500 [WARN ] [org.example.Old] - late warning yesterday
"""
CURRENT = f"""\
2026-11-21 14:00:00.000 [INFO ] [org.example.Avpro] - AVPro matrix at 192.0.2.10 answered
2026-11-21 14:00:01.000 [ERROR] [org.example.Http] - request to 198.51.100.7 failed
java.lang.IllegalStateException: fixture failure
\tat org.example.Http.call(Http.java:10)
2026-11-21 14:00:03.000 [INFO ] [org.example.Auth] - login password={PASSWORD} with {TOKEN}
2026-11-21 14:30:00.000 [DEBUG] [org.example.Debug] - debug detail
"""
EVENTS = """\
2026-11-21 14:00:02.000 [INFO ] [openhab.event.ItemStateChangedEvent] - Item 'den_tv_power' changed from OFF to ON
"""


def at(text: str) -> str:
    """__REALTIME_TIMESTAMP of a local fixture time."""
    moment = datetime.fromisoformat(text).replace(tzinfo=ZONE)
    return str(int(moment.timestamp() * 1_000_000))


def entry(when, unit, message, priority=6, container=None, **extra):
    found = {"__REALTIME_TIMESTAMP": at(when), "__CURSOR": f"c-{when}", "_SYSTEMD_UNIT": unit,
             "PRIORITY": str(priority), "MESSAGE": message,
             "SYSLOG_IDENTIFIER": container or unit.removesuffix(".service")}
    if container:
        found["CONTAINER_NAME"] = container
    found.update(extra)
    return found


JOURNAL = [
    {**entry("2026-11-21 13:59:00", "init.scope", "Started openHAB 5.2.3 home automation."),
     "UNIT": "thermite-openhab.service", "SYSLOG_IDENTIFIER": "systemd"},
    entry("2026-11-21 14:00:00.500", "thermite-access.service",
          "INFO oh_identity.trail: req id=1 path=/api peer=198.51.100.20", 3, "thermite-access"),
    entry("2026-11-21 14:00:01.001", "thermite-openhab.service",
          "[ERROR] [org.example.Http] - request to 198.51.100.7 failed", 6, "thermite-openhab"),
    entry("2026-11-21 14:00:01.002", "thermite-openhab.service",
          "java.lang.IllegalStateException: fixture failure", 6, "thermite-openhab"),
    entry("2026-11-21 14:00:01.003", "thermite-openhab.service",
          "\tat org.example.Http.call(Http.java:10)", 6, "thermite-openhab"),
    entry("2026-11-21 14:00:02.500", "thermite-atv.service", "WARNING: den_apple_tv: retrying", 3, "thermite-atv"),
    entry("2026-11-21 14:10:00", "thermite-grafana.service",
          'logger=sqlstore level=eror msg="database is locked"', 6, "thermite-grafana"),
    entry("2026-11-21 14:20:00", "thermite-access.service", "Traceback (most recent call last):", 3,
          "thermite-access"),
    entry("2026-11-21 14:20:00.001", "thermite-access.service", '  File "fixture.py", line 1, in <module>', 3,
          "thermite-access"),
    entry("2026-11-21 14:25:00", "thermite-prometheus.service",
          "level=info msg=scrape Authorization: Bearer " + "fixturebearer0123456789", 6, "thermite-prometheus"),
    entry("2026-11-21 14:26:00", "thermite-music.service", list(b"binary \xff line"), 6, "thermite-music"),
    entry("2026-11-21 14:27:00", "thermite-share-gate@nas.example.service", "nas.example answers", 6),
]
#: What the runtime's journal adds while openhab.log cannot be read.
RUNTIME_COPIES = [
    entry("2026-11-21 14:00:00.001", "thermite-openhab.service",
          "[INFO ] [org.example.Avpro] - AVPro matrix at 192.0.2.10 answered", 6, "thermite-openhab"),
]

FAKE = """\
#!{python}
import fnmatch, json, os, sys
argv = sys.argv[1:]
with open(os.environ["FAKE_JOURNAL_ARGV"], "a") as out:
    out.write(json.dumps(argv) + "\\n")
def value(flag):
    return argv[argv.index(flag) + 1] if flag in argv else None
units = [argv[i + 1] for i, word in enumerate(argv) if word == "-u"]
name = os.environ["FAKE_JOURNAL_FOLLOW" if "-f" in argv else "FAKE_JOURNAL"]
entries = [json.loads(line) for line in open(name)] if os.path.exists(name) else []
def ours(e):
    return any(fnmatch.fnmatch(e.get(k) or "", u) for u in units for k in ("_SYSTEMD_UNIT", "UNIT"))
entries = [e for e in entries if ours(e)]
since, until, lines = value("--since"), value("--until"), value("-n")
if since:
    entries = [e for e in entries if int(e["__REALTIME_TIMESTAMP"]) >= int(since[1:]) * 1000000]
if until:
    entries = [e for e in entries if int(e["__REALTIME_TIMESTAMP"]) <= int(until[1:]) * 1000000]
if lines is not None and "-f" not in argv:
    entries = entries[-int(lines):] if int(lines) else []
for e in entries:
    print(json.dumps(e))
sys.stderr.write(os.environ.get("FAKE_JOURNAL_STDERR", ""))
"""


class Fixture(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="thermite-logs-"))
        # Cleanups run last first: the modes come back before the tree goes.
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.addCleanup(subprocess.run, ["chmod", "-R", "u+rwx", str(self.root)], check=False)
        self.state = self.root / "state"
        self.logs = self.state / "logs"
        self.logs.mkdir(parents=True)
        (self.logs / "openhab.log").write_text(CURRENT)
        with gzip.open(self.logs / "openhab.log.1.gz", "wt") as fh:
            fh.write(OLDER)
        old = datetime(2026, 11, 20, 23, 59, 59, tzinfo=ZONE).timestamp()
        os.utime(self.logs / "openhab.log.1.gz", (old, old))
        (self.logs / "events.log").write_text(EVENTS)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        fake = self.bin / "journalctl"
        fake.write_text(FAKE.format(python=sys.executable))
        fake.chmod(0o755)
        self.journal = self.root / "journal.jsonl"
        self.write_journal(JOURNAL)
        self.argv_file = self.root / "argv.jsonl"
        self.env = {"PATH": f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}",
                    "FAKE_JOURNAL": str(self.journal), "FAKE_JOURNAL_ARGV": str(self.argv_file),
                    "FAKE_JOURNAL_FOLLOW": str(self.root / "follow.jsonl")}
        for patch in (mock.patch.dict(os.environ, self.env), mock.patch.object(oh_logs, "ZONE", ZONE),
                      mock.patch.object(oh_logs, "now", lambda: NOW)):
            patch.start()
            self.addCleanup(patch.stop)

    def write_journal(self, entries, name="journal.jsonl"):
        (self.root / name).write_text("".join(json.dumps(e) + "\n" for e in entries))

    def run_logs(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                status = oh_cli.main(["logs", "--state-dir", str(self.state), *args])
            except SystemExit as exc:
                status = exc.code
        return status, out.getvalue(), err.getvalue()

    def records(self, *args):
        status, out, err = self.run_logs("--json", *args)
        self.assertEqual(status, 0, err)
        return [json.loads(line) for line in out.splitlines()]

    def calls(self):
        if not self.argv_file.exists():
            return []
        return [json.loads(line) for line in self.argv_file.read_text().splitlines()]


class Reading(Fixture):
    def test_by_default_the_last_records_of_files_and_journal_merge_by_time(self):
        found = self.records()
        self.assertEqual([(r["component"], r["source"], r["message"].split("\n")[0][:24]) for r in found], [
            ("runtime", "openhab.log", "started the day before"),
            ("runtime", "openhab.log", "late warning yesterday"),
            ("runtime", "journal", "Started openHAB 5.2.3 ho"),
            ("runtime", "openhab.log", "AVPro matrix at 192.0.2."),
            ("gateway", "journal", "req id=1 path=/api peer="),
            ("runtime", "openhab.log", "request to 198.51.100.7 "),
            ("atv", "journal", "WARNING: den_apple_tv: r"),
            ("runtime", "openhab.log", "login password=*** with "),
            ("grafana", "journal", 'logger=sqlstore level=er'),
            ("gateway", "journal", "Traceback (most recent c"),
            ("prometheus", "journal", "level=info msg=scrape Au"),
            ("music", "journal", "binary \ufffd line"),
            ("share-gate", "journal", "nas.example answers"),
            ("runtime", "openhab.log", "debug detail"),
        ])
        [call] = self.calls()
        self.assertEqual(call[:5], ["--no-pager", "-o", "json", "--utc", "-u"])
        self.assertIn("thermite-share-gate@*.service", call)
        self.assertEqual(call[call.index("-n") + 1], "800")

    def test_levels_loggers_and_continuations(self):
        found = {r["message"].split("\n")[0][:12]: r for r in self.records()}
        self.assertEqual(found["request to 1"]["message"], "request to 198.51.100.7 failed\n"
                         "java.lang.IllegalStateException: fixture failure\n\tat org.example.Http.call(Http.java:10)")
        self.assertEqual((found["request to 1"]["level"], found["request to 1"]["logger"]),
                         ("error", "org.example.Http"))
        self.assertEqual((found["req id=1 pat"]["level"], found["req id=1 pat"]["logger"]),
                         ("info", "oh_identity.trail"))
        self.assertEqual(found["WARNING: den"]["level"], "warn")
        self.assertEqual((found["logger=sqlst"]["level"], found["logger=sqlst"]["logger"]), ("error", "sqlstore"))
        self.assertEqual(found["Traceback (m"]["level"], "error")
        self.assertIn('File "fixture.py"', found["Traceback (m"]["message"])
        self.assertEqual((found["Started open"]["logger"], found["Started open"]["level"]), ("systemd", "info"))
        self.assertEqual(found["debug detail"]["level"], "debug")

    def test_the_json_shape_and_utc(self):
        record = self.records("--utc", "--lines", "1")[0]
        self.assertEqual(record, {"component": "runtime", "level": "debug", "logger": "org.example.Debug",
                                  "message": "debug detail", "source": "openhab.log",
                                  "time": "2026-11-21T20:30:00.000Z"})
        self.assertEqual(self.records("--lines", "1")[0]["time"], "2026-11-21T14:30:00.000-06:00")

    def test_text_lines_pad_the_columns_and_indent_continuations(self):
        status, out, _ = self.run_logs("runtime", "atv", "--since", "2026-11-21 14:00:01", "--until",
                                       "2026-11-21 14:00:02.5")
        self.assertEqual(status, 0)
        self.assertEqual(out.splitlines(), [
            "2026-11-21T14:00:01.000-06:00 runtime ERROR [org.example.Http] request to 198.51.100.7 failed",
            "    java.lang.IllegalStateException: fixture failure",
            "    at org.example.Http.call(Http.java:10)".replace("    at", "    \tat"),
            "2026-11-21T14:00:02.500-06:00 atv     WARN  WARNING: den_apple_tv: retrying",
        ])

    def test_lines_keeps_the_last_n_and_reads_no_older_rotation(self):
        with mock.patch.object(oh_logs.gzip, "open", wraps=gzip.open) as opened:
            found = self.records("runtime", "--lines", "2")
        self.assertEqual([r["message"][:12] for r in found], ["login passwo", "debug detail"])
        opened.assert_not_called()

    def test_a_range_stops_at_a_rotation_older_than_since(self):
        with mock.patch.object(oh_logs.gzip, "open", wraps=gzip.open) as opened:
            found = self.records("runtime", "--since", "today")
        opened.assert_not_called()
        self.assertEqual(found[0]["time"], "2026-11-21T13:59:00.000-06:00")
        found = self.records("runtime", "--since", "yesterday", "--until", "today")
        self.assertEqual([r["message"] for r in found], ["started the day before", "late warning yesterday"])

    def test_relative_iso_and_offset_ranges(self):
        def times(*args):
            return [r["time"][11:19] for r in self.records("runtime", "gateway", *args)]
        self.assertEqual(times("--since", "35m"), ["14:30:00"])
        self.assertEqual(times("--since", "60m", "--until", "2026-11-21T14:00:01"),
                         ["14:00:00", "14:00:00", "14:00:01"])
        self.assertEqual(times("--since", "2026-11-21T20:20:00Z"), ["14:20:00", "14:30:00"])
        self.assertEqual(times("--since", "2026-11-21 21:00+01:00", "--until", "2026-11-21T15:00:01-05:00"),
                         ["14:00:00", "14:00:00", "14:00:01"])
        [call] = [c for c in self.calls() if "--until" in c][-1:]
        self.assertEqual(call[call.index("--since") + 1], f"@{int(datetime(2026, 11, 21, 14, tzinfo=ZONE).timestamp())}")
        self.assertNotIn("-n", call)

    def test_parse_when(self):
        self.assertEqual(oh_logs.parse_when("now", NOW), NOW)
        self.assertEqual(oh_logs.parse_when("90s", NOW), NOW - timedelta(seconds=90))
        self.assertEqual(oh_logs.parse_when("12h", NOW), NOW - timedelta(hours=12))
        self.assertEqual(oh_logs.parse_when("7d", NOW), NOW - timedelta(days=7))
        self.assertEqual(oh_logs.parse_when("2w", NOW), NOW - timedelta(weeks=2))
        self.assertEqual(oh_logs.parse_when("today", NOW), datetime(2026, 11, 21, tzinfo=ZONE))
        self.assertEqual(oh_logs.parse_when("yesterday", NOW), datetime(2026, 11, 20, tzinfo=ZONE))
        self.assertEqual(oh_logs.parse_when("2026-11-21", NOW), datetime(2026, 11, 21, tzinfo=ZONE))
        self.assertEqual(oh_logs.parse_when("2026-11-21 14:00", NOW), datetime(2026, 11, 21, 14, tzinfo=ZONE))
        self.assertEqual(oh_logs.parse_when("2026-11-21T14:00:00Z", NOW),
                         datetime(2026, 11, 21, 14, tzinfo=timezone.utc))
        for bad in ("soon", "1y", "2026-13-01", "21/11/2026", ""):
            with self.assertRaises(ValueError, msg=bad):
                oh_logs.parse_when(bad, NOW)

    def test_grep_is_smart_case(self):
        def messages(pattern):
            return [r["message"][:12] for r in self.records("runtime", "--grep", pattern)]
        self.assertEqual(messages("avpro"), ["AVPro matrix"])
        self.assertEqual(messages("Matrix"), [])
        self.assertEqual(messages("AVPro matrix"), ["AVPro matrix"])
        self.assertEqual(messages("org.example.avpro"), ["AVPro matrix"])
        self.assertEqual(messages(r"example\.debug"), ["debug detail"])

    def test_level_keeps_that_level_and_worse(self):
        found = self.records("--level", "warn")
        self.assertEqual({r["level"] for r in found}, {"error", "warn"})
        self.assertEqual(len(found), 5)

    def test_secrets_are_hidden_unless_no_redact(self):
        _, out, _ = self.run_logs()
        self.assertNotIn(PASSWORD, out)
        self.assertNotIn(TOKEN, out)
        self.assertNotIn("fixturebearer", out)
        self.assertIn("password=*** with oh.fixture.***", out)
        self.assertIn("Bearer ***", out)
        self.assertIn("192.0.2.10", out)
        _, out, _ = self.run_logs("--no-redact")
        self.assertIn(PASSWORD, out)
        self.assertIn(TOKEN, out)

    def test_components_events_only_when_named_and_unknown_ones_refused(self):
        self.assertNotIn("events", {r["component"] for r in self.records()})
        self.argv_file.unlink()
        found = self.records("events")
        self.assertEqual([(r["component"], r["source"]) for r in found], [("events", "events.log")])
        self.assertEqual(self.calls(), [], "events alone reads no journal")
        found = self.records("runtime", "events", "--since", "2026-11-21 14:00:01.5", "--until", "2026-11-21 14:00:01.9")
        self.assertEqual(found, [])
        found = self.records("events", "gateway", "--since", "2026-11-21 14:00", "--until", "2026-11-21 14:00:02")
        self.assertEqual([r["component"] for r in found], ["gateway", "events"])
        self.assertEqual(self.calls()[-1].count("-u"), 1)
        status, _, err = self.run_logs("runtime", "kitchen")
        self.assertEqual(status, 2)
        self.assertIn("no component 'kitchen'; the components: runtime, events, gateway", err)

    def test_wrong_switches_exit_2(self):
        for args, said in ((("--since", "soon"), "is not a time"), (("--since", "1h", "--until", "2h"), "after"),
                           (("-f", "--until", "now"), "no --until"), (("--lines", "0"), "--lines"),
                           (("--grep", "("), "--grep"), (("--level", "loud"), "--level"), (("-n",), "-n")):
            status, _, err = self.run_logs(*args)
            self.assertEqual(status, 2, args)
            self.assertIn(said, err, args)

    def test_nothing_in_range_is_a_note(self):
        status, out, err = self.run_logs("--since", "2026-11-22")
        self.assertEqual((status, out), (0, ""))
        self.assertIn("thermite logs: no log records in that range", err)


class Access(Fixture):
    def setUp(self):
        super().setUp()
        if os.geteuid() == 0:
            self.skipTest("root reads every file")

    def test_unreadable_openhab_log_uses_the_journal_copies_and_exits_1(self):
        self.write_journal(RUNTIME_COPIES + JOURNAL)
        found = self.records()
        self.assertEqual([r["source"] for r in found if r["message"].startswith("AVPro")], ["openhab.log"])
        (self.logs / "openhab.log").chmod(0)
        status, out, err = self.run_logs("--json", "runtime")
        self.assertEqual(status, 1)
        self.assertIn(f"{self.logs / 'openhab.log'}: permission denied; join the thermite group "
                      "(sudo usermod -aG thermite $USER, then log in again)", err)
        found = [json.loads(line) for line in out.splitlines()]
        self.assertEqual([(r["source"], r["level"], r["logger"], r["message"].split("\n")[0][:16]) for r in found], [
            ("journal", "info", "systemd", "Started openHAB "),
            ("journal", "info", "org.example.Avpro", "AVPro matrix at "),
            ("journal", "error", "org.example.Http", "request to 198.5"),
        ])
        self.assertEqual(found[2]["message"].count("\n"), 2)

    def test_a_journal_without_permission_says_how_and_still_prints_the_files(self):
        with mock.patch.dict(os.environ, {"FAKE_JOURNAL_STDERR": "Hint: You are currently not seeing messages "
                                          "from other users and the system.\n"}):
            status, out, err = self.run_logs("--json")
        self.assertEqual(status, 1)
        self.assertIn("systemd-journal group (sudo usermod -aG systemd-journal $USER", err)
        self.assertIn("openhab.log", out)

    def test_no_source_at_all_exits_2(self):
        with mock.patch.object(oh_logs, "JOURNALCTL", "thermite-no-such-journalctl"):
            status, _, err = self.run_logs("--state-dir", str(self.root / "absent"))
        self.assertEqual(status, 2)
        self.assertIn("thermite-no-such-journalctl not found", err)
        self.assertIn("no log could be read", err)

    def test_journalctl_missing_reads_the_files(self):
        with mock.patch.object(oh_logs, "JOURNALCTL", "thermite-no-such-journalctl"):
            status, out, err = self.run_logs("--json")
        self.assertEqual(status, 0)
        self.assertIn("not found: showing the log files only", err)
        self.assertEqual({json.loads(line)["source"] for line in out.splitlines()}, {"openhab.log"})


class Targets(Fixture):
    def test_sandbox_reads_its_openhab_log_alone(self):
        sandbox = self.root / "sandbox-logs"
        sandbox.mkdir()
        (sandbox / "openhab.log").write_text("2026-11-21 14:40:00.000 [INFO ] [org.example.Sandbox] - sandbox up\n")
        with mock.patch.object(logs, "sandbox_logs", lambda userdata: sandbox if userdata is None else None):
            status, out, err = self.run_logs("--sandbox", "--json")
            self.assertEqual(status, 0, err)
            self.assertEqual([json.loads(line)["message"] for line in out.splitlines()], ["sandbox up"])
            status, out, err = self.run_logs("--sandbox", "gateway")
        self.assertEqual(status, 2)
        self.assertIn("the sandbox has only the runtime's log: no gateway", err)
        self.assertEqual(self.calls(), [])

    def test_sandbox_userdata_reads_the_log_of_that_sandbox(self):
        userdata = self.root / "elsewhere"
        (userdata / "logs").mkdir(parents=True)
        (userdata / "logs/openhab.log").write_text("2026-11-21 14:40:00.000 [INFO ] [org.example.Sandbox] - "
                                                   "elsewhere up\n")
        status, out, err = self.run_logs("--sandbox", "--userdata", str(userdata), "--json")
        self.assertEqual(status, 0, err)
        self.assertEqual([json.loads(line)["message"] for line in out.splitlines()], ["elsewhere up"])
        self.assertEqual(logs.sandbox_logs(userdata), userdata.resolve() / "logs")
        self.assertEqual(self.calls(), [])

    def test_userdata_goes_only_with_sandbox(self):
        for argv in (["--userdata", str(self.root)], ["--production", "--userdata", str(self.root)]):
            with self.subTest(argv=argv):
                status, out, err = self.run_logs(*argv)
                self.assertEqual((status, out), (2, ""))
                self.assertIn("--userdata names a sandbox's userdata: give it with --sandbox", err)
        self.assertEqual(self.calls(), [])

    def test_url_is_refused_in_process(self):
        status, out, err = self.run_logs("--url", "https://thermite.example")
        self.assertEqual((status, out), (2, ""))
        self.assertIn("--url ssh://", err)

    def test_through_the_launcher(self):
        env = {**os.environ, **self.env, "TZ": "CST6", "NO_COLOR": "1"}
        done = subprocess.run([sys.executable, str(AREA / "bin/thermite"), "logs", "--state-dir", str(self.state),
                               "runtime", "--lines", "2", "--utc"], env=env, capture_output=True, text=True,
                              timeout=30, check=False)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.splitlines(), [
            "2026-11-21T20:00:03.000Z runtime INFO  [org.example.Auth] login password=*** with oh.fixture.***",
            "2026-11-21T20:30:00.000Z runtime DEBUG [org.example.Debug] debug detail",
        ])


class Following(Fixture):
    def test_follow_prints_the_last_lines_then_new_records_through_rotation(self):
        log = self.logs / "openhab.log"
        self.write_journal([entry("2026-11-21 15:00:05", "thermite-access.service",
                                  "INFO oh_identity.trail: later id=2", 3, "thermite-access")], "follow.jsonl")
        def answered():
            # The journal's record is read before the file's last one is
            # complete, which a slow start of the journal once hid.
            while not any("-f" in call for call in self.calls()):
                time.sleep(0.01)
            time.sleep(0.3)

        steps = iter([
            lambda: (answered(), log.open("a").write("2026-11-21 15:00:01.000 [INFO ] [org.example.A] - first new\n")),
            lambda: (log.open("a").write("2026-11-21 15:00:02.000 [WARN ] [org.example.B] - before rotation\n"
                                         "\tat a continuation\n"),
                     log.rename(self.logs / "openhab.log.1"),
                     log.write_text("2026-11-21 15:00:03.000 [INFO ] [org.example.C] - after rotation\n")),
        ])
        count = [0]
        seen = []

        def sleep(_):
            count[0] += 1
            step = next(steps, None)
            if step:
                step()
            else:
                time.sleep(0.02)

        def stop():
            return count[0] > 100 or (count[0] >= 4 and any(r.component == "gateway" for r in seen))

        def follow(query, logs_dir, journal, start, emit):
            def shown(record):
                seen.append(record)
                emit(record)
            return REAL_FOLLOW(query, logs_dir, journal, start, shown, stop=stop, sleep=sleep)

        with mock.patch.object(oh_logs, "follow", follow):
            status, out, err = self.run_logs("runtime", "gateway", "-f", "--lines", "1")
        self.assertEqual(status, 0, err)
        lines = out.splitlines()
        self.assertEqual(lines[0], "2026-11-21T14:30:00.000-06:00 runtime DEBUG [org.example.Debug] debug detail")
        self.assertEqual([line[30:] if line[:1] == "2" else line for line in lines[1:]], [
            "runtime INFO  [org.example.A] first new",
            "runtime WARN  [org.example.B] before rotation",
            "    \tat a continuation",
            "runtime INFO  [org.example.C] after rotation",
            "gateway INFO  [oh_identity.trail] later id=2",
        ])
        follow = self.calls()[-1]
        self.assertIn("-f", follow)
        self.assertEqual(follow[follow.index("--after-cursor") + 1], "c-2026-11-21 14:20:00.001")


class JournalOrder(unittest.TestCase):
    def test_a_quiet_units_last_record_settles_once_no_entry_can_continue_it(self):
        """While the journal is busy, follow prints what came after a quiet
        unit's last record only once that record is out."""
        parser = oh_logs.JournalParser(runtime_file=False)
        quiet = entry("2026-11-21 15:00:00", "thermite-access.service", "INFO oh_identity.trail: quiet", 6,
                      "thermite-access")
        self.assertEqual(parser.feed(quiet), [])

        def busy(second):
            return parser.feed(entry(f"2026-11-21 15:00:0{second}", "thermite-openhab.service",
                                     f"[INFO ] [org.example.Busy] - line {second}", 6, "thermite-openhab"))
        busy(1), busy(2)
        self.assertEqual(parser.settled(), [])
        busy(3)
        self.assertEqual([record.message for record in parser.settled()], ["quiet"])
        self.assertEqual(sorted(parser.pending), ["runtime"])


class Redact(unittest.TestCase):
    def test_redact(self):
        cases = {
            "password=hunter22 user=bob": "password=*** user=bob",
            '{"token":"abc123xyz","host":"192.0.2.4"}': '{"token":"***","host":"192.0.2.4"}',  # lint: allow (fictional)
            "api_key: 'zzzz'": "api_key: '***'",  # lint: allow (fictional)
            "access_token=qqqqq&b=1": "access_token=***&b=1",
            "Authorization: Basic dXNlcjpwYXNz": "Authorization: Basic ***",
            "curl -H Bearer abcdefghijk": "curl -H Bearer ***",
            "Cookie: sid=abc; x=y": "Cookie: ***",
            "session=deadbeef tokens=3": "session=*** tokens=3",
            "fetch https://admin:" + "s3cret@203.0.113.7/x": "fetch https://admin:***@203.0.113.7/x",
            "gh" + "p_" + "a" * 36: "ghp_***",
            f"{KEY_BEGIN}\nMIIabc\n{KEY_END}\nafter": f"{KEY_BEGIN}\n***\n{KEY_END}\nafter",
            f"password=null mac {MAC} host tv.example basic setup": f"password=null mac {MAC} host tv.example basic setup",
        }
        for text, expected in cases.items():
            self.assertEqual(oh_secrets.redact(text), expected, text)

    def test_a_value_ends_at_its_own_quote_and_objects_and_arrays_go_whole(self):
        m = oh_secrets.MASK
        cases = {
            '{"password": "a\\"b c", "user": "bob"}': f'{{"password": "{m}", "user": "bob"}}',  # lint: allow (fictional)
            "password='abcd and the rest of the line": f"password='{m}",  # lint: allow (fictional)
            '{"secret": {"value": "abcd"}, "x": 1}': f'{{"secret": {m}, "x": 1}}',  # lint: allow (fictional)
            '{"psk": 12345678, "token": ["abcd", "efgh"]}': f'{{"psk": {m}, "token": {m}}}',  # lint: allow (fictional)
            '{"session": {"a": {"b": "abcd"}}}': f'{{"session": {m}',
            'api_key: [] token: {}': 'api_key: [] token: {}',
        }
        for text, expected in cases.items():
            self.assertEqual(oh_secrets.redact(text), expected, text)


class JournalKeys(unittest.TestCase):
    def records(self, lines):
        parser = oh_logs.JournalParser(runtime_file=True)
        found = []
        for index, line in enumerate(lines):
            found += parser.feed(entry(f"2026-11-21 14:00:00.{index:03d}", "thermite-access.service", line, 6,
                                       "thermite-access"))
        return [oh_logs.Query().prepare(record).message for record in found + parser.flush()]

    def test_a_key_printed_a_line_per_entry_is_one_record_and_hidden(self):
        body = ["MIIEowIBAAKCAQEAfixtureFIXTURE", "Proc-Type: 4,ENCRYPTED", "fixtureBODY+/line2=="]
        self.assertEqual(self.records(["loading the gateway key", KEY_BEGIN, *body, KEY_END, "listening"]),
                         ["loading the gateway key", f"{KEY_BEGIN}\n{oh_secrets.MASK}\n{KEY_END}", "listening"])

    def test_a_key_left_open_ends_at_the_first_line_that_is_none_of_it(self):
        self.assertEqual(self.records([KEY_BEGIN, "MIIEowIBAAKCAQEAfixture", "listening on 198.51.100.20"]),
                         [f"{KEY_BEGIN}\n{oh_secrets.MASK}", "listening on 198.51.100.20"])


if __name__ == "__main__":
    unittest.main()
