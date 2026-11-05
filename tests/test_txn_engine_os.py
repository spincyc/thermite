"""The install engine on real directories, and its command line as root runs it.

Everything happens in temporary directories this test owns, as the user
running it: the engine accepts its own effective uid where root would be.
systemctl and nft are small scripts that keep their state in files; cp and
sync are the system's. The command line runs a copy of engine.py under
/usr/bin/python3 -I -S with SYSTEM pointed at a scratch tree, the way the
operator's private copy runs under sudo.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))

from oh_install import engine

PYTHON = "/usr/bin/python3"
ID = "20261105T101500Z-3f9a2c"
SECOND = "20261105T121500Z-4b0d1e"
OLD = "20261020T080000Z-77ab01"
BOUNDARY = "thermite-native-boundary.service"


def reflink():
    cp = shutil.which("cp", path="/usr/bin:/bin")
    if cp is None:
        return None
    result = subprocess.run([cp, "--help"], capture_output=True, text=True, check=False)
    return cp if "--reflink" in result.stdout else None


CP = reflink()
SYNC = shutil.which("sync", path="/usr/bin:/bin")


def script(path, text):
    path.write_text("#!/bin/sh\n" + text)
    path.chmod(0o755)
    return str(path)


@unittest.skipUnless(CP and SYNC, "needs GNU cp with --reflink and sync")
class RealDisk(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.disk = engine.OsDisk({"cp": engine.trusted_tool("cp", CP), "sync": engine.trusted_tool("sync", SYNC)})

    def test_copies_keep_links_hard_links_modes_and_times(self):
        source = self.base / "userdata"
        (source / "jsondb").mkdir(parents=True)
        (source / "jsondb/things.json").write_bytes(b"fictional things")
        (source / "jsondb/things.json").chmod(0o640)
        os.link(source / "jsondb/things.json", source / "jsondb/hard")
        (source / "current").symlink_to("jsondb/things.json")
        (source / "outside").symlink_to("/nonexistent/fixture")
        os.utime(source / "jsondb/things.json", ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000))
        source.chmod(0o750)
        self.disk.copy(source, self.base / "aside")
        copy = self.base / "aside"
        self.assertEqual(stat.S_IMODE(copy.stat().st_mode), 0o750)
        self.assertEqual((copy / "jsondb/things.json").read_bytes(), b"fictional things")
        self.assertEqual(stat.S_IMODE((copy / "jsondb/things.json").stat().st_mode), 0o640)
        self.assertEqual((copy / "jsondb/things.json").stat().st_mtime_ns, 1_700_000_000_000_000_000)
        self.assertEqual((copy / "jsondb/things.json").stat().st_ino, (copy / "jsondb/hard").stat().st_ino)
        self.assertEqual(os.readlink(copy / "current"), "jsondb/things.json")
        self.assertEqual(os.readlink(copy / "outside"), "/nonexistent/fixture")
        with self.assertRaisesRegex(engine.EngineError, "cp -a --reflink=auto --no-target-directory"):
            self.disk.copy(self.base / "missing", self.base / "elsewhere")

    def test_write_atomic_syncs_the_file_then_renames_then_syncs_the_directory(self):
        calls = []
        real_fsync, real_replace = os.fsync, os.replace

        def fsync(descriptor):
            calls.append(("fsync", stat.S_ISDIR(os.fstat(descriptor).st_mode)))
            real_fsync(descriptor)

        def replace(source, destination):
            calls.append(("replace", Path(source).name, Path(destination).name))
            real_replace(source, destination)

        target = self.base / "journal.json"
        target.write_bytes(b"old")
        with mock.patch("os.fsync", fsync), mock.patch("os.replace", replace):
            self.disk.write_atomic(target, b"new", 0o600)
        self.assertEqual(calls, [("fsync", False), ("replace", ".journal.json.tmp", "journal.json"), ("fsync", True)])
        self.assertEqual(target.read_bytes(), b"new")
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertEqual(sorted(path.name for path in self.base.iterdir()), ["journal.json"])

    def test_sync_runs_sync_on_the_files_filesystem(self):
        record = self.base / "sync.log"
        tool = script(self.base / "sync", f'echo "$(pwd) $*" >> {record}\n')
        with mock.patch.object(engine, "TOOL_DIRS", (str(self.base),)):
            disk = engine.OsDisk({"sync": engine.trusted_tool("sync", tool)})
        disk.sync_fs(self.base / "files/.3.partial")
        self.assertEqual(record.read_text(), f"/ --file-system -- {self.base}/files/.3.partial\n")

    def test_tools_run_from_the_root_directory(self):
        """A tool never finds a program or file in the caller's directory."""
        planted = self.base / "is-enabled"
        planted.write_text("echo planted\n")
        tool = script(self.base / "systemctl", 'pwd; [ ! -e ./is-enabled ] || echo "found ./is-enabled"\n')
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(self.base)
        host = engine.SubprocessHost({"systemctl": tool})
        self.assertEqual(host.capture(("systemctl", "is-enabled", engine.TARGET)), (0, "/\n"))

    def test_sizes_are_allocated_blocks(self):
        tree = self.base / "tree"
        tree.mkdir()
        (tree / "dense").write_bytes(b"x" * 100_000)
        with open(tree / "sparse", "wb") as sparse:
            sparse.truncate(50 << 20)
        (tree / "link").symlink_to("/nonexistent")
        expected = sum(os.lstat(path).st_blocks * 512 for path in (tree, *tree.iterdir()))
        self.assertEqual(self.disk.allocated(tree), expected)
        self.assertLess(self.disk.allocated(tree), 10 << 20)
        self.assertGreater(self.disk.available(self.base), 0)

    def test_removal_never_follows_a_symlink(self):
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "keep").write_text("kept")
        tree = self.base / "tree"
        (tree / "deep").mkdir(parents=True)
        (tree / "deep/file").write_text("gone")
        (tree / "deep/escape").symlink_to(outside)
        self.disk.remove_tree(tree)
        self.assertFalse(tree.exists())
        self.assertEqual((outside / "keep").read_text(), "kept")
        self.disk.remove_tree(tree)  # Absent is done.

    def test_the_lock_is_exclusive_and_renames_never_replace(self):
        with self.disk.lock(self.base):
            with self.assertRaisesRegex(engine.EngineError, "another engine verb is running"):
                with self.disk.lock(self.base):
                    pass
        with self.disk.lock(self.base):
            pass
        (self.base / "a").write_text("a")
        (self.base / "b").write_text("b")
        with self.assertRaises(FileExistsError):
            self.disk.rename(self.base / "a", self.base / "b")
        self.assertEqual((self.base / "b").read_text(), "b")

    def test_reads_are_bounded_and_never_follow_a_symlink(self):
        (self.base / "file").write_bytes(b"12345")
        (self.base / "link").symlink_to(self.base / "file")
        self.assertEqual(self.disk.read_bytes(self.base / "file", 5), b"12345")
        with self.assertRaisesRegex(engine.EngineError, "larger than 4 bytes"):
            self.disk.read_bytes(self.base / "file", 4)
        with self.assertRaises(OSError):
            self.disk.read_bytes(self.base / "link", 5)

    def test_only_tools_root_alone_can_change_are_run(self):
        self.assertEqual(engine.trusted_tool("cp", CP), os.path.realpath(CP))
        # Any root-owned program is not any tool: a shell named as systemctl.
        for name, value in (("systemctl", "/usr/bin/sh"), ("systemctl", CP), ("cp", "/usr/local/bin/cp"),
                            ("cp", "/usr/bin/../bin/cp"), ("python3", "/usr/bin/python3")):
            with self.subTest(name=name, value=value):
                with self.assertRaises(engine.EngineError):
                    engine.trusted_tool(name, value)
        with self.assertRaisesRegex(engine.EngineError, "must be a program named systemctl"):
            engine.trusted_tools({"systemctl": "/usr/bin/sh", "cp": CP, "sync": SYNC, "nft": None,
                                  "apparmor_parser": None, "podman": None})
        tools = self.base / "tools"
        tools.mkdir(mode=0o700)
        shared = tools / "shared"
        shared.mkdir()
        with mock.patch.object(engine, "TOOL_DIRS", (str(tools), str(shared))):
            loose = script(tools / "nft", "exit 0\n")
            os.chmod(loose, 0o775)
            with self.assertRaisesRegex(engine.EngineError, "must be an executable file only root can change"):
                engine.trusted_tool("nft", loose)
            tool = script(shared / "podman", "exit 0\n")
            shared.chmod(0o777)
            with self.assertRaisesRegex(engine.EngineError, "could be changed by someone other than root"):
                engine.trusted_tool("podman", tool)
            shared.chmod(0o755)
            self.assertEqual(engine.trusted_tool("podman", tool), tool)
            if os.geteuid():  # A tool the caller owns is not root's when root is the only owner.
                with self.assertRaisesRegex(engine.EngineError, "someone other than root"):
                    engine.trusted_tool("podman", tool, owner=0)


def snapshot(*tops, skip=()):
    """Every path below the tops: its kind, mode and bytes or link target."""
    found = {}
    for top in tops:
        for directory, names, files in os.walk(top):
            names[:] = sorted(name for name in names if Path(directory, name) not in skip)
            for name in [*names, *files]:
                path = Path(directory, name)
                if path in skip:
                    continue
                info = path.lstat()
                content = (os.readlink(path) if path.is_symlink() else path.read_bytes() if path.is_file() else None)
                found[str(path)] = (stat.S_IFMT(info.st_mode), stat.S_IMODE(info.st_mode), content)
    return found


@unittest.skipUnless(CP and SYNC and os.access(PYTHON, os.X_OK), "needs /usr/bin/python3 and GNU cp with --reflink")
class CommandLine(unittest.TestCase):
    """Two transactions through `engine.py <verb>`: one rolled back, one
    committed, each later verb started from the operator's copy and run by
    the copy begin pinned."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = base = Path(temporary.name)
        self.system, self.root, self.site, self.state = base / "system", base / "srv", base / "site", base / "state"
        for path in (self.system, self.root, self.state):
            path.mkdir(mode=0o755)
        self.site.mkdir(mode=0o700)
        tools = base / "tools"
        tools.mkdir(mode=0o700)
        units = self.system / "etc/systemd/system"
        wants = units / "multi-user.target.wants"
        self.units, self.wants = units, wants
        self.tools = {
            "systemctl": script(tools / "systemctl", f"""\
state={self.state} units={units} wants={wants}
echo "$*" >> "$state/log"
pwd >> "$state/cwd"
case "$1" in
  is-enabled) [ -L "$wants/thermite.target" ] && {{ echo enabled; exit 0; }}; echo disabled; exit 1 ;;
  is-active) [ -e "$state/active" ] && {{ echo active; exit 0; }}; echo inactive; exit 3 ;;
  show) if [ -e "$state/active" ]; then echo active; else echo inactive; fi ;;
  disable) rm -f "$wants/thermite.target" ;;
  enable) [ -e "$units/thermite.target" ] || exit 1; ln -sfn "$units/thermite.target" "$wants/thermite.target" ;;
  stop) rm -f "$state/active" ;;
  start) [ -e "$units/thermite.target" ] || exit 5; touch "$state/active" ;;
  restart) cp "{self.system}/etc/thermite-native.nft" "$state/table" ;;
  daemon-reload|reset-failed) ;;
  *) exit 64 ;;
esac
"""),
            "nft": script(tools / "nft", f"""\
state={self.state}
case "$1" in
  list) [ -e "$state/table" ] || exit 1; cat "$state/table" ;;
  delete) [ -e "$state/table" ] || exit 1; rm "$state/table" ;;
  *) exit 64 ;;
esac
"""),
            "cp": CP, "sync": SYNC, "apparmor_parser": None, "podman": None,
        }
        source = (AREA / "lib/oh_install/engine.py").read_text()
        dirs = 'TOOL_DIRS = ("/usr/bin", "/usr/sbin", "/bin", "/sbin")\n'
        self.assertEqual((source.count('SYSTEM = Path("/")\n'), source.count(dirs)), (1, 1))
        self.copy = self.site / "engine.py"
        self.copy.write_text(source.replace('SYSTEM = Path("/")\n', f"SYSTEM = Path({str(self.system)!r})\n")
                             .replace(dirs, f'TOOL_DIRS = ("/usr/bin", "/bin", {str(tools)!r})\n'))
        self.copy.chmod(0o600)
        self.install_old()

    def write(self, path, data, mode=0o644):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(mode)
        return path

    def files(self, label):
        return {self.units / "thermite.target": f"[Unit]\nDescription=Fictional target {label}\n".encode(),
                self.units / BOUNDARY: f"[Unit]\nDescription=Fictional boundary {label}\n".encode(),
                self.system / "etc/thermite-native.nft": f"table inet thermite_native {{ # {label} }}\n".encode()}

    def install_old(self):
        for path, data in self.files("old").items():
            self.write(path, data)
        self.wants.mkdir(parents=True, exist_ok=True)
        (self.wants / BOUNDARY).symlink_to(self.units / BOUNDARY)
        (self.wants / "thermite.target").symlink_to(self.units / "thermite.target")
        self.write(self.root / "runtime/userdata/jsondb/things.json", b"old things", 0o640)
        self.write(self.root / f"access/releases/{OLD}/config.json", b"{}")
        (self.system / "etc/thermite").mkdir(parents=True, exist_ok=True)
        uid, gid = os.geteuid(), os.getegid()
        items = [{"path": str(path), "class": "nft" if path.suffix == ".nft" else "unit",
                  "sha256": hashlib.sha256(data).hexdigest(), "mode": 0o644, "uid": uid, "gid": gid,
                  **({"apply_unit": BOUNDARY} if path.suffix == ".nft" else {"unit": path.name})}
                 for path, data in self.files("old").items()]
        items += [{"path": str(self.wants / BOUNDARY), "class": "link", "target": str(self.units / BOUNDARY)},
                  {"path": str(self.root / "runtime/userdata"), "class": "tree"},
                  {"path": str(self.root / "access/releases"), "class": "versioned", "current": OLD, "previous": None}]
        manifest = engine.validate_manifest({
            "format": 2, "installation": str(self.root), "transaction": OLD, "kept": OLD,
            "committed_at": "2026-10-20T08:10:00Z", "installed": True, "release": {"label": "old"}, "pins": {},
            "tools": self.tools, "items": items, "images": {"current": [], "previous": []},
            "application": {"current": "sha256:old", "previous": None}}, root=self.root, system=self.system)
        self.write(self.system / "etc/thermite/installed.json", engine.render_manifest(manifest))
        (self.state / "active").touch()
        (self.state / "table").write_bytes(self.files("old")[self.system / "etc/thermite-native.nft"])
        desired = [{"path": item["path"], "class": item["class"],
                    **{key: item[key] for key in ("unit", "apply_unit", "target") if key in item}} for item in items]
        self.inputs = {"desired": self.write(self.site / "desired.json", json.dumps(
                           {"kind": "upgrade", "tools": self.tools, "items": desired}).encode(), 0o600),
                       "operator": self.write(self.site / "operator.json", json.dumps(
                           {"uid": os.geteuid(), "site": str(self.site), "record": str(self.site / "record.json")}
                       ).encode(), 0o600),
                       "release": self.write(self.site / "release.json", json.dumps(
                           {"release": {"label": "new"}, "pins": {}, "images": [],
                            "application": "sha256:new"}).encode(), 0o600)}

    def run_engine(self, verb, *argv, status=0, copy=None):
        result = subprocess.run([PYTHON, "-I", "-S", str(copy or self.copy), verb, "--root", str(self.root), *argv],
                                capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"}, cwd=self.base,
                                check=False)
        self.assertEqual(result.returncode, status, f"{verb}: {result.stdout}{result.stderr}")
        return result

    def switch(self, id):
        for path, data in self.files("new").items():
            path.write_bytes(data)
        self.write(self.root / "runtime/userdata/jsondb/things.json", b"migrated things", 0o640)
        self.write(self.root / f"access/releases/{id}/config.json", b'{"new": true}')
        subprocess.run([self.tools["systemctl"], "restart", BOUNDARY], check=True, cwd="/")
        subprocess.run([self.tools["systemctl"], "start", "thermite.target"], check=True, cwd="/")

    def test_a_rolled_back_and_a_committed_transaction(self):
        before = snapshot(self.system, self.root, skip={self.root / ".transaction"})
        state = snapshot(self.state, skip={self.state / "log", self.state / "cwd"})
        plan = json.loads(self.run_engine("plan", "--desired", str(self.inputs["desired"])).stdout)
        self.assertEqual(plan["required"], plan["margin"] + sum(count for _, count in plan["items"]))
        begun = json.loads(self.run_engine("begin", "--id", ID, "--desired", str(self.inputs["desired"]),
                                           "--operator", str(self.inputs["operator"])).stdout)
        pinned = self.root / f".transaction/{ID}/engine.py"
        self.assertEqual(begun, {"id": ID, "phase": "planned", "engine": str(pinned)})
        self.assertEqual(pinned.read_bytes(), self.copy.read_bytes())
        self.assertEqual(stat.S_IMODE(pinned.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(pinned.parent.stat().st_mode), 0o700)
        # Change the operator's copy: only the pinned one can run the rest.
        with open(self.copy, "a") as copy:
            copy.write("# a later release's engine\n")
        self.run_engine("guard", "--id", ID)
        self.assertFalse((self.wants / "thermite.target").exists())
        self.assertIn("collect: ", self.run_engine("collect", "--id", ID, status=1).stderr.replace(
            "thermite install engine: thermite.target must be disabled and thermite.slice stopped before collecting",
            "collect: "))
        subprocess.run([self.tools["systemctl"], "stop", "thermite.target", "thermite.slice"], check=True, cwd="/")
        self.run_engine("collect", "--id", ID)
        self.switch(ID)
        printed = self.run_engine("rollback", "--print").stdout
        self.assertIn(f"mv -T {self.root}/.transaction/{ID}/files/", printed)
        self.run_engine("rollback", status=engine.ROLLED_BACK)
        self.assertEqual(snapshot(self.system, self.root, skip={self.root / ".transaction"}), before)
        self.assertEqual(snapshot(self.state, skip={self.state / "log", self.state / "cwd"}), state)
        self.assertEqual(self.run_engine("rollback").stdout, "No transaction is open.\n")
        journal = json.loads((self.root / f".transaction/{ID}/journal.json").read_bytes())
        self.assertEqual((journal["phase"], journal["outcome"]["started"]), ("rolled-back", True))

        # The changed copy begins the next transaction, pins itself, and commits.
        self.run_engine("begin", "--id", SECOND, "--desired", str(self.inputs["desired"]),
                        "--operator", str(self.inputs["operator"]))
        self.run_engine("guard", "--id", SECOND)
        subprocess.run([self.tools["systemctl"], "stop", "thermite.target", "thermite.slice"], check=True, cwd="/")
        self.run_engine("collect", "--id", SECOND)
        self.switch(SECOND)
        self.assertEqual(json.loads(self.run_engine("commit", "--id", SECOND, "--release",
                                                    str(self.inputs["release"])).stdout),
                         {"id": SECOND, "phase": "kept"})
        installed = engine.parse_manifest((self.system / "etc/thermite/installed.json").read_bytes(), root=self.root,
                                          system=self.system)
        self.assertEqual(installed["transaction"], SECOND)
        rules = next(item for item in installed["items"] if item["class"] == "nft")
        self.assertEqual(rules["sha256"], hashlib.sha256(self.files("new")[Path(rules["path"])]).hexdigest())
        self.assertTrue((self.wants / "thermite.target").is_symlink())
        status = json.loads(self.run_engine("status", "--json").stdout)
        self.assertEqual([(each["id"], each["phase"]) for each in status], [(SECOND, "kept")])
        self.assertIn(f"{SECOND}: upgrade kept, the kept backup (", self.run_engine("status").stdout)
        self.assertEqual(set((self.state / "cwd").read_text().split()), {"/"}, "every tool ran from /")

    def test_a_journal_of_another_format_needs_the_engine_that_wrote_it(self):
        self.run_engine("begin", "--id", ID, "--desired", str(self.inputs["desired"]),
                        "--operator", str(self.inputs["operator"]))
        path = self.root / f".transaction/{ID}/journal.json"
        journal = json.loads(path.read_bytes())
        journal["format"] = 99
        path.write_text(json.dumps(journal))
        pinned = self.root / f".transaction/{ID}/engine.py"
        status = self.run_engine("status").stdout
        self.assertIn(f"has format 99, not {engine.FORMAT}; run the engine that wrote it: {pinned}", status)
        refused = self.run_engine("rollback", status=engine.FAILED).stderr
        self.assertIn("run the engine that wrote it", refused)
        self.assertTrue((self.wants / "thermite.target").is_symlink(), "nothing was rolled back")

    def test_an_unsafe_pinned_copy_is_never_run(self):
        self.run_engine("begin", "--id", ID, "--desired", str(self.inputs["desired"]),
                        "--operator", str(self.inputs["operator"]))
        pinned = self.root / f".transaction/{ID}/engine.py"
        pinned.chmod(0o644)
        self.assertIn(f"{pinned} is not private to root", self.run_engine("guard", "--id", ID, status=1).stderr)
        self.assertTrue((self.wants / "thermite.target").is_symlink())

    def test_not_enough_room_refuses_with_the_table(self):
        with mock.patch.object(engine.OsDisk, "available", return_value=0):
            space = engine.Engine(engine.OsDisk(), engine.SubprocessHost(), self.root, system=self.system).plan(
                json.loads(self.inputs["desired"].read_bytes()))
        self.assertIn("Not enough space: nothing was stopped or changed.", engine.describe_space(space, self.root))


if __name__ == "__main__":
    unittest.main()
