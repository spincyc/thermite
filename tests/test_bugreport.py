"""thermite bugreport: one archive of version, status, history, logs and the system's facts.

No command runs: bugreport.run is replaced by a fake that answers each
argv, and oh_cli.over_ssh by one that writes an archive. Every host, address
and secret here is fictional.
"""

import contextlib
import io
import json
import os
import socket
import stat
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))

import oh_cli  # noqa: E402
from oh_cli import bugreport  # noqa: E402

PASSWORD = "fixture-" + "pass-sentinel"  # lint: allow (fictional)
TOKEN = "oh.fixture." + "A1b2C3d4E5f6G7h8I9j0K1"  # lint: allow (fictional)


class Binary(io.StringIO):
    """A standard output that is not a terminal, with the buffer bytes go to."""

    def __init__(self):
        super().__init__()
        self.buffer = io.BytesIO()

    def isatty(self):
        return False


class Fake:
    """bugreport.run: what each command prints, and the argvs it was given."""

    def __init__(self, failing=()):
        self.ran = []
        self.failing = failing

    def __call__(self, argv, timeout=bugreport.TIMEOUT):
        self.ran.append(list(argv))
        words = argv[3:] if argv[:2] == bugreport.thermite()[:2] else argv
        if words and words[0] in self.failing:
            return None, "", f"{words[0]}: fixture failure\nlast line", 0.25
        if words[:1] == ["logs"]:
            return 0, f"2026-11-21T14:00:03Z runtime INFO login password={PASSWORD} with {TOKEN}\n", "", 1.0
        if words[:1] == ["status"]:
            return 1, json.dumps({"status": "warnings", "matrix": "192.0.2.10"}) + "\n", "", 0.5
        return 0, f"{' '.join(words)} said this\n", "", 0.0


def members(data: bytes) -> dict[str, tuple[int, str]]:
    """{name: (mode, text)} of an archive."""
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        return {member.name: (member.mode, tar.extractfile(member).read().decode()) for member in tar.getmembers()}


class Case(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="thermite-bugreport-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        release = self.directory / "os-release"
        release.write_text('NAME="Fixture Linux"\n', encoding="utf-8")
        self.fake = Fake()
        for patch in (mock.patch.object(bugreport, "run", self.fake), mock.patch.object(bugreport, "OS_RELEASE", release),
                      mock.patch.object(socket, "gethostname", return_value="thermite.example"),
                      mock.patch.dict(os.environ)):
            patch.start()
            self.addCleanup(patch.stop)
        os.environ.pop(oh_cli.URL_VARIABLE, None)
        os.chdir(self.directory)
        self.addCleanup(os.chdir, AREA)

    def run_cli(self, *argv, stdout=None):
        """(exit status, stdout, stderr) of thermite bugreport ARGV."""
        out, err = stdout or io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                status = oh_cli.main(["bugreport", *argv])
            except SystemExit as exc:
                status = exc.code
        return status, out.getvalue(), err.getvalue()

    def archives(self):
        return sorted(path for path in self.directory.iterdir() if path.name.endswith(".tar.gz"))


class Collect(Case):
    def test_each_part_is_a_thermite_command_s_output_redacted_and_the_manifest_names_it(self):
        files, manifest = bugreport.collect("24h", True)
        self.assertEqual(set(files), {"version.json", "status.json", "history.json", "logs.txt", "events.txt",
                                      "system.txt"})
        self.assertNotIn(PASSWORD, files["logs.txt"])
        self.assertNotIn(TOKEN, files["events.txt"])
        self.assertIn("password=***", files["logs.txt"])
        self.assertIn("192.0.2.10", files["status.json"])
        commands = {entry["file"]: entry for entry in manifest["commands"]}
        self.assertEqual(commands["logs.txt"]["command"], "thermite logs --since 24h --utc")
        self.assertEqual(commands["events.txt"]["command"], "thermite logs events --since 24h --utc")
        self.assertEqual(commands["status.json"]["exit"], 1)
        self.assertEqual((manifest["host"], manifest["since"], manifest["redacted"]), ("thermite.example", "24h", True))
        thermite = [argv for argv in self.fake.ran if argv[:2] == bugreport.thermite()[:2]]
        self.assertTrue(all(argv[2] == "--no-color" for argv in thermite))

    def test_the_system_s_facts_are_read_without_root(self):
        files, _ = bugreport.collect("24h", True)
        self.assertIn("## kernel: uname -a\nuname -a said this\n", files["system.txt"])
        self.assertIn('NAME="Fixture Linux"', files["system.txt"])
        self.assertFalse(any(argv[0] in ("sudo", "doas") for argv in self.fake.ran))

    def test_no_redact_keeps_secrets_and_asks_logs_for_them(self):
        files, manifest = bugreport.collect("2h", False)
        self.assertIn(PASSWORD, files["logs.txt"])
        self.assertIn("--no-redact", self.fake.ran[3])
        self.assertFalse(manifest["redacted"])

    def test_a_command_that_fails_leaves_its_part_empty_and_the_rest_is_collected(self):
        self.fake.failing = ("history",)
        files, manifest = bugreport.collect("24h", True)
        self.assertEqual(files["history.json"], "")
        self.assertIn("said this", files["version.json"])
        failed = next(entry for entry in manifest["commands"] if entry["file"] == "history.json")
        self.assertEqual((failed["exit"], failed["stderr"]), (None, "history: fixture failure\nlast line"))


class Run(unittest.TestCase):
    def test_each_command_runs_here_whatever_thermite_url_names(self):
        with mock.patch.dict(os.environ, {oh_cli.URL_VARIABLE: "ssh://thermite.example"}), \
                mock.patch.object(bugreport.subprocess, "run") as ran:
            ran.return_value.returncode, ran.return_value.stdout, ran.return_value.stderr = 0, "out", ""
            self.assertEqual(bugreport.run(["uname", "-a"])[:3], (0, "out", ""))
        self.assertNotIn(oh_cli.URL_VARIABLE, ran.call_args.kwargs["env"])


class Write(Case):
    def test_it_writes_a_private_archive_here_and_says_where(self):
        status, out, err = self.run_cli()
        self.assertEqual((status, err), (0, ""))
        [path] = self.archives()
        self.assertRegex(path.name, r"^thermite-bugreport-thermite-\d{8}T\d{6}Z\.tar\.gz$")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertIn(f"wrote {path.name} (", out)
        self.assertIn("review it before sharing", out)
        found = members(path.read_bytes())
        folder = path.name.removesuffix(".tar.gz")
        self.assertEqual(set(found), {f"{folder}/{name}" for name in (
            "version.json", "status.json", "history.json", "logs.txt", "events.txt", "system.txt", "manifest.json")})
        self.assertTrue(all(mode == 0o600 for mode, _ in found.values()))
        manifest = json.loads(found[f"{folder}/manifest.json"][1])
        self.assertEqual(manifest["thermite"], oh_cli.VERSION)
        self.assertNotIn(PASSWORD, "".join(text for _, text in found.values()))

    def test_out_and_since_and_json(self):
        status, out, _ = self.run_cli("--out", "report.tar.gz", "--since", "3d", "--json")
        self.assertEqual(status, 0)
        report = json.loads(out)
        self.assertEqual(report["archive"], "report.tar.gz")
        self.assertEqual(report["bytes"], (self.directory / "report.tar.gz").stat().st_size)
        self.assertEqual(report["commands"][3]["command"], "thermite logs --since 3d --utc")

    def test_a_failed_command_is_named_on_stderr_and_the_archive_is_still_written(self):
        self.fake.failing = ("history",)
        status, _, err = self.run_cli("--out", "report.tar.gz")
        self.assertEqual(status, 0)
        self.assertIn("thermite bugreport: thermite history --json --details --last 0 did not run: last line", err)
        self.assertTrue((self.directory / "report.tar.gz").exists())

    def test_out_dash_writes_the_archive_to_standard_output(self):
        stdout = Binary()
        status, said, err = self.run_cli("--out", "-", stdout=stdout)
        self.assertEqual((status, said, err), (0, "", ""))
        self.assertIn("manifest.json", " ".join(members(stdout.buffer.getvalue())))
        self.assertEqual(self.archives(), [])

    def test_what_cannot_work_is_refused(self):
        for argv, said in ((["--out", "-", "--json"], "--json prints a summary"),
                           (["--since", "the day after tomorrow"], "--since"),
                           (["--out", str(self.directory / "missing" / "x.tar.gz")], "cannot write")):
            with self.subTest(argv=argv):
                status, _, err = self.run_cli(*argv, stdout=Binary())
                self.assertEqual(status, 2)
                self.assertIn(said, err)
        self.assertEqual(self.archives(), [])

    def test_out_dash_to_a_terminal_is_refused(self):
        terminal = Binary()
        terminal.isatty = lambda: True
        status, _, err = self.run_cli("--out", "-", stdout=terminal)
        self.assertEqual(status, 2)
        self.assertIn("send standard output to a file or a pipe", err)


class Fetch(Case):
    def over(self, status=0, data=b"archive bytes"):
        def over_ssh(verb, words, url, stdout=None):
            self.called = (verb, list(words), url)
            stdout.write(data)
            return status
        return mock.patch.object(oh_cli, "over_ssh", side_effect=over_ssh)

    def test_it_collects_there_and_writes_the_archive_here(self):
        with self.over():
            status, out, _ = self.run_cli("--url", "ssh://owner@thermite.example:2222/srv/site", "--since", "2h")
        self.assertEqual(status, 0)
        self.assertEqual(self.called, ("bugreport", ["--since", "2h", "--out", "-"],
                                       "ssh://owner@thermite.example:2222/srv/site"))
        [path] = self.archives()
        self.assertTrue(path.name.startswith("thermite-bugreport-thermite-"))
        self.assertEqual((path.read_bytes(), stat.S_IMODE(path.stat().st_mode)), (b"archive bytes", 0o600))
        self.assertIn("from owner@thermite.example", out)

    def test_out_and_json_stay_here(self):
        with self.over():
            status, out, _ = self.run_cli("--json", "--url=ssh://thermite.example", "--out", "here.tar.gz")
        self.assertEqual(status, 0)
        self.assertEqual(self.called[1], ["--out", "-"])
        self.assertEqual(json.loads(out), {"archive": "here.tar.gz", "bytes": 13, "url": "ssh://thermite.example"})

    def test_a_failure_there_leaves_nothing_here(self):
        with self.over(status=2, data=b"part"):
            status, _, _ = self.run_cli("--url", "ssh://thermite.example", "--out", "here.tar.gz")
        self.assertEqual(status, 2)
        self.assertEqual(list(self.directory.glob("*.tar.gz*")) + list(self.directory.glob(".*tmp")), [])

    def test_quiet_stays_here_and_verbose_goes_there(self):
        for words, sent in ((["-qv"], ["-v"]), (["-q", "--verbose"], ["--verbose"]), (["--quiet"], []),
                            (["-vq", "--no-color"], ["-v", "--no-color"])):
            with self.subTest(words=words), self.over():
                status, out, _ = self.run_cli("--url", "ssh://thermite.example", *words, "--out", "here.tar.gz")
                self.assertEqual((status, out), (0, ""))
                self.assertEqual(self.called[1], [*sent, "--out", "-"])

    def test_thermite_url_names_the_server(self):
        os.environ[oh_cli.URL_VARIABLE] = "ssh://thermite.example"
        with self.over():
            status, _, _ = self.run_cli("--out", "here.tar.gz")
        self.assertEqual((status, self.called[2]), (0, "ssh://thermite.example"))

    def test_out_dash_over_ssh_refuses_json_and_quiet(self):
        for words, said in ((["--out", "-", "--json"], "--json prints a summary"),
                            (["--out", "-", "-q"], "-q silences standard output"),
                            (["--out", "-", "--quiet"], "-q silences standard output")):
            with self.subTest(words=words), self.over():
                status, _, err = self.run_cli("--url", "ssh://thermite.example", *words, stdout=Binary())
                self.assertEqual(status, 2)
                self.assertIn(said, err)

    def test_fetch_measures_size_with_stat_not_read_bytes(self):
        with self.over(), mock.patch.object(Path, "read_bytes", side_effect=AssertionError("must not load whole archive")):
            status, out, _ = self.run_cli("--json", "--url=ssh://thermite.example", "--out", "here.tar.gz")
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(out)["bytes"], 13)


if __name__ == "__main__":
    unittest.main()
