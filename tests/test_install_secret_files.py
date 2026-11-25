"""Finite administrative credential custody, using only the test user's uid/gid."""

from contextlib import contextmanager, redirect_stdout, redirect_stderr
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import fcntl
import hashlib
import importlib.util
import io
import json
import marshal
import os
from pathlib import Path
import shutil
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))
from oh_install import secrets


class SecretFiles(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.state = self.root / "state"
        self.state.mkdir(mode=0o750)
        self.source = self.root / "operator-copy"
        self.source.write_bytes(b"synthetic credential value\n")
        self.source.chmod(0o600)
        self.uid, self.gid = os.geteuid(), os.getegid()
        self.environment = patch.dict(os.environ, {"SUDO_UID": str(os.getuid()), "SUDO_GID": str(os.getgid())})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def install(self, name="openhab-token", **changes):
        values = {"name": name, "source": self.source, "uid": self.uid, "gid": self.gid}
        values.update(changes)
        return secrets.install(self.state, **values)

    def target(self, name="openhab-token"):
        return self.state / "monitoring/secrets" / name

    def test_sync_creates_private_owned_file_and_updates_only_changed_bytes(self):
        result = self.install()
        self.assertTrue(result["changed"])
        target = self.target()
        self.assertEqual(target.read_bytes(), self.source.read_bytes())
        for path, mode in ((target, 0o600), (target.parent, 0o700), (target.parent.parent, 0o750)):
            info = path.stat()
            self.assertEqual((info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)), (self.uid, self.gid, mode))
        inode = target.stat().st_ino
        target.chmod(0o640)
        self.assertFalse(self.install()["changed"])
        self.assertEqual(target.stat().st_ino, inode)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.source.write_bytes(b"a different synthetic credential\n")
        self.assertTrue(self.install()["changed"])
        self.assertEqual(target.read_bytes(), self.source.read_bytes())
        self.assertFalse(any(path.name.startswith(".") for path in target.parent.iterdir()))

    def test_existing_grafana_password_preserves_bytes_inode_and_ignores_source(self):
        name = "grafana-admin-password"
        self.install(name)
        target = self.target(name)
        before = (target.read_bytes(), target.stat().st_ino)
        target.chmod(0o644)
        target.parent.chmod(0o755)
        unavailable = self.root / "missing-owner-copy"
        result = self.install(name, source=unavailable, preserve_existing=True)
        self.assertTrue(result["preserved"])
        self.assertFalse(result["changed"])
        self.assertEqual((target.read_bytes(), target.stat().st_ino), before)
        self.assertFalse(unavailable.exists())
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(target.parent.stat().st_mode), 0o700)
        self.assertTrue(self.install(name, source=None, preserve_existing=True)["preserved"])

    def test_missing_grafana_password_uses_source_once(self):
        result = self.install("grafana-admin-password", preserve_existing=True)
        self.assertTrue(result["changed"])
        self.assertFalse(result["preserved"])

    def test_grafana_generation_happens_only_when_service_target_and_owner_copy_are_missing(self):
        self.source.unlink()
        result = self.install("grafana-admin-password", preserve_existing=True, generate_if_missing=True)
        target = self.target("grafana-admin-password")
        self.assertTrue(result["changed"])
        self.assertFalse(result["preserved"])
        content = target.read_bytes()
        self.assertGreaterEqual(len(content), 32)
        self.assertEqual(self.source.read_bytes(), content)
        info = self.source.stat()
        self.assertEqual((info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode), info.st_nlink), (self.uid, self.gid, 0o600, 1))
        self.source.unlink()
        result = self.install("grafana-admin-password", preserve_existing=True, generate_if_missing=True)
        self.assertTrue(result["preserved"])
        self.assertEqual(target.read_bytes(), content)
        self.assertFalse(self.source.exists(), "an unavailable copy must not be silently replaced with a different password")

    def test_generation_refuses_a_writable_operator_copy_parent(self):
        self.source.unlink()
        self.root.chmod(0o777)
        with self.assertRaises(secrets.SecretError):
            self.install("grafana-admin-password", preserve_existing=True, generate_if_missing=True)
        self.assertFalse(self.source.exists())
        self.assertEqual(list(self.state.iterdir()), [])

    def test_bad_source_is_rejected_before_creating_private_parents(self):
        self.source.chmod(0o644)
        with self.assertRaises(secrets.SecretError):
            self.install()
        self.assertEqual(list(self.state.iterdir()), [])
        self.source.chmod(0o600)
        with patch.dict(os.environ, {"SUDO_UID": str(self.uid + 1)}), self.assertRaises(secrets.SecretError):
            self.install()
        self.assertEqual(list(self.state.iterdir()), [])

    def test_empty_and_oversized_sources_are_refused_without_writes(self):
        for content in (b"", b"x" * (secrets.MAXIMUM + 1)):
            with self.subTest(size=len(content)):
                self.source.write_bytes(content)
                with self.assertRaises(secrets.SecretError):
                    self.install()
                self.assertEqual(list(self.state.iterdir()), [])

    def test_source_links_and_parent_links_are_refused(self):
        link = self.root / "source-link"
        link.symlink_to(self.source)
        with self.assertRaises(OSError):
            self.install(source=link)
        link.unlink()
        os.link(self.source, link)
        with self.assertRaises(secrets.SecretError):
            self.install()
        link.unlink()
        linked_parent = self.root / "linked-parent"
        linked_parent.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(OSError):
            self.install(source=linked_parent / self.source.name)
        self.assertEqual(list(self.state.iterdir()), [])

    def test_state_and_monitoring_symlinks_are_refused_before_writes(self):
        alias = self.root / "state-link"
        alias.symlink_to(self.state, target_is_directory=True)
        with self.assertRaises(OSError):
            secrets.install(alias, name="openhab-token", source=self.source, uid=self.uid, gid=self.gid)
        outside = self.root / "outside"
        outside.mkdir()
        (self.state / "monitoring").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(OSError):
            self.install()
        self.assertEqual(list(outside.iterdir()), [])

    def test_target_symlink_hardlink_and_special_file_are_refused_without_repairs(self):
        private = self.state / "monitoring/secrets"
        private.mkdir(parents=True, mode=0o755)
        private.chmod(0o755)
        target = self.target("grafana-admin-password")
        for kind in ("symlink", "hardlink", "fifo", "directory"):
            with self.subTest(kind=kind):
                if kind == "symlink": target.symlink_to(self.source)
                elif kind == "hardlink": os.link(self.source, target)
                elif kind == "fifo": os.mkfifo(target)
                else: target.mkdir()
                with self.assertRaises((OSError, secrets.SecretError)):
                    self.install("grafana-admin-password", preserve_existing=True, source=None)
                self.assertEqual(stat.S_IMODE(private.stat().st_mode), 0o755)
                if target.is_dir(): target.rmdir()
                else: target.unlink()

    def test_name_escape_and_parent_traversal_are_refused(self):
        for name in ("../escape", "/etc/passwd", "unknown"):
            with self.subTest(name=name), self.assertRaises(secrets.SecretError):
                self.install(name)
        with self.assertRaises(secrets.SecretError):
            secrets.install(self.state / ".." / "state", name="openhab-token", source=self.source, uid=self.uid, gid=self.gid)
        with self.assertRaises(secrets.SecretError):
            self.install(preserve_existing=True)
        self.assertEqual(list(self.state.iterdir()), [])

    def test_cli_reports_disposition_without_credential_content(self):
        output, errors = io.StringIO(), io.StringIO()
        argv = [str(self.state), "--name", "grafana-admin-password", "--source", str(self.source),
                "--uid", str(self.uid), "--gid", str(self.gid), "--preserve-existing"]
        with redirect_stdout(output), redirect_stderr(errors):
            self.assertEqual(secrets.main(argv), 0)
        self.assertTrue(json.loads(output.getvalue())["installed"])
        self.assertNotIn(self.source.read_text().strip(), output.getvalue() + errors.getvalue())
        self.source.unlink()
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            self.assertEqual(secrets.main(argv), 0)
        self.assertTrue(json.loads(output.getvalue())["preserved"])

    def test_standalone_script_installs_without_package_imports(self):
        result = subprocess.run([sys.executable, str(AREA / "lib/oh_install/secrets.py"), str(self.state),
                                 "--name", "openhab-token", "--source", str(self.source),
                                 "--uid", str(self.uid), "--gid", str(self.gid)],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["changed"])
        self.assertNotIn(self.source.read_text().strip(), result.stdout + result.stderr)
        self.assertEqual(self.target().read_bytes(), self.source.read_bytes())

    def prepare_recovery(self):
        self.source.write_bytes(b"oh.synthetic." + b"x" * 64 + b"\n")
        self.install()
        self.operator = self.root / "operator"
        self.operator.mkdir(mode=0o700)
        self.destination = self.operator / "api-token"

    def recover(self, url, **changes):
        arguments = {"destination": self.destination, "uid": self.uid, "gid": self.gid, "url": url}
        arguments.update(changes)
        return secrets.recover_runtime_token(self.state, **arguments)

    @contextmanager
    def runtime(self, *, status=200, body=b"[]"):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):
                requests.append((self.command, self.path, self.headers.get("Authorization")))
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}", requests
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())

    def service_snapshot(self):
        result = {}
        for path in (self.state, *sorted(self.state.rglob("*"))):
            info = path.lstat()
            result[str(path.relative_to(self.state))] = (
                info.st_mode, info.st_uid, info.st_gid, info.st_ino, info.st_mtime_ns,
                path.read_bytes() if path.is_file() else None,
            )
        return result

    def test_recovery_validates_service_token_and_restores_only_private_operator_copy(self):
        self.prepare_recovery()
        before = self.service_snapshot()
        with self.runtime() as (url, requests):
            result = self.recover(url)
        self.assertEqual(result, {"recovered": True, "name": "openhab-token"})
        self.assertEqual(self.destination.read_bytes(), self.target().read_bytes())
        info = self.destination.stat()
        self.assertEqual((info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode), info.st_nlink),
                         (self.uid, self.gid, 0o600, 1))
        self.assertEqual(self.service_snapshot(), before)
        self.assertEqual(requests, [("GET", "/rest/auth/apitokens",
                                     "Bearer " + self.source.read_text().strip())])
        self.assertEqual({path.name for path in self.operator.iterdir()}, {"api-token", ".runtime-access.lock"})

    def test_recovery_rejects_stale_token_without_publishing_or_changing_service(self):
        self.prepare_recovery()
        before = self.service_snapshot()
        with self.runtime(status=401, body=self.source.read_bytes()) as (url, requests):
            with self.assertRaises(secrets.SecretError):
                self.recover(url)
        self.assertEqual(len(requests), 1)
        self.assertFalse(self.destination.exists())
        self.assertEqual(self.service_snapshot(), before)
        self.assertEqual({path.name for path in self.operator.iterdir()}, {".runtime-access.lock"})

    def test_recovery_refuses_existing_token_or_managed_record_before_validation(self):
        self.prepare_recovery()
        for name in ("api-token", "runtime-credentials.json"):
            for kind in ("file", "dangling_symlink", "directory"):
                with self.subTest(name=name, kind=kind):
                    existing = self.operator / name
                    if kind == "file":
                        existing.write_bytes(b"existing private record")
                        existing.chmod(0o600)
                    elif kind == "dangling_symlink":
                        existing.symlink_to(self.root / "absent")
                    else:
                        existing.mkdir()
                    before = existing.lstat()
                    with patch.object(secrets, "_validate_runtime_token") as validate, self.assertRaises(secrets.SecretError):
                        self.recover("http://127.0.0.1:13602")
                    validate.assert_not_called()
                    self.assertEqual(existing.lstat().st_ino, before.st_ino)
                    if kind == "directory":
                        existing.rmdir()
                    else:
                        existing.unlink()

    def test_recovery_rejects_service_token_links_and_wrong_custody_without_repairs(self):
        self.prepare_recovery()
        target = self.target()
        contents = target.read_bytes()
        for kind in ("symlink", "hardlink", "fifo", "mode", "uid", "gid", "directory_mode"):
            with self.subTest(kind=kind):
                target.unlink()
                if kind == "symlink":
                    target.symlink_to(self.source)
                elif kind == "hardlink":
                    os.link(self.source, target)
                elif kind == "fifo":
                    os.mkfifo(target)
                else:
                    target.write_bytes(contents)
                    target.chmod(0o644 if kind == "mode" else 0o600)
                if kind == "directory_mode":
                    target.parent.chmod(0o755)
                uid = self.uid + 1 if kind == "uid" else self.uid
                gid = self.gid + 1 if kind == "gid" else self.gid
                with patch.object(secrets, "_validate_runtime_token") as validate, \
                        self.assertRaises((OSError, secrets.SecretError)):
                    self.recover("http://127.0.0.1:13602", uid=uid, gid=gid)
                validate.assert_not_called()
                self.assertFalse(self.destination.exists())
                self.assertEqual(stat.S_IMODE(target.parent.stat().st_mode), 0o755 if kind == "directory_mode" else 0o700)
                target.parent.chmod(0o700)

    def test_recovery_rejects_linked_ancestors_and_unsafe_operator_directory(self):
        self.prepare_recovery()
        alias = self.root / "state-alias"
        alias.symlink_to(self.state, target_is_directory=True)
        with patch.object(secrets, "_validate_runtime_token") as validate:
            with self.assertRaises(OSError):
                secrets.recover_runtime_token(alias, destination=self.destination, uid=self.uid, gid=self.gid,
                                              url="http://127.0.0.1:13602")
            operator_alias = self.root / "operator-alias"
            operator_alias.symlink_to(self.operator, target_is_directory=True)
            with self.assertRaises(OSError):
                self.recover("http://127.0.0.1:13602", destination=operator_alias / "api-token")
            self.operator.chmod(0o750)
            with self.assertRaises(secrets.SecretError):
                self.recover("http://127.0.0.1:13602")
            self.assertEqual(stat.S_IMODE(self.operator.stat().st_mode), 0o750)
            validate.assert_not_called()
        self.assertFalse(self.destination.exists())

    def test_recovery_refuses_service_token_replaced_during_validation(self):
        self.prepare_recovery()

        def replace_service_token(url, content):
            replacement = self.target().with_name("replacement")
            replacement.write_bytes(b"different synthetic credential")
            replacement.chmod(0o600)
            replacement.replace(self.target())

        with patch.object(secrets, "_validate_runtime_token", side_effect=replace_service_token), \
                self.assertRaises(secrets.SecretError):
            self.recover("http://127.0.0.1:13602")
        self.assertFalse(self.destination.exists())

    def test_recovery_refuses_lock_links_and_concurrent_setup(self):
        self.prepare_recovery()
        lock = self.operator / ".runtime-access.lock"
        for kind in ("symlink", "hardlink", "fifo", "mode", "busy"):
            with self.subTest(kind=kind):
                if kind == "symlink":
                    lock.symlink_to(self.source)
                elif kind == "hardlink":
                    os.link(self.source, lock)
                elif kind == "fifo":
                    os.mkfifo(lock)
                else:
                    lock.touch(mode=0o600)
                    lock.chmod(0o644 if kind == "mode" else 0o600)
                held = None
                try:
                    if kind == "busy":
                        held = os.open(lock, os.O_RDWR)
                        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    with patch.object(secrets, "_validate_runtime_token") as validate, \
                            self.assertRaises((OSError, secrets.SecretError)):
                        self.recover("http://127.0.0.1:13602")
                    validate.assert_not_called()
                    self.assertFalse(self.destination.exists())
                finally:
                    if held is not None:
                        os.close(held)
                    lock.unlink()

    def test_runtime_validator_restricts_origin_before_sending_any_credential(self):
        origins = (None, "http://localhost:13602", "http://192.0.2.1:13602", "https://127.0.0.1:13602",
                   "http://127.0.0.1:13602/path", "http://127.0.0.1:13602?query", "http://127.0.0.1:13602#fragment",
                   "http://user@127.0.0.1:13602", "http://127.0.0.1:65536", "http://127.0.0.1:0")
        for origin in origins:
            with self.subTest(origin=origin), patch("http.client.HTTPConnection") as connection, \
                    self.assertRaises(secrets.SecretError):
                secrets._validate_runtime_token(origin, b"synthetic-private-token")
            connection.assert_not_called()

    def test_runtime_validator_refuses_malformed_tokens_before_network_access(self):
        for content in (b"", b"\xff", b"oh.synthetic.unsafe\r\nInjected: header", b"two tokens", b"null\x00"):
            with self.subTest(content=content), patch("http.client.HTTPConnection") as connection, \
                    self.assertRaises(secrets.SecretError):
                secrets._validate_runtime_token("http://127.0.0.1:13602", content)
            connection.assert_not_called()

    def test_runtime_validator_refuses_redirect_and_invalid_or_oversized_json(self):
        for status, body in ((302, b"[]"), (500, b"[]"), (200, b"{}"), (200, b"not json"),
                             (200, b"[" * 1100 + b"]" * 1100), (200, b'[{"name":"duplicate"},{"name":"duplicate"}]'),
                             (200, b"[\"" + b"x" * (1024 * 1024) + b"\"]")):
            with self.subTest(status=status, size=len(body)), self.runtime(status=status, body=body) as (url, requests):
                with self.assertRaises(secrets.SecretError):
                    secrets._validate_runtime_token(url, b"synthetic-private-token")
                self.assertEqual(len(requests), 1)

    def test_recovery_cli_never_prints_credentials_on_success_or_refusal(self):
        self.prepare_recovery()
        for status in (401, 200):
            with self.subTest(status=status), self.runtime(status=status, body=b"[]") as (url, _):
                output, errors = io.StringIO(), io.StringIO()
                argv = [str(self.state), "--name", "openhab-token", "--source", str(self.destination),
                        "--uid", str(self.uid), "--gid", str(self.gid), "--recover-runtime-token", "--runtime-url", url]
                with redirect_stdout(output), redirect_stderr(errors):
                    result = secrets.main(argv)
                self.assertEqual(result, 0 if status == 200 else 2)
                self.assertNotIn(self.source.read_text().strip(), output.getvalue() + errors.getvalue())
                if status == 200:
                    self.assertEqual(json.loads(output.getvalue()), {"recovered": True, "name": "openhab-token"})
                else:
                    self.assertEqual(output.getvalue(), "")
                    self.assertIn("operation refused", errors.getvalue())

    def test_standalone_recovery_validates_without_application_package_imports(self):
        self.prepare_recovery()
        with self.runtime() as (url, requests):
            result = subprocess.run([sys.executable, "-B", str(AREA / "lib/oh_install/secrets.py"), str(self.state),
                                     "--name", "openhab-token", "--source", str(self.destination),
                                     "--uid", str(self.uid), "--gid", str(self.gid), "--recover-runtime-token",
                                     "--runtime-url", url], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["recovered"])
        self.assertEqual(len(requests), 1)
        self.assertEqual(self.destination.read_bytes(), self.source.read_bytes())
        self.assertNotIn(self.source.read_text().strip(), result.stdout + result.stderr)

    def test_standalone_recovery_ignores_forged_adjacent_bytecode(self):
        self.prepare_recovery()
        program = self.root / "helper"
        program.mkdir()
        for name in ("secrets.py", "native_auth.py"):
            (program / name).write_bytes((AREA / "lib/oh_install" / name).read_bytes())
        marker = self.root / "unverified-bytecode-executed"
        for name in ("secrets.py", "native_auth.py"):
            source = program / name
            cache = program / "__pycache__" / f"{source.stem}.{sys.implementation.cache_tag}.pyc"
            cache.parent.mkdir(exist_ok=True)
            code = compile(f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\n"
                           "raise RuntimeError('unverified bytecode executed')\n", str(source), "exec")
            info = source.stat()
            cache.write_bytes(importlib.util.MAGIC_NUMBER + struct.pack("<III", 0, int(info.st_mtime) & 0xffffffff,
                              info.st_size & 0xffffffff) + marshal.dumps(code))
        poisoned = self.root / "poisoned-pythonpath"
        poisoned.mkdir()
        (poisoned / "native_auth.py").write_text(
            f"from pathlib import Path\nPath({str(marker)!r}).write_text('pythonpath')\nraise RuntimeError('pythonpath')\n")
        environment = {key: value for key, value in os.environ.items() if key != "PYTHONPYCACHEPREFIX"}
        # Prove the forged timestamp/size cache is executable even with -B.
        control = subprocess.run([sys.executable, "-B", "-c", "import native_auth"], cwd=program,
                                 env=environment, capture_output=True, text=True, timeout=5)
        self.assertNotEqual(control.returncode, 0)
        self.assertEqual(marker.read_text(), "executed")
        marker.unlink()
        before = {str(path.relative_to(program)): path.read_bytes() for path in program.rglob("*") if path.is_file()}
        for flags, extra_env in ((["-I", "-B"], {"PYTHONPATH": str(poisoned)}), (["-B"], {})):
            with self.subTest(flags=flags), self.runtime() as (url, requests):
                if self.destination.exists():
                    self.destination.unlink()
                result = subprocess.run([sys.executable, *flags, str(program / "secrets.py"), str(self.state),
                                         "--name", "openhab-token", "--source", str(self.destination),
                                         "--uid", str(self.uid), "--gid", str(self.gid), "--recover-runtime-token",
                                         "--runtime-url", url], cwd=program, env={**environment, **extra_env},
                                        capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertTrue(json.loads(result.stdout)["recovered"])
                self.assertEqual(len(requests), 1)
                self.assertFalse(marker.exists(), "recovery must execute verified source, never adjacent bytecode")
                self.assertEqual(self.destination.read_bytes(), self.source.read_bytes())
                after = {str(path.relative_to(program)): path.read_bytes() for path in program.rglob("*") if path.is_file()}
                self.assertEqual(before, after, "standalone recovery must leave its program and caches unchanged")
                self.assertNotIn(self.source.read_text().strip(), result.stdout + result.stderr)


class RootHelperBytecodeIsolation(unittest.TestCase):
    """Every root-invoked standalone Python helper runs cleanly under
    `python3 -I -B`, ignores forged adjacent __pycache__/*.pyc caches (both
    for itself and imported siblings), ignores PYTHONPATH, and writes no .pyc."""

    LOG4J2 = (
        b'<?xml version="1.0" encoding="UTF-8"?>\n'
        b'<Configuration>\n'
        b'  <Appenders>\n'
        b'    <RollingFile name="LOGFILE" fileName="openhab.log" filePattern="openhab.log.%i">\n'
        b'      <PatternLayout pattern="%m%n"/>\n'
        b'    </RollingFile>\n'
        b'  </Appenders>\n'
        b'  <Loggers>\n'
        b'    <Root level="INFO"><AppenderRef ref="LOGFILE"/></Root>\n'
        b'  </Loggers>\n'
        b'</Configuration>\n'
    )

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.uid, self.gid = os.geteuid(), os.getegid()
        self.marker = self.root / "forged-bytecode-executed"
        self.poisoned = self.root / "poisoned-pythonpath"
        self.poisoned.mkdir()
        for name in ("oh_ports", "oh_logging", "oh_androidtv_patch", "oh_heos_patch", "json"):
            (self.poisoned / f"{name}.py").write_text(
                f"from pathlib import Path\nPath({str(self.marker)!r}).write_text('pythonpath:{name}')\n"
                f"raise RuntimeError('pythonpath:{name}')\n")
        self.env = {key: value for key, value in os.environ.items() if key != "PYTHONPYCACHEPREFIX"}
        self.env["PYTHONPATH"] = str(self.poisoned)

    def forge_pyc(self, source: Path) -> Path:
        cache = source.parent / "__pycache__" / f"{source.stem}.{sys.implementation.cache_tag}.pyc"
        cache.parent.mkdir(exist_ok=True)
        code = compile(f"from pathlib import Path\nPath({str(self.marker)!r}).write_text({source.name!r})\n"
                       "raise RuntimeError('unverified bytecode executed')\n", str(source), "exec")
        info = source.stat()
        cache.write_bytes(importlib.util.MAGIC_NUMBER + struct.pack("<III", 0, int(info.st_mtime) & 0xffffffff,
                          info.st_size & 0xffffffff) + marshal.dumps(code))
        return cache

    def tree(self, root: Path) -> dict[str, bytes]:
        return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}

    def unused_port(self) -> int:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]

    def copy_program(self, *names: str) -> Path:
        program = self.root / ("program-" + names[0].removesuffix(".py"))
        program.mkdir()
        for name in names:
            source = AREA / "lib" / name
            target = program / name
            if source.is_dir():
                shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            else:
                target.write_bytes(source.read_bytes())
        for py in program.rglob("*.py"):
            self.forge_pyc(py)
        return program

    def test_oh_logging_runs_under_I_B_and_ignores_forged_bytecode(self):
        import oh_logging
        program = self.copy_program("oh_logging.py", "oh_ports.py")
        # Control: forged oh_ports .pyc executes under ordinary `python3 -B`.
        control = subprocess.run([sys.executable, "-B", "-c", "import oh_ports"], cwd=program,
                                 env={k: v for k, v in self.env.items() if k != "PYTHONPATH"},
                                 capture_output=True, text=True, timeout=5)
        self.assertNotEqual(control.returncode, 0)
        self.assertEqual(self.marker.read_text(), "oh_ports.py")
        self.marker.unlink()

        userdata = self.root / "logging-userdata"
        (userdata / "etc").mkdir(parents=True)
        (userdata / "etc/log4j2.xml").write_bytes(self.LOG4J2)
        (userdata / "etc/org.ops4j.pax.logging.cfg").write_bytes(oh_logging.DEFAULT_SELECTOR)
        before = self.tree(program)
        result = subprocess.run([sys.executable, "-I", "-B", str(program / "oh_logging.py"), str(userdata),
                                 "--http-port", str(self.unused_port())],
                                cwd=program, env=self.env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(oh_logging.configured((userdata / "etc/log4j2.xml").read_bytes()))
        self.assertFalse(self.marker.exists())
        self.assertEqual(self.tree(program), before)

    def test_oh_addons_runs_under_I_B_and_ignores_forged_bytecode(self):
        import oh_addons
        program = self.copy_program("oh_addons.py", "oh_ports.py", "oh_androidtv_patch", "oh_heos_patch")
        userdata = self.root / "addons-userdata"
        (userdata / "etc").mkdir(parents=True)
        (userdata / "etc" / oh_addons.MAVEN_CFG).write_text(
            "org.ops4j.pax.url.mvn.defaultRepositories=file:/system@id=system.repository\n")
        rel = "org/example/bundles/binding.http/1.0/binding.http-1.0.jar"
        payload = b"fixture http binding bytes"
        store = self.root / "addons-store"
        (store / rel).parent.mkdir(parents=True)
        (store / rel).write_bytes(payload)
        versions = self.root / "versions.json"
        versions.write_text(json.dumps({
            "openhab": {"version": "5.2.3"},
            "addons": {
                "repository": "https://repository.example/release",
                "requires": {"binding-http": [rel]},
                "files": {rel: {"sha256": hashlib.sha256(payload).hexdigest(), "size": len(payload)}},
            },
        }))
        before = self.tree(program)
        result = subprocess.run([sys.executable, "-I", "-B", str(program / "oh_addons.py"), str(userdata),
                                 "--versions", str(versions), "--store", str(store),
                                 "--uid", str(self.uid), "--gid", str(self.gid), "binding-http"],
                                cwd=program, env=self.env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((userdata / oh_addons.REPOSITORY / rel).read_bytes(), payload)
        self.assertFalse(self.marker.exists())
        self.assertEqual(self.tree(program), before)

    def test_oh_service_runs_under_I_B_and_ignores_forged_bytecode(self):
        import oh_logging
        program = self.copy_program("oh_service.py", "oh_logging.py", "oh_ports.py")
        state = self.root / "service-state"
        (state / "userdata/etc").mkdir(parents=True)
        (state / "userdata/etc/log4j2.xml").write_bytes(oh_logging.guarded(self.LOG4J2))
        (state / "userdata/etc/org.ops4j.pax.logging.cfg").write_bytes(oh_logging.DEFAULT_SELECTOR)
        before = self.tree(program)
        for extra in (["--check-pairing-logging"], ["--check"]):
            with self.subTest(extra=extra):
                result = subprocess.run([sys.executable, "-I", "-B", str(program / "oh_service.py"), str(state),
                                         "--uid", str(self.uid), "--gid", str(self.gid), *extra],
                                        cwd=program, env=self.env, capture_output=True, text=True, timeout=10)
                if extra == ["--check-pairing-logging"]:
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                else:
                    self.assertIn(result.returncode, (0, 1), result.stdout + result.stderr)
                self.assertFalse(self.marker.exists())
                self.assertEqual(self.tree(program), before)

    def test_oh_ports_runs_under_I_B_and_ignores_forged_bytecode(self):
        import oh_ports
        program = self.copy_program("oh_ports.py")
        userdata = self.root / "ports-userdata"
        (userdata / "etc").mkdir(parents=True)
        (userdata / "etc/org.apache.karaf.shell.cfg").write_text("sshPort = 8101\nsshHost = 0.0.0.0\n")
        (userdata / "etc/custom.properties").write_text("# fixture\n")
        before = self.tree(program)
        result = subprocess.run([sys.executable, "-I", "-B", str(program / "oh_ports.py"), str(userdata),
                                 "--http-port", str(self.unused_port()),
                                 "--uid", str(self.uid), "--gid", str(self.gid)],
                                cwd=program, env=self.env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(oh_ports.native_configured_bytes((userdata / "etc/org.apache.karaf.shell.cfg").read_bytes()))
        self.assertTrue(oh_ports.shutdown_configured_bytes((userdata / "etc/custom.properties").read_bytes()))
        self.assertFalse(self.marker.exists())
        self.assertEqual(self.tree(program), before)

    def test_oh_apparmor_runs_under_I_B_and_ignores_forged_bytecode(self):
        program = self.copy_program("oh_apparmor.py", "thermite-node-exporter.apparmor",
                                    "thermite-node-exporter.apparmor.LICENSE")
        before = self.tree(program)
        result = subprocess.run([sys.executable, "-I", "-B", str(program / "oh_apparmor.py"),
                                 "--verify", "/tmp/not-a-parser"],
                                cwd=program, env=self.env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("node confinement:", result.stderr)
        self.assertFalse(self.marker.exists())
        self.assertEqual(self.tree(program), before)

    def test_secrets_and_apparmor_preserve_safe_diagnostics_without_leaking_secrets(self):
        import io
        from contextlib import redirect_stderr
        import oh_apparmor
        from oh_install import secrets as install_secrets

        err = io.StringIO()
        with redirect_stderr(err):
            code = install_secrets.main(["--share", "photos", "--uid", "9001", "--source", "/tmp/unused"])
        self.assertEqual(code, 2)
        self.assertIn("operation refused", err.getvalue())
        self.assertIn("invalid share credential arguments", err.getvalue())

        err2 = io.StringIO()
        missing = self.root / "missing-apparmor"
        with patch.object(oh_apparmor.os, "geteuid", return_value=0), \
                patch.object(oh_apparmor, "MODULE_ENABLED", missing), \
                redirect_stderr(err2):
            code2 = oh_apparmor.main(["--verify", oh_apparmor.PARSERS[0]])
        self.assertEqual(code2, 2)
        self.assertIn("operation refused", err2.getvalue())
        self.assertIn("verification requires an enabled AppArmor kernel", err2.getvalue())


if __name__ == "__main__":
    unittest.main()
