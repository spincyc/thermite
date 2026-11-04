"""Opt-in, isolated openHAB regression for jUPnP commissioning readiness.

Requires THERMITE_JAVA (pinned bin/java), THERMITE_RUNTIME (pristine pinned
distribution directory), and THERMITE_SONOS_JAR if the distribution's Maven
repository lacks the pinned Sonos binding. THERMITE_RUNTIME_ARCHIVE can select
the pinned distribution zip; its default is ../downloads/openhab-VERSION.zip
beside THERMITE_RUNTIME. Linux user/network/PID namespaces
and iproute2 are required. No downloads, installed services, live userdata or
physical devices are used. Only authenticated metrics availability is stubbed;
Thing/add-on/configuration/authentication requests reach the real runtime.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request
import zipfile


AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))
FIXTURE_TIMEOUT = 300
CHILD = "--isolated-native-gateway-fixture"
THING = "sonos:One:fixture"
PENDING = {"UNINITIALIZED", "INITIALIZING", "UNKNOWN"}


def tool(name):
    found = shutil.which(name)
    fallback = Path.home() / ".local/bin" / name
    return found or (str(fallback) if os.access(fallback, os.X_OK) else None)


@unittest.skipUnless(os.environ.get("THERMITE_JAVA") and os.environ.get("THERMITE_RUNTIME"),
                     "native gateway lifecycle needs cached pinned Java/runtime paths")
class NativeGatewayLifecycle(unittest.TestCase):
    def test_commissioned_jupnp_readback_precedes_stable_thing_readiness(self):
        unshare, ip = tool("unshare"), tool("ip")
        if not unshare or not ip or sys.platform != "linux":
            self.skipTest("native gateway lifecycle requires Linux unshare and iproute2")
        isolation = [unshare, "--user", "--map-root-user", "--net", "--pid", "--fork",
                     "--kill-child=KILL", "--mount-proc"]
        available = subprocess.run([*isolation, sys.executable, "-c", "pass"],
                                   capture_output=True, text=True, timeout=10)
        if available.returncode:
            self.skipTest("native gateway lifecycle requires permitted user/network/PID namespaces")
        scratch = AREA / ".scratch/native-repro"
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="gateway-lifecycle-", dir=scratch) as name:
            root = Path(name)
            argv = [*isolation, sys.executable, str(Path(__file__).resolve()), CHILD, str(root),
                    str(Path(os.environ["THERMITE_RUNTIME"]).resolve()),
                    str(Path(os.environ["THERMITE_JAVA"]).resolve()),
                    os.environ.get("THERMITE_SONOS_JAR", ""), ip,
                    str(Path("/proc/self/ns/net").readlink())]
            # Killing the namespace's PID 1 also kills JVMs in separate process
            # groups, even if startup or the normal teardown itself wedges.
            env = {key: value for key, value in os.environ.items()
                   if not key.lower().endswith("_proxy") and key != "PYTHONOPTIMIZE"}
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, start_new_session=True, env=env)
            try:
                output, _ = proc.communicate(timeout=FIXTURE_TIMEOUT)
            except BaseException:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.communicate(timeout=10)
                raise
            self.assertEqual(proc.returncode, 0, output[-12000:])
            report = json.loads((root / "result.json").read_text())
            self.assertTrue(report["config_readback_while_pending"], report)
            self.assertGreaterEqual(report["continuous_ready_seconds"], 10, report)
            self.assertTrue(report["credential_preserved"], report)
            self.assertTrue(report["jvm_unchanged"], report)
            self.assertFalse(report["restart_attempted"], report)
            self.assertTrue(report["teardown_complete"], report)


def isolated_fixture(root, source, java, sonos, ip, host_network):
    import oh_sandbox as sandbox
    from oh_access import native_boundary
    from oh_install.steps import Context, NativeReady
    from oh_install.system import NativeProbe, System

    assert str(Path("/proc/self/ns/net").readlink()) != host_network
    assert os.getpid() == 1, "fixture must own a disposable PID namespace"
    for args in (("link", "set", "lo", "up"), ("link", "add", "fixture0", "type", "dummy"),
                 ("addr", "add", "192.0.2.1/24", "dev", "fixture0"),
                 ("link", "set", "fixture0", "up")):
        subprocess.run([ip, *args], check=True, timeout=5)
    assert not subprocess.check_output([ip, "route", "show", "default"], timeout=5).strip()

    versions = json.loads((AREA / "versions.json").read_text())
    version = versions["openhab"]["version"]
    archive_path = Path(os.environ.get("THERMITE_RUNTIME_ARCHIVE", "")) \
        if os.environ.get("THERMITE_RUNTIME_ARCHIVE") else source.parent / "downloads" / f"openhab-{version}.zip"
    assert archive_path.is_file(), "set THERMITE_RUNTIME_ARCHIVE to the cached pinned distribution zip"
    assert sandbox.sha256_file(archive_path) == versions["openhab"]["distribution"]["sha256"]
    release = (java.parent.parent / "release").read_text()
    runtime_version = next(line.split('"')[1] for line in release.splitlines()
                           if line.startswith("JAVA_RUNTIME_VERSION="))
    assert runtime_version.removesuffix("-LTS") == versions["java"]["version"]
    dist = root / "dist"
    # The pinned archive is authoritative, never env-supplied userdata/etc.
    # Shared caches stay untouched and no live configuration/credentials enter.
    sandbox.extract_zip(archive_path, dist)
    artifact = Path(f"org/openhab/addons/bundles/org.openhab.binding.sonos/{version}/"
                    f"org.openhab.binding.sonos-{version}.jar")
    destination = dist / "runtime/system" / artifact
    if not destination.exists():
        sonos = Path(sonos) if sonos else source / "runtime/system" / artifact
        assert sonos.is_file(), "set THERMITE_SONOS_JAR to the cached public Sonos binding jar"
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(sonos, destination)
    with zipfile.ZipFile(destination) as archive:
        manifest = archive.read("META-INF/MANIFEST.MF").decode().replace("\r\n ", "")
        assert f"Bundle-Version: {version}\r\n" in manifest
        assert "Bundle-SymbolicName: org.openhab.binding.sonos\r\n" in manifest

    conf = root / "conf"
    for name in ("services", "things", "items", "scripts", "rules", "sitemaps", "persistence", "transform"):
        (conf / name).mkdir(parents=True)
    (conf / "services/addons.cfg").write_text("package = minimal\nbinding = sonos\n")
    (conf / "services/runtime.cfg").write_text("org.openhab.network:primaryAddress=192.0.2.1\n")
    (conf / "things/fixture.things").write_text(
        f'Thing {THING} "Fictional Sonos" [ udn="RINCON_02000000001401400", refresh=10 ]\n')
    runtime = sandbox.Runtime(version, dist, java.parent.parent)
    state = root / "state"
    instance = sandbox.Instance("gateway-lifecycle", state, conf, state / "userdata",
                                18600, 18601, 18602, shutdown_port=18603)
    context = Context(root, versions, {"http_port": instance.http_port}, "fixture", "fixture",
                      "fixture", "192.0.2.1")
    (root / "tmp").mkdir()
    (root / "secrets").mkdir()
    original_environment = sandbox.instance_env

    def environment(*args, **kwargs):
        env = original_environment(*args, **kwargs)
        for key in list(env):
            if key in {"JAVA_OPTS", "JAVA_TOOL_OPTIONS", "JDK_JAVA_OPTIONS", "_JAVA_OPTIONS"} \
                    or key.startswith("THERMITE_") or key.lower().endswith("_proxy"):
                env.pop(key)
        env["OPENHAB_SECRETS"] = str(root / "secrets")
        env["EXTRA_JAVA_OPTS"] += f" -Duser.home={root / 'home'} -Djava.io.tmpdir={root / 'tmp'}"
        return env

    report = {"restart_attempted": False, "metrics": "known-healthy fixture"}
    samples = []

    class FixtureSystem(System):
        def native_probe(self, url, token, path, deadline, *, json_body=True):
            if path == NativeReady.metrics_path:
                return NativeProbe(200)
            return super().native_probe(url, token, path, deadline, json_body=json_body)

        def unit(self, *args, **kwargs):
            report["restart_attempted"] = True
            raise AssertionError("this healthy fixture must recover without a service restart")

        def say(self, text):
            print(text, flush=True)

    class RecordingReady(NativeReady):
        def snapshot(self, system):
            found, signature = super().snapshot(system)
            samples.append((time.monotonic(), found.done))
            return found, signature

    token = None

    def api(method, path, body=None, *, authenticated=True):
        headers = {"Accept": "application/json"}
        if authenticated and token:
            headers["Authorization"] = "Bearer " + token
        if body is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(body).encode()
        request = urllib.request.Request(instance.url + path, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                data = response.read()
                return response.status, json.loads(data) if data else None
        except urllib.error.HTTPError as exc:
            code = exc.code
            exc.close()
            return code, None

    with patch.object(sandbox, "instance_env", environment):
        try:
            deadline = time.monotonic() + 180
            pgid = sandbox.start_instance(runtime, instance, deadline=deadline)
            sandbox.wait_rest(instance, pgid, 180, deadline=deadline)
            sandbox.provision_token(runtime, instance, deadline=deadline, log=lambda _: None)
            token = instance.token_file.read_text().strip()
            owner_token = root / context.token_file
            sandbox.write_secret(owner_token, token)
            original_token = owner_token.read_bytes()
            assert not sandbox.wait_addons(sandbox.Http(instance.url, token=token), {"binding-sonos"}, deadline)
            native_boundary.configure(instance.url, owner_token, {"devices": []}, "192.0.2.1", timeout=30)
            while time.monotonic() < deadline:
                code, thing = api("GET", "/rest/things/" + THING)
                if code == 200 and thing["statusInfo"]["status"] == "OFFLINE" \
                        and api("GET", "/rest/items")[0] == 200 \
                        and api("GET", "/rest/items", authenticated=False)[0] in (401, 403):
                    break
                time.sleep(.1)
            else:
                raise AssertionError("fictional Sonos handler never completed initialization")

            # A successful service-config response precedes dependent DS
            # component reattachment; a real Thing remains pending meanwhile.
            code, config = api("GET", native_boundary.UPNP_CONFIG)
            assert code == 200
            config["httpProxyPort"] = "18614"
            assert api("PUT", native_boundary.UPNP_CONFIG, config)[0] in (200, 204)
            observe_until = time.monotonic() + 5
            report["config_readback_while_pending"] = False
            while time.monotonic() < observe_until:
                code, readback = api("GET", native_boundary.UPNP_CONFIG)
                assert code == 200 and readback["httpProxyPort"] == "18614"
                code, thing = api("GET", "/rest/things/" + THING)
                if code == 200 and thing["statusInfo"]["status"] in PENDING:
                    report["config_readback_while_pending"] = True
                    break
                time.sleep(.02)
            assert report["config_readback_while_pending"], thing["statusInfo"]
            report["pending_status"] = thing["statusInfo"]

            ready = RecordingReady(context)
            ready.deadline = time.monotonic() + 60
            assert ready.settle == 10
            assert ready.wait(FixtureSystem()) == 0
            assert not ready.restarted
            last_pending = max((index for index, (_, done) in enumerate(samples) if not done), default=-1)
            consecutive = samples[last_pending + 1:]
            assert len(consecutive) >= 2 and all(done for _, done in consecutive)
            report["continuous_ready_seconds"] = consecutive[-1][0] - consecutive[0][0]
            report["credential_preserved"] = owner_token.read_bytes() == original_token \
                and instance.token_file.read_text().strip() == token \
                and api("GET", "/rest/items")[0] == 200 \
                and api("GET", "/rest/items", authenticated=False)[0] in (401, 403)
            report["jvm_unchanged"] = sandbox.running_pgid(instance) == pgid
        finally:
            sandbox.stop_instance(instance, timeout=10)
            report["teardown_complete"] = sandbox.running_pgid(instance) is None
            (root / "result.json").write_text(json.dumps(report, indent=2) + "\n")
        assert report["teardown_complete"]
    print(json.dumps(report), flush=True)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == CHILD:
        isolated_fixture(Path(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4]), *sys.argv[5:])
    else:
        unittest.main()
