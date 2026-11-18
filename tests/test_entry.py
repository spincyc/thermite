"""Pinned native entry routing and its script-owned Java lifecycle."""

import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import zipfile

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))
import oh_access_mode
import oh_entry
import oh_inventory
import oh_render
import oh_routes
from oh_cli import sandbox as sandbox_cli

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


class SandboxOnly(unittest.TestCase):
    """The no-gateway page mode: the runtime serves the pages and they talk to
    it directly. A sandbox's copy of conf/ has it; what thermite generate
    writes, and so an install, does not."""

    @classmethod
    def setUpClass(cls):
        cls.house = oh_inventory.load(AREA / "instance/devices.yaml")
        cls.files = oh_render.render(cls.house)

    def test_generated_conf_has_neither_the_entry_nor_the_direct_transport(self):
        self.assertFalse(set(oh_entry.files()) & set(self.files))
        self.assertFalse([path for path in self.files if "thermite_entry" in path or "thermite-entry" in path])
        self.assertEqual(self.files["html/remote/access-config.js"], oh_access_mode.script())
        self.assertIn('"mode":"authenticated"', oh_access_mode.script())
        with self.assertRaises(ValueError):
            oh_access_mode.script("automatic")

    def test_both_sandbox_copies_have_them_under_the_same_guards(self):
        for live in (False, True):
            with self.subTest(live=live), mock.patch.object(sandbox_cli.lib, "mirror_conf") as mirror:
                sandbox_cli.mirror(None, live)
                copied = mirror.call_args.args[2](dict(self.files))
                self.assertEqual({path: copied[path] for path in oh_entry.files()}, oh_entry.files())
                self.assertEqual(copied["html/remote/access-config.js"], oh_access_mode.script("native"))
                self.assertIn('"mode":"native"', copied["html/remote/access-config.js"])
                # The mode is added before the copy's guard runs, not after it.
                guard = oh_render.unrelayed if live else sandbox_cli.read_only
                self.assertEqual(copied, guard(oh_render.direct(dict(self.files))))
        self.assertEqual(set(oh_render.direct(self.files)) - set(self.files), set(oh_entry.files()))

    def test_the_copy_asks_for_the_add_on_the_entry_needs_where_the_house_does_not(self):
        import oh_sandbox
        asked = lambda files: oh_sandbox.parse_addons_cfg(files[oh_render.ADDONS_CFG])
        # The example's own rules already ask: its list is left as generated.
        self.assertIn("jsscripting", asked(self.files)["automation"])
        self.assertEqual(oh_render.direct(self.files)[oh_render.ADDONS_CFG], self.files[oh_render.ADDONS_CFG])
        bare = oh_render.render({"version": 1, "devices": []})
        self.assertNotIn("automation", asked(bare))
        copied = oh_render.direct(bare)
        self.assertEqual(asked(copied), {**asked(bare), "automation": ["jsscripting"]})
        listed = bare[oh_render.ADDONS_CFG].replace("misc = ", "automation = groovyscripting\nmisc = ")
        self.assertEqual(asked(oh_render.direct({**bare, oh_render.ADDONS_CFG: listed}))["automation"],
                         ["groovyscripting", "jsscripting"])

    def test_a_house_file_selects_no_page_mode(self):
        for mode in ("native", "authenticated"):
            with self.subTest(mode=mode):
                self.assertEqual(oh_inventory.validate({**self.house, "access": {"mode": mode}}),
                                 ["unknown top-level key 'access'"])


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
