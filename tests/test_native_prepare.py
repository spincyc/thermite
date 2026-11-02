"""Run native preparation against controlled fake JVMs, never a real runtime."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))
import oh_deploy as deploy


FAKE_JVM = r'''
import json,os,signal,sys,time
from pathlib import Path
root=Path(os.environ['OPENHAB_USERDATA'])
control=Path(os.environ['FIXTURE_CONTROL'])
def event(name):
    with (control/'events').open('a') as stream:stream.write(name+'\n')
def stop(signum,frame):
    event('TERM')
    (root/'openhab-start-level').write_text('0\n')
    (control/'stopping').touch()
    if os.environ.get('FIXTURE_HOLD_EXIT'):
        while not (control/'allow-exit').exists():time.sleep(.01)
    event('EXIT')
    (control/'exited').touch()
    raise SystemExit(0)
signal.signal(signal.SIGTERM,signal.SIG_IGN if os.environ.get('FIXTURE_IGNORE_TERM') else stop)
(control/'pid').write_text(str(os.getpid()))
assert (root/'cache-refresh-lock').exists(),'upstream cache priming was not disabled'
conf=Path(os.environ['OPENHAB_CONF'])
assert not list(conf.glob('automation/**/*')),'actual house rules reached the primer'
assert 'no add-ons' in (conf/'services/addons.cfg').read_text()
assert os.environ['OPENHAB_HTTP_ADDRESS']=='127.0.0.1'
assert os.environ['OPENHAB_HTTP_PORT']==os.environ['OPENHAB_HTTPS_PORT']=='0'
assert 'sshHost = 127.0.0.1' in (root/'etc/org.apache.karaf.shell.cfg').read_text()
assert os.environ['KARAF_EXEC']=='exec'
assert '-Duser.home='+str(root) in os.environ['EXTRA_JAVA_OPTS']
event('START')
(control/'identity').write_text(json.dumps([os.geteuid(),os.getegid()]))
(root/'cache/org.eclipse.osgi').mkdir(parents=True,exist_ok=True)
if os.environ.get('FIXTURE_CRASH'):raise SystemExit(7)
(root/'openhab-start-level').write_text(os.environ.get('FIXTURE_LEVEL','10')+'\n')
while True:time.sleep(.01)
'''


class NativePreparation(unittest.TestCase):
    def setUp(self):
        repository = next((parent for parent in (AREA, *AREA.parents) if (parent / ".git").exists()), AREA)
        scratch = Path(os.environ.get("TMPDIR", repository / ".scratch/fix-native-prime"))
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="test-", dir=scratch)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.home = self.root / "openhab"
        self.userdata = self.home / "userdata"
        self.control = self.root / "control"
        self.tools = self.root / "tools"
        for path in (self.userdata, self.home / "conf/automation/js", self.home / "addons",
                     self.home / "dist/conf", self.home / "dist/userdata/etc", self.home / "runtime/bin",
                     self.control, self.tools):
            path.mkdir(parents=True, exist_ok=True)
        self.version = self.home / "dist/userdata/etc/version.properties"
        self.version.write_text("openhab-distro = 5.2.3\n")
        (self.home / "dist/userdata/etc/org.apache.karaf.shell.cfg").write_text("sshHost = 0.0.0.0\nsshPort = 8101\n")
        (self.home / "conf/automation/js/actual-house.js").write_text("must not run during cache priming\n")
        (self.root / "fake-jvm.py").write_text(FAKE_JVM)
        karaf = self.home / "runtime/bin/karaf"
        karaf.write_text("#!/bin/sh\nexec " + shlex.join([sys.executable, str(self.root / "fake-jvm.py")]) + "\n")
        karaf.chmod(0o700)
        tini = self.tools / "tini"
        tini.write_text('#!/bin/sh\ntest -e "$FIXTURE_CONTROL/exited" || exit 99\nprintf "real start\\n" >> "$FIXTURE_CONTROL/events"\n')
        tini.chmod(0o700)
        self.environment = {**os.environ, "PATH": str(self.tools) + ":/usr/bin:/bin",
                            "OPENHAB_HOME": str(self.home), "OPENHAB_USERDATA": str(self.userdata),
                            "OPENHAB_BACKUPS": str(self.userdata / "backup"), "OPENHAB_LOGDIR": str(self.userdata / "logs"),
                            "EXTRA_JAVA_OPTS": "", "FIXTURE_CONTROL": str(self.control)}
        self.processes = []
        self.addCleanup(self.stop_fixtures)

    def stop_fixtures(self):
        (self.control / "allow-exit").touch()
        for process in self.processes:
            if process.poll() is None:
                # Only our isolated fake processes are ever terminated here.
                os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=5)

    def prepare(self, **environment):
        with patch.object(deploy, "NATIVE_PRIME_WAIT", 30), patch.object(deploy, "NATIVE_PRIME_STOP_WAIT", 30), \
                patch.object(deploy, "NATIVE_PRIME_POLL", .02):
            command = deploy.openhab_prepare().replace("/openhab/runtime/bin/update", str(self.home / "runtime/bin/update"))
        process = subprocess.Popen(["sh", "-ec", command], cwd=self.home, env={**self.environment, **environment},
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
        self.processes.append(process)
        return process

    def completed(self, **environment):
        process = self.prepare(**environment)
        out, err = process.communicate(timeout=5)
        return process.returncode, out, err

    def wait_file(self, path, process):
        deadline = time.monotonic() + 3
        while not path.exists():
            if process.poll() is not None:
                out, err = process.communicate()
                self.fail(f"preparation exited before {path.name}: {out}\n{err}")
            if time.monotonic() >= deadline:
                self.fail(f"timed out waiting for {path.name}")
            time.sleep(.01)

    def existing_userdata(self, *, cache=False):
        (self.userdata / "etc").mkdir()
        (self.userdata / "etc/version.properties").write_bytes(self.version.read_bytes())
        if cache:
            (self.userdata / "cache/org.eclipse.osgi").mkdir(parents=True)
        (self.userdata / "jsondb").mkdir()
        credential = self.userdata / "jsondb/users.json"
        credential.write_bytes(b'{"fixture":"persistent authority"}\n')
        credential.chmod(0o600)
        return credential

    def snapshot(self, path):
        info = path.stat()
        return info.st_ino, info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode), path.read_bytes()

    def test_zero_marker_does_not_release_preparation_before_actual_jvm_exit(self):
        process = self.prepare(FIXTURE_HOLD_EXIT="1")
        self.wait_file(self.control / "stopping", process)
        self.assertEqual((self.userdata / "openhab-start-level").read_text(), "0\n")
        self.assertIsNone(process.poll(), "marker 0 must not count as JVM exit")
        self.assertTrue((self.userdata / deploy.NATIVE_PRIME_MARKER).exists())
        (self.control / "allow-exit").touch()
        out, err = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0, out + err)
        self.assertEqual(json.loads((self.control / "identity").read_text()), [os.geteuid(), os.getegid()])
        self.assertFalse((self.userdata / deploy.NATIVE_PRIME_MARKER).exists())
        self.assertFalse((self.userdata / "cache-refresh-lock").exists())
        self.assertTrue((self.userdata / "cache/org.eclipse.osgi").is_dir())
        started = subprocess.run(["sh", "-ec", deploy.openhab_start()], cwd=self.home, env=self.environment,
                                 capture_output=True, text=True, timeout=5)
        self.assertEqual(started.returncode, 0, started.stderr)
        self.assertEqual((self.control / "events").read_text().splitlines(), ["START", "TERM", "EXIT", "real start"])

    def test_warmed_userdata_is_unchanged_and_never_launches_a_primer(self):
        credential = self.existing_userdata(cache=True)
        before = self.snapshot(credential)
        self.assertEqual(self.completed()[0], 0)
        self.assertEqual(self.snapshot(credential), before)
        self.assertFalse((self.control / "pid").exists())
        self.assertFalse((self.userdata / deploy.NATIVE_PRIME_MARKER).exists())

    def test_established_restore_without_cache_uses_normal_startup_without_offline_rules(self):
        credential = self.existing_userdata()
        before = self.snapshot(credential)
        code, out, err = self.completed()
        self.assertEqual(code, 0, out + err)
        self.assertIn("Skipping offline cache priming for existing userdata", out)
        self.assertEqual(self.snapshot(credential), before)
        self.assertFalse((self.control / "pid").exists())
        self.assertFalse((self.userdata / "cache").exists())

    def test_managed_jsondb_or_local_addons_prevent_automatic_fresh_priming(self):
        source = self.home / "dist/userdata/jsondb"
        source.mkdir()
        (source / "users.json").write_text('{"fixture":"managed"}')
        code, out, err = self.completed()
        self.assertEqual(code, 0, out + err)
        self.assertIn("managed jsondb state exists", out)
        self.assertFalse((self.control / "pid").exists())
        (self.home / "addons/local.jar").write_text("prepositioned addon")
        # A separate fresh copy tests the local-addon guard independently.
        shutil.rmtree(self.userdata)
        self.userdata.mkdir()
        shutil.rmtree(source)
        code, out, err = self.completed()
        self.assertEqual(code, 0, out + err)
        self.assertIn("local add-on files exist", out)
        self.assertFalse((self.control / "pid").exists())

    def test_failed_or_interrupted_prime_retains_cache_marker_and_blocks_every_rerun(self):
        code, out, err = self.completed(FIXTURE_LEVEL="5")
        self.assertNotEqual(code, 0)
        self.assertIn("did not reach start level 10", err)
        self.assertTrue((self.control / "exited").exists())
        marker = self.userdata / deploy.NATIVE_PRIME_MARKER
        before = self.snapshot(marker)
        self.assertTrue((self.userdata / "cache/org.eclipse.osgi").is_dir())
        self.assertTrue((self.userdata / "cache-refresh-lock").exists())
        self.assertEqual(len(list((self.userdata / "tmp").glob("thermite-prime.*/console.log"))), 1)
        code, out, err = self.completed()
        self.assertNotEqual(code, 0)
        self.assertIn("Incomplete fresh native cache priming", err)
        self.assertEqual(self.snapshot(marker), before)
        self.assertEqual((self.control / "events").read_text().splitlines(), ["START", "TERM", "EXIT"])
        native = subprocess.run(["sh", "-ec", deploy.openhab_start()], cwd=self.home, env=self.environment,
                                capture_output=True, text=True, timeout=5)
        self.assertNotEqual(native.returncode, 0)
        self.assertIn("Incomplete fresh native cache priming", native.stderr)

    def test_unresponsive_fresh_primer_is_bounded_and_never_reported_successful(self):
        code, out, err = self.completed(FIXTURE_IGNORE_TERM="1")
        self.assertNotEqual(code, 0)
        self.assertIn("did not stop within its grace", err)
        self.assertTrue((self.userdata / deploy.NATIVE_PRIME_MARKER).exists())
        self.assertFalse((self.control / "exited").exists())
        with self.assertRaises(ProcessLookupError):
            os.kill(int((self.control / "pid").read_text()), 0)

    def test_crashed_primer_is_not_accepted_as_a_warmed_cache(self):
        code, out, err = self.completed(FIXTURE_CRASH="1")
        self.assertNotEqual(code, 0)
        self.assertIn("did not reach start level 10", err)
        self.assertTrue((self.userdata / deploy.NATIVE_PRIME_MARKER).exists())

    def test_interruption_waits_for_the_fresh_child_and_retains_the_failure_marker(self):
        process = self.prepare(FIXTURE_LEVEL="5")
        self.wait_file(self.control / "pid", process)
        process.send_signal(signal.SIGTERM)
        process.communicate(timeout=5)
        self.assertEqual(process.returncode, 143)
        self.assertTrue((self.control / "exited").exists())
        self.assertTrue((self.userdata / deploy.NATIVE_PRIME_MARKER).exists())

    def test_existing_updater_keeps_its_hash_guard_backup_and_authority(self):
        credential = self.existing_userdata(cache=True)
        before = self.snapshot(credential)
        (self.userdata / "etc/version.properties").write_text("openhab-distro = 5.1.0\n")
        updater = self.home / "runtime/bin/update"
        updater.write_text('''#!/bin/sh
  # Ask to run as root
  if test "$(id -u)" != 0; then
    echo 'Please run this script as root!' >&2
    exit 1
  fi
cp "$OPENHAB_HOME/dist/userdata/etc/version.properties" "$OPENHAB_USERDATA/etc/version.properties"
''')
        with patch.object(deploy, "OPENHAB_UPDATE_SHA256", hashlib.sha256(updater.read_bytes()).hexdigest()):
            code, out, err = self.completed()
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.snapshot(credential), before)
        self.assertEqual((self.userdata / "etc/version.properties").read_bytes(), self.version.read_bytes())
        self.assertEqual(len(list((self.userdata / "backup").glob("thermite-before-*.tar"))), 1)
        self.assertFalse((self.control / "pid").exists())
        (self.userdata / "etc/version.properties").write_text("openhab-distro = 5.1.0\n")
        code, out, err = self.completed()
        self.assertNotEqual(code, 0)
        self.assertIn("FAILED", out + err)
        self.assertEqual(self.snapshot(credential), before)
        self.assertIn("5.1.0", (self.userdata / "etc/version.properties").read_text())


if __name__ == "__main__":
    unittest.main()
