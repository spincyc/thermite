"""Pinned native entry routing and its script-owned Java lifecycle."""

import contextlib
import http.client
import io
import json
import os
from pathlib import Path
import shutil
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
import zipfile

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))
import oh_entry
import oh_routes

SCRATCH = Path(os.environ.get("TMPDIR", AREA.parents[1] / ".scratch/thermite_clean_routes"))


class Packaging(unittest.TestCase):
    def test_exact_artifact_and_source_pins_and_generated_activation_order(self):
        artifact = oh_entry.checked_artifact()
        files = oh_entry.files()
        self.assertEqual(sorted(files), [oh_entry.JAR_PATH, oh_entry.SCRIPT_PATH])
        self.assertEqual(files[oh_entry.JAR_PATH], artifact)
        self.assertIn(oh_entry.sha(artifact), files[oh_entry.SCRIPT_PATH])
        self.assertNotIn("@JAR_", files[oh_entry.SCRIPT_PATH])
        self.assertNotIn("@ROUTES_", files[oh_entry.SCRIPT_PATH])
        self.assertNotIn("@PATHS_", files[oh_entry.SCRIPT_PATH])
        self.assertNotIn("/settings/", oh_routes.ROUTES)
        self.assertEqual(oh_entry.approved_artifacts(), {"lib/oh_entry/filter.jar": oh_entry.sha(artifact)})
        with zipfile.ZipFile(io.BytesIO(artifact)) as archive:
            self.assertEqual(archive.namelist(), ["META-INF/MANIFEST.MF", oh_entry.CLASS])
            self.assertTrue(all(info.date_time == (1980, 1, 1, 0, 0, 0) for info in archive.infolist()))
            self.assertEqual(oh_entry.package(archive.read(oh_entry.CLASS)), artifact)

    def test_source_or_artifact_drift_is_refused_before_generation(self):
        for key in ("source_sha256", "artifact_sha256"):
            lock = oh_entry.pins()
            lock[key] = "0" * 64
            with self.subTest(key=key), mock.patch.object(oh_entry, "pins", return_value=lock):
                with self.assertRaisesRegex(ValueError, "reviewed pin"):
                    oh_entry.files()

    @unittest.skipUnless(shutil.which("node"), "Node is required for script lifecycle stand-ins")
    def test_loader_registers_java_service_and_retires_it_on_unload_and_errors(self):
        # No HTTP callback is implemented by this harness or by the actual JS loader.
        script = oh_entry.files()[oh_entry.SCRIPT_PATH]
        harness = r'''
const vm = require('vm');
const script = JSON.parse(process.argv[1]);
function run(mode) {
  const actions = []; let hook; let properties;
  const jar = {toUri:()=>({toURL:()=>({})})};
  class Loader {
    constructor() { actions.push('open'); }
    loadClass(name) {
      if (name !== 'org.thermite.entry.EntryFilter') throw Error('wrong class');
      return {getDeclaredConstructor:()=>({newInstance:pages=>{
        if (JSON.stringify(pages.values)!==process.argv[4]) throw Error('wrong route catalogue');
        return {pureJava:true};
      }})};
    }
    close() { actions.push('close'); }
  }
  class Table { constructor() { this.values = {}; } put(k,v) { this.values[k] = v; } }
  const types = {
    'java.nio.file.Files':{readAllBytes:()=>[]},
    'java.nio.file.Path':{of:()=>({resolve:()=>jar})},
    'java.lang.System':{getProperty:()=>'/private/conf'},
    'java.security.MessageDigest':{getInstance:()=>({digest:()=>[]})},
    'java.util.HexFormat':{of:()=>({formatHex:()=>mode==='tampered'?'bad':process.argv[2]})},
    'java.net.URLClassLoader':Loader,
    'javax.servlet.Filter':{class:{getClassLoader:()=>({})}},
    'java.util.Hashtable':Table,
    'java.util.HashMap':Table,
    'java.util.Map':{class:{}},
    'java.lang.Integer':{valueOf:x=>x}
  };
  const context = {
    Java:{type:name=>types[name],to:value=>value},
    require:name=>name==='@runtime/osgi'?{bundleContext:{registerService:(name,service,p)=>{
      properties=p.values;
      if (name !== 'javax.servlet.Filter' || !service.pureJava) throw Error('wrong service');
      if (mode==='registerFail') throw Error('registration failed');
      actions.push('register');return {unregister:()=>actions.push('unregister')};
    }}}:{lifecycleTracker:{addDisposeHook:fn=>{
      if (mode==='hookFail') throw Error('hook failed'); hook=fn;
    }}}
  };
  let failed=false;
  try { vm.runInNewContext(script,context); } catch (_) { failed=true; }
  if (mode==='ok') {
    if (failed || JSON.stringify(actions)!=='["open","register"]') throw Error('failed to register');
    if (properties['osgi.http.whiteboard.context.select']!=='(osgi.http.whiteboard.context.name=default)' ||
        properties['osgi.http.whiteboard.filter.dispatcher']!=='REQUEST' ||
        properties['osgi.http.whiteboard.filter.asyncSupported']!==true ||
        JSON.stringify(properties['osgi.http.whiteboard.filter.pattern'])!==process.argv[3]) throw Error('wrong mapping');
    hook();
    if (JSON.stringify(actions)!=='["open","register","unregister","close"]') throw Error('leaked lifecycle');
  } else {
    if (!failed) throw Error('accepted failure');
    const expected={tampered:[],registerFail:['open','close'],hookFail:['open','register','unregister','close']}[mode];
    if (JSON.stringify(actions)!==JSON.stringify(expected)) throw Error('leaked failure: '+actions);
  }
}
for (const mode of ['ok','tampered','registerFail','hookFail']) run(mode);
'''
        result = subprocess.run([shutil.which("node"), "-e", harness, json.dumps(script),
                                 oh_entry.sha(oh_entry.checked_artifact()),
                                 json.dumps([*oh_routes.ROUTES, *oh_routes.ALIASES], separators=(",", ":")),
                                 json.dumps(oh_routes.ROUTES, separators=(",", ":"))],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class Readiness(unittest.TestCase):
    @contextlib.contextmanager
    def server(self, respond):
        stopping = threading.Event()
        requests = []

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                self.connection.settimeout(2)
                try:
                    method, path, _ = self.rfile.readline().decode().strip().split()
                    headers = {}
                    while True:
                        line = self.rfile.readline()
                        if line in (b"\r\n", b"\n", b""):
                            break
                        name, value = line.decode().split(":", 1)
                        headers[name.lower()] = value.strip()
                    requests.append((method, path, headers))
                    respond(path, self.connection, stopping)
                except OSError:
                    # Deliberate unfinished/late responses are closed by the client.
                    pass

        server = socketserver.TCPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01})
        worker.start()
        try:
            yield f"http://127.0.0.1:{server.server_address[1]}", requests
        finally:
            stopping.set()
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)
            self.assertFalse(worker.is_alive(), "entry HTTP fixture did not stop")

    @staticmethod
    def reply(connection, *, status=302, location="/", page=None, cache="no-store", extra=""):
        head = f"HTTP/1.1 {status} Fixture\r\nContent-Length: 999999\r\nConnection: close\r\n"
        if location is not None:
            head += f"Location: {location}\r\n"
        if cache is not None:
            head += f"Cache-Control: {cache}\r\n"
        if page is not None:
            head += f"X-Thermite-Page: {page}\r\n"
        connection.sendall((head + extra + "\r\n").encode())
        # No body: the readiness contract is headers only.

    def successful(self, path, connection):
        self.reply(connection, **({"status": 200, "location": None, "page": "home"} if path == "/" else {}))

    def test_real_redirects_are_checked_directly_without_credentials_or_body_reads(self):
        with self.server(lambda path, connection, stopping: self.successful(path, connection)) as (origin, requests), \
                mock.patch("oh_http.urllib.request.getproxies", side_effect=AssertionError("must not use proxies")), \
                mock.patch.object(http.client.HTTPResponse, "read", side_effect=AssertionError("must not read bodies")):
            self.assertTrue(oh_entry.ready(origin + "/", timeout=1))
            self.assertEqual([(method, path) for method, path, _ in requests], [("HEAD", "/"), ("HEAD", "/overview/")])
            for _, _, headers in requests:
                self.assertFalse(set(headers) & {"authorization", "proxy-authorization", "cookie"})

    def test_missing_wrong_or_ambiguous_redirects_and_bad_headers_are_not_ready(self):
        cases = [{"status": 200}, {"status": 404}, {"location": None}, {"location": "/settings/"},
                 {"location": "//example.invalid/static/remote/home.html"},
                 {"location": "https://example.invalid/static/remote/home.html"},
                 {"cache": None}, {"cache": "max-age=60"},
                 {"extra": "Location: /settings/\r\n"}, {"extra": "Cache-Control: max-age=60\r\n"}]
        for changes in cases:
            def respond(path, connection, stopping):
                if path == "/overview/":
                    self.reply(connection, **changes)
                else:
                    self.successful(path, connection)
            with self.subTest(changes=changes), self.server(respond) as (origin, requests):
                self.assertFalse(oh_entry.ready(origin, timeout=1))
                self.assertEqual([path for _, path, _ in requests], ["/", "/overview/"])

    def test_native_mainui_or_missing_asset_cannot_impersonate_the_home_route(self):
        for changes in ({"status": 200, "location": None},
                        {"status": 200, "location": None, "page": "index"},
                        {"status": 503, "location": None},
                        {"status": 200, "location": None, "page": "home", "extra": "X-Thermite-Page: other\r\n"}):
            with self.subTest(changes=changes), self.server(
                    lambda path, connection, stopping: self.reply(connection, **changes)) as (origin, requests):
                self.assertFalse(oh_entry.ready(origin, timeout=1))
                self.assertEqual([path for _, path, _ in requests], ["/"])

    def test_redirect_is_never_followed_even_to_another_loopback_server(self):
        with self.server(lambda path, connection, stopping: self.reply(connection)) as (target, target_requests):
            with self.server(lambda path, connection, stopping: self.reply(
                    connection, location=target + "/static/remote/home.html")) as (origin, requests):
                self.assertFalse(oh_entry.ready(origin, timeout=1))
                self.assertEqual(len(requests), 1)
                self.assertEqual(target_requests, [])

    def test_trickled_headers_obey_absolute_deadline_and_malformed_http_fails_closed(self):
        def trickle(path, connection, stopping):
            connection.sendall(b"HTTP/1.1 302 Found\r\nLocation: /static/remote/home.html\r\nX-Slow: ")
            for _ in range(100):
                connection.sendall(b" ")
                if stopping.wait(.025):
                    return

        with self.server(trickle) as (origin, requests):
            started = time.monotonic()
            self.assertFalse(oh_entry.ready(origin, timeout=.15))
            self.assertLess(time.monotonic() - started, .8)
            self.assertEqual(len(requests), 1)
        for response in (b"broken status\r\n\r\n", b""):
            with self.subTest(response=response), self.server(
                    lambda path, connection, stopping: connection.sendall(response)) as (origin, _):
                self.assertFalse(oh_entry.ready(origin, timeout=1))

    def test_both_routes_share_one_budget_and_late_success_cannot_pass(self):
        def delayed(path, connection, stopping):
            if not stopping.wait(.11):
                self.successful(path, connection)
        with self.server(delayed) as (origin, requests):
            started = time.monotonic()
            self.assertFalse(oh_entry.ready(origin, timeout=.18))
            self.assertLess(time.monotonic() - started, .8)
            self.assertEqual([path for _, path, _ in requests], ["/", "/overview/"])

    def test_only_literal_loopback_origins_are_accepted_and_programmer_errors_are_visible(self):
        invalid = ["http://localhost:1234", "http://192.0.2.1:1234", "https://127.0.0.1:1234",
                   "http://user@127.0.0.1:1234", "http://127.0.0.1:1234/path", "http://127.0.0.1:1234?x=1",
                   "http://127.0.0.1:1234#x", "http://127.0.0.1:0", "http://127.0.0.1:65536", " http://127.0.0.1"]
        with mock.patch("oh_entry.oh_http.headers") as headers:
            for origin in invalid:
                with self.subTest(origin=origin):
                    self.assertFalse(oh_entry.ready(origin, timeout=1))
            headers.assert_not_called()
            for timeout in (0, -1, float("inf"), float("nan"), True):
                with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                    oh_entry.ready("http://127.0.0.1", timeout)
            with self.assertRaises(TypeError):
                oh_entry.ready(None, 1)
        with mock.patch("oh_entry.oh_http.headers", side_effect=RuntimeError("programming mistake")):
            with self.assertRaisesRegex(RuntimeError, "programming mistake"):
                oh_entry.ready("http://127.0.0.1", 1)


@unittest.skipUnless(all(os.environ.get(name) for name in ("THERMITE_JAVA", "THERMITE_ECJ", "THERMITE_RUNTIME")),
                     "native entry build gate requires cached pinned JRE/ECJ/runtime paths")
class NativeFilter(unittest.TestCase):
    def test_exact_rebuild_and_shipped_java_class_request_behavior(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        java = Path(os.environ["THERMITE_JAVA"])
        compiler = Path(os.environ["THERMITE_ECJ"])
        servlet = Path(os.environ["THERMITE_RUNTIME"]) / oh_entry.pins()["servlet"]["runtime_path"]
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            work = Path(temporary)
            self.assertEqual(oh_entry.rebuild(java=java, compiler=compiler, servlet=servlet, work=work),
                             oh_entry.checked_artifact())
            subprocess.run([str(java), "-jar", str(compiler), "-17", "-proc:none", "-classpath",
                            str(oh_entry.ROOT / oh_entry.JAR) + os.pathsep + str(servlet), "-d", str(work),
                            str(AREA / "tests/entry/EntryFilterTest.java")],
                           check=True, capture_output=True, text=True, timeout=30)
            (work / "routes.tsv").write_text("".join(f"{route}\t{asset}\n" for route, asset in oh_routes.ROUTES.items()),
                                             encoding="utf-8")
            result = subprocess.run([str(java), "-cp", os.pathsep.join(map(str, [work, oh_entry.ROOT / oh_entry.JAR,
                                                                              servlet])), "EntryFilterTest",
                                     str(work / "conf"), str(work / "routes.tsv")],
                                    capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("OAuth callbacks and native request preservation passed", result.stdout)


if __name__ == "__main__":
    unittest.main()
