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


FAKE_ENVIRONMENT = r'''
import json,os,socket,sys,time
from pathlib import Path
root=Path(os.environ['OPENHAB_USERDATA'])
control=Path(os.environ['FIXTURE_CONTROL'])
def event(name):
    with (control/'events').open('a') as stream:stream.write(name+'\n')
properties={}
for line in (root/'etc/custom.properties').read_text().splitlines():
    if '=' in line and not line.lstrip().startswith('#'):
        key,value=line.split('=',1)
        properties[key.strip()]=value.strip()
assert properties['karaf.shutdown.port']=='0'
assert properties['karaf.shutdown.host']=='127.0.0.1'
portfile=Path(properties['karaf.shutdown.port.file'])
assert portfile.parent.name.startswith('thermite-prime.')
assert portfile.parent.parent==root/'tmp'
environment={key:os.environ[key] for key in ('JAVA_HOME','KARAF_EXEC','OPENHAB_CONF','OPENHAB_LOGDIR',
    'OPENHAB_HTTP_ADDRESS','OPENHAB_HTTP_PORT','OPENHAB_HTTPS_PORT','EXTRA_JAVA_OPTS')}
'''

FAKE_JVM = FAKE_ENVIRONMENT + r'''
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
(control/'prime-environment').write_text(json.dumps(environment))
event('START')
(control/'identity').write_text(json.dumps([os.geteuid(),os.getegid()]))
(root/'cache/org.eclipse.osgi').mkdir(parents=True,exist_ok=True)
if os.environ.get('FIXTURE_CRASH'):raise SystemExit(7)
server=socket.socket()
server.bind(('127.0.0.1',0))
server.listen(1)
server.settimeout(.05)
if not os.environ.get('FIXTURE_NO_PORTFILE'):
    portfile.write_text(os.environ.get('FIXTURE_PORT',str(server.getsockname()[1])))
(root/'openhab-start-level').write_text(os.environ.get('FIXTURE_LEVEL','10')+'\n')
(control/'ready').touch()
while True:
    try:connection,_=server.accept()
    except TimeoutError:continue
    with connection:
        assert connection.recv(1024)==b'fixture-private-command\n'
    event('TCP_STOP')
    if os.environ.get('FIXTURE_JVM_STAYS_AFTER_STOP'):continue
    (root/'openhab-start-level').write_text('0\n')
    (control/'stopping').touch()
    if os.environ.get('FIXTURE_HOLD_EXIT'):
        while not (control/'allow-exit').exists():time.sleep(.01)
    event('EXIT')
    (control/'exited').touch()
    raise SystemExit(0)
'''

FAKE_STOP = FAKE_ENVIRONMENT + r'''
(control/'stop-pid').write_text(str(os.getpid()))
event('STOP_CLIENT')
assert environment==json.loads((control/'prime-environment').read_text())
if os.environ.get('FIXTURE_HUNG_STOP'):
    while True:time.sleep(.01)
if os.environ.get('FIXTURE_STOP_FAIL'):raise SystemExit(7)
with socket.create_connection(('127.0.0.1',int(portfile.read_text())),timeout=1) as connection:
    connection.sendall(b'fixture-private-command\n')
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
        self.custom = self.home / "dist/userdata/etc/custom.properties"
        self.custom.write_bytes(b"# distribution defaults\nkaraf.shutdown.port.file = ${karaf.data}/tmp/port\n")
        (self.home / "dist/userdata/etc/org.apache.karaf.shell.cfg").write_text("sshHost = 0.0.0.0\nsshPort = 8101\n")
        (self.home / "conf/automation/js/actual-house.js").write_text("must not run during cache priming\n")
        (self.root / "fake-jvm.py").write_text(FAKE_JVM)
        karaf = self.home / "runtime/bin/karaf"
        karaf.write_text("#!/bin/sh\nexec " + shlex.join([sys.executable, str(self.root / "fake-jvm.py")]) + "\n")
        karaf.chmod(0o700)
        (self.root / "fake-stop.py").write_text(FAKE_STOP)
        stop = self.home / "runtime/bin/stop"
        stop.write_text("#!/bin/sh\nexec " + shlex.join([sys.executable, str(self.root / "fake-stop.py")]) + "\n")
        stop.chmod(0o700)
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
            try:
                # Failed payloads deliberately leave cleanup to the container
                # runtime. Here only our isolated fixture group is terminated,
                # including children of a shell that has already returned.
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate(timeout=5)

    def prepare(self, *, deny_signals=False, **environment):
        with patch.object(deploy, "NATIVE_PRIME_WAIT", 30), patch.object(deploy, "NATIVE_PRIME_STOP_WAIT", 30), \
                patch.object(deploy, "NATIVE_PRIME_REQUEST_WAIT", 30), \
                patch.object(deploy, "NATIVE_PRIME_POLL", .02):
            command = deploy.openhab_prepare().replace("/openhab/runtime/bin/update", str(self.home / "runtime/bin/update"))
        if deny_signals:
            command = 'kill() { printf "SIGNAL_ATTEMPT\\n" >> "$FIXTURE_CONTROL/events"; return 1; }; ' + command
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
        process = self.prepare(deny_signals=True, FIXTURE_HOLD_EXIT="1")
        self.wait_file(self.control / "stopping", process)
        custom = self.userdata / "etc/custom.properties"
        custom_inode = custom.stat().st_ino
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
        self.assertEqual(custom.read_bytes(), self.custom.read_bytes())
        self.assertEqual(custom.stat().st_ino, custom_inode)
        self.assertIn("temporary JVM has fully exited", out)
        self.assertNotIn("fixture-private-command", out + err)
        started = subprocess.run(["sh", "-ec", deploy.openhab_start()], cwd=self.home, env=self.environment,
                                 capture_output=True, text=True, timeout=5)
        self.assertEqual(started.returncode, 0, started.stderr)
        self.assertEqual((self.control / "events").read_text().splitlines(),
                         ["START", "STOP_CLIENT", "TCP_STOP", "EXIT", "real start"])

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
        self.assertEqual((self.control / "events").read_text().splitlines(), ["START", "STOP_CLIENT", "TCP_STOP", "EXIT"])
        native = subprocess.run(["sh", "-ec", deploy.openhab_start()], cwd=self.home, env=self.environment,
                                capture_output=True, text=True, timeout=5)
        self.assertNotEqual(native.returncode, 0)
        self.assertIn("Incomplete fresh native cache priming", native.stderr)

    def test_successful_stop_request_cannot_count_as_actual_jvm_exit(self):
        started = time.monotonic()
        code, out, err = self.completed(deny_signals=True, FIXTURE_JVM_STAYS_AFTER_STOP="1")
        self.assertLess(time.monotonic() - started, 3)
        self.assertNotEqual(code, 0)
        self.assertIn("JVM did not exit after Karaf shutdown", err)
        self.assertIn("preparation container cleanup", err)
        self.assertTrue((self.userdata / deploy.NATIVE_PRIME_MARKER).exists())
        self.assertTrue((self.userdata / "cache-refresh-lock").exists())
        self.assertFalse((self.control / "exited").exists())
        self.assertTrue(Path(f"/proc/{(self.control / 'pid').read_text()}").is_dir())
        self.assertEqual((self.control / "events").read_text().splitlines(), ["START", "STOP_CLIENT", "TCP_STOP"])

    def test_hung_stop_client_and_denied_signals_return_once_with_retained_state(self):
        started = time.monotonic()
        code, out, err = self.completed(deny_signals=True, FIXTURE_HUNG_STOP="1")
        self.assertLess(time.monotonic() - started, 3)
        self.assertNotEqual(code, 0)
        self.assertIn("shutdown client did not exit within its allowance", err)
        self.assertTrue((self.userdata / deploy.NATIVE_PRIME_MARKER).exists())
        self.assertTrue((self.userdata / "cache-refresh-lock").exists())
        self.assertTrue((self.userdata / "cache/org.eclipse.osgi").is_dir())
        primes = list((self.userdata / "tmp").glob("thermite-prime.*"))
        self.assertEqual(len(primes), 1)
        self.assertTrue((primes[0] / "stop.log").exists())
        self.assertEqual((primes[0] / "custom.properties.before").read_bytes(), self.custom.read_bytes())
        self.assertIn(str(primes[0] / "shutdown.port"), (self.userdata / "etc/custom.properties").read_text())
        self.assertTrue(Path(f"/proc/{(self.control / 'pid').read_text()}").is_dir())
        self.assertTrue(Path(f"/proc/{(self.control / 'stop-pid').read_text()}").is_dir())
        self.assertEqual((self.control / "events").read_text().splitlines(), ["START", "STOP_CLIENT"])

    def test_missing_shutdown_port_never_invokes_stop_or_its_pid_fallback(self):
        code, out, err = self.completed(deny_signals=True, FIXTURE_NO_PORTFILE="1")
        self.assertNotEqual(code, 0)
        self.assertIn("shutdown port file is missing or unreadable", err)
        self.assertFalse((self.control / "stop-pid").exists())
        self.assertTrue((self.userdata / deploy.NATIVE_PRIME_MARKER).exists())
        self.assertEqual((self.control / "events").read_text().splitlines(), ["START"])

    def test_failed_stop_request_returns_without_waiting_for_the_live_jvm(self):
        code, out, err = self.completed(deny_signals=True, FIXTURE_STOP_FAIL="1")
        self.assertNotEqual(code, 0)
        self.assertIn("shutdown request failed", err)
        self.assertIn("preparation container cleanup", err)
        self.assertTrue((self.userdata / deploy.NATIVE_PRIME_MARKER).exists())
        self.assertTrue(Path(f"/proc/{(self.control / 'pid').read_text()}").is_dir())
        self.assertEqual((self.control / "events").read_text().splitlines(), ["START", "STOP_CLIENT"])

    def test_nonpositive_shutdown_port_never_invokes_stop_or_its_pid_fallback(self):
        code, out, err = self.completed(deny_signals=True, FIXTURE_PORT="0")
        self.assertNotEqual(code, 0)
        self.assertIn("refusing the PID fallback", err)
        self.assertFalse((self.control / "stop-pid").exists())
        self.assertTrue((self.userdata / deploy.NATIVE_PRIME_MARKER).exists())
        self.assertEqual((self.control / "events").read_text().splitlines(), ["START"])

    def test_crashed_primer_is_not_accepted_as_a_warmed_cache(self):
        code, out, err = self.completed(FIXTURE_CRASH="1")
        self.assertNotEqual(code, 0)
        self.assertIn("did not reach start level 10", err)
        self.assertTrue((self.userdata / deploy.NATIVE_PRIME_MARKER).exists())

    def test_interruption_waits_for_the_fresh_child_and_retains_the_failure_marker(self):
        process = self.prepare(FIXTURE_LEVEL="5")
        self.wait_file(self.control / "ready", process)
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
