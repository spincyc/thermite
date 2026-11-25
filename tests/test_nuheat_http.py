"""Tests for the nuheat_http integration, generated openHAB rule execution
against oh_climate_sim, metrics/alert gating, and Web/TUI climate UI."""

from __future__ import annotations

import copy
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))

import oh_climate_sim  # noqa: E402
import oh_integrations as integrations  # noqa: E402
import oh_inventory  # noqa: E402
import oh_metrics  # noqa: E402
import oh_ports  # noqa: E402
import oh_render  # noqa: E402
import oh_units  # noqa: E402
from oh_climate import nuheat_api  # noqa: E402
from oh_conf import House  # noqa: E402
from oh_features import remote_model  # noqa: E402
from oh_integrations import _io, nuheat_http  # noqa: E402
from oh_metrics import alerts, dashboards  # noqa: E402


def _thermostat_house(*, include_ip: bool = False, monitor: str | None = None,
                      extra_room_climate: bool = False) -> dict:
    data = oh_inventory.load(AREA / "instance/devices.yaml")
    data["rooms"]["example_bath"] = {"label": "Example Bath", "tag": "Bathroom"}
    device: dict = {
        "id": "example_west_floor",
        "room": "example_west",
        "kind": "thermostat",
        "make": "NuHeat",
        "model": "Signature",
        "name": "West Floor",
        "controls": {"control": ["nuheat_http"]},
        "config": {"nuheat_http": {"scale": "F", "step": 100}},
    }
    if include_ip:
        device["ip"] = "192.0.2.88"
    if monitor is not None:
        device["monitor"] = monitor
    data["devices"].append(device)
    if extra_room_climate:
        data["devices"].append({
            "id": "example_bath_floor",
            "room": "example_bath",
            "kind": "thermostat",
            "make": "NuHeat",
            "model": "Signature",
            "name": "Bath Floor",
            "controls": {"control": ["nuheat_http"]},
            "config": {"nuheat_http": {"scale": "C", "step": 50}},
        })
    return data


class IntegrationContract(unittest.TestCase):
    def test_hooks_validation_and_rendering_without_lan_ip(self):
        self.assertIn(nuheat_http, integrations.REGISTRY)
        self.assertEqual(nuheat_http.NAME, "nuheat_http")
        self.assertEqual(nuheat_http.PHASE, "control")
        self.assertFalse(nuheat_http.NEEDS_IP)
        self.assertEqual(nuheat_http.HELPER_SERVICE, oh_units.CLIMATE)
        self.assertEqual(nuheat_http.HELPER, f"http://127.0.0.1:{oh_ports.CLIMATE_PORT}")
        self.assertEqual(nuheat_http.MODES, ("Auto", "Hold", "Manual"))
        self.assertTrue(nuheat_http.SOURCE.is_file())

        data = _thermostat_house(include_ip=False, extra_room_climate=True)
        self.assertEqual(oh_inventory.validate(copy.deepcopy(data)), [])
        house = House(data)
        self.assertIn(oh_units.CLIMATE, integrations.helper_services(house))

        device = house.by_id["example_west_floor"]
        self.assertEqual(nuheat_http.table([device]), [{"id": "example_west_floor", "helper": nuheat_http.HELPER}])
        self.assertIn("example_west_floor", nuheat_http.rule([device]))
        self.assertEqual(nuheat_http.manual_items(device), 2)
        self.assertEqual(nuheat_http.problem(device), "example_west_floor_problem")
        self.assertIn("example_west_floor_climate_mode", nuheat_http.fields(device))

        desc = nuheat_http.climate(device)
        assert desc is not None
        self.assertEqual(desc["device"], "example_west_floor")
        self.assertEqual(desc["scale"], "F")
        self.assertEqual(desc["step"], 100)
        self.assertEqual(desc["modes"], ["Auto", "Hold", "Manual"])
        self.assertEqual(set(desc["items"]), {
            "temperature", "setpoint", "mode", "heating", "hold_until",
            "online", "error", "stale", "write_state", "status", "problem",
        })

        # Render full openHAB conf and check YAML items, script, and aim().
        conf = oh_render.build(house)
        yaml_items = conf.yaml["yaml/climate.yaml"].items
        for suffix in ("temperature", "heating_call", "hold_until", "online", "has_error",
                       "stale", "write_state", "climate_status", "climate_mode", "climate_setpoint"):
            item_name = f"example_west_floor_{suffix}"
            self.assertIn(item_name, yaml_items)
            self.assertIs(yaml_items[item_name]["autoupdate"], False)
        self.assertIn("example_west_floor_problem", yaml_items)
        path = nuheat_http.rule_path(device)
        self.assertIn(path, conf.files)
        aimed = nuheat_http.aim(conf.files, device, "127.0.0.1:19999")
        self.assertIn("http://127.0.0.1:19999", aimed[path])

        # Remote model projects routed room.climate, climateRooms, and devices[id].climate.
        model = remote_model.build(house)
        west = next(r for r in model["rooms"] if r["id"] == "example_west")
        self.assertEqual(west["climate"], ["example_west_floor"])
        self.assertEqual(model["devices"]["example_west_floor"]["climate"], desc)
        self.assertEqual(model["climateRooms"], [{
            "id": "example_bath", "label": "Example Bath", "climate": ["example_bath_floor"],
        }])

    def test_validation_rejects_bad_scale_step_and_device_kind(self):
        bad_scale = _thermostat_house()
        bad_scale["devices"][-1]["config"]["nuheat_http"]["scale"] = "K"
        self.assertTrue(any("scale" in p for p in oh_render.validate(bad_scale)))

        bad_step = _thermostat_house()
        bad_step["devices"][-1]["config"]["nuheat_http"]["step"] = 0
        self.assertTrue(any("step" in p for p in oh_render.validate(bad_step)))

        bad_kind = _thermostat_house()
        bad_kind["devices"][-1]["kind"] = "tv"
        self.assertTrue(any("thermostat" in p for p in oh_render.validate(bad_kind)))


class ClimateMetricsAndGating(unittest.TestCase):
    def test_thermostat_signals_health_checks_and_service_gating(self):
        base_house = House(oh_inventory.load(AREA / "instance/devices.yaml"))
        base_map = oh_metrics.exporter_map(base_house)
        self.assertNotIn(oh_units.CLIMATE, base_map["helpers"])
        self.assertNotIn(oh_units.CLIMATE, alerts.active_daemons(base_map))
        self.assertNotIn("climate_helper", json.dumps(alerts.checks(base_map)[0]))
        base_prom = oh_metrics.prometheus_yml(base_house, {"http_port": 13602})
        self.assertNotIn("climate_helper", base_prom)

        # With a thermostat: helper probe, daemon alert, dashboard tile, and thermostat signals appear.
        house = House(_thermostat_house())
        conf = oh_render.build(house)
        mapping = oh_metrics.exporter_map(house, conf)
        self.assertIn(oh_units.CLIMATE, mapping["helpers"])
        self.assertIn(oh_units.CLIMATE, alerts.active_daemons(mapping))
        self.assertIn("climate_helper", json.dumps(alerts.checks(mapping)[0]))
        self.assertIn("climate_helper", oh_metrics.prometheus_yml(house, {"http_port": 13602}))

        # Verify thermostat signals & health checks.
        t_checks = [c for c in mapping["health_checks"] if c["device"] == "example_west_floor"]
        self.assertEqual({c["check"] for c in t_checks}, {"reads", "online", "has_error"})
        self.assertEqual(mapping["items"]["example_west_floor_heating_call"]["point"], "heating")
        self.assertEqual(mapping["items"]["example_west_floor_has_error"]["point"], "has_error")
        self.assertEqual(mapping["items"]["example_west_floor_climate_setpoint"]["point"], "setpoint")
        self.assertEqual(mapping["items"]["example_west_floor_temperature"]["point"], "temperature")

        # Dashboard host tile includes thermite-climate.service only when active.
        host_with = json.dumps(dashboards.host(mapping))
        host_without = json.dumps(dashboards.host(base_map))
        self.assertIn("thermite-climate.service", host_with)
        self.assertNotIn("thermite-climate.service", host_without)

        # monitor: optional marks thermostat health checks as optional (required="false").
        opt_house = House(_thermostat_house(monitor="optional"))
        opt_map = oh_metrics.exporter_map(opt_house)
        opt_checks = [c for c in opt_map["health_checks"] if c["device"] == "example_west_floor"]
        self.assertTrue(opt_checks)
        self.assertTrue(all(c.get("optional") is True for c in opt_checks))
        opt_rules = [r for r in alerts.checks(opt_map)[0] if r["labels"].get("device") == "example_west_floor"]
        self.assertTrue(opt_rules)
        self.assertTrue(all(r["labels"].get("required") == "false" for r in opt_rules))


@unittest.skipUnless(shutil.which("node"), "node is required for rule and UI harnesses")
class LiveRuleAndClientUi(unittest.TestCase):
    def test_generated_nuheat_rule_polls_commands_honors_send_and_indeterminate(self):
        now_box = [datetime(2026, 11, 25, 18, 0, 0, tzinfo=timezone.utc)]

        def tick_now() -> datetime:
            now_box[0] = now_box[0] + timedelta(seconds=1)
            return now_box[0]

        sim_t = oh_climate_sim.Thermostat(
            serial="NH-RULE-001",
            name="West Floor",
            online=True,
            is_heating=True,
            current_temperature=2140,
            setpoint_temperature=2200,
            mode=1,
            error_state=None,
        )
        contract = nuheat_api.DeviceContract(
            serial="NH-RULE-001",
            temperature_encoding="celsius_x100",
            minimum_raw=1000,
            maximum_raw=3000,
            writable_modes=("Auto", "Hold", "Manual"),
            mode_codes={"Auto": 1, "Hold": 2, "Manual": 3},
        )
        with tempfile.TemporaryDirectory() as tmp:
            with oh_climate_sim.Server(
                {"NH-RULE-001": sim_t},
                {"example_west_floor": contract},
                secrets=Path(tmp) / "secrets",
                now=tick_now,
                poll_interval_s=0,
            ) as server:
                house = House(_thermostat_house())
                device = house.by_id["example_west_floor"]
                conf = oh_render.build(house)
                path = nuheat_http.rule_path(device)
                aimed = nuheat_http.aim(conf.files, device, server.address)[path]
                # Swap only the GraalJS Java bounded-IO block for our Node.js thermiteIO shim.
                java_io = _io.SOURCE.read_text(encoding="utf-8")
                self.assertIn(java_io, aimed)
                aimed_node = aimed.replace(java_io, "", 1)
                readonly_rule = aimed_node.replace("const SEND = true;", "const SEND = false;", 1)

                script = r"""
"use strict";
const assert = require("assert/strict");
const { execFileSync } = require("child_process");
const vm = require("vm");

function runRule(source) {
  const states = Object.create(null);
  const rules = [];
  const logs = [];
  const fakeOpenhab = {
    items: {
      getItem(name) {
        return {
          get state() { return states[name] ?? "NULL"; },
          postUpdate(v) { states[name] = String(v); },
          sendCommand(v) { states[name] = String(v); },
        };
      },
    },
    triggers: {
      GenericCronTrigger: (expr) => ({ kind: "cron", expr }),
      ItemCommandTrigger: (item) => ({ kind: "command", item }),
    },
    rules: {
      JSRule(def) { rules.push(def); return def; },
    },
  };
  // Synchronous HTTP bridge backed by a short python subprocess so openHAB's
  // synchronous thermiteIO.http() runs cleanly against the real loopback server.
  const thermiteIO = {
    http(url, method, body, headers) {
      const py = `
import sys, json, urllib.request, urllib.error
url, method, body = sys.argv[1], sys.argv[2], sys.argv[3]
data = body.encode('utf-8') if body != '__NONE__' else None
hdrs = {'Accept': 'application/json'}
if data is not None: hdrs['Content-Type'] = 'application/json'
req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
try:
    with urllib.request.urlopen(req, timeout=5) as r:
        print(json.dumps({'status': r.status, 'body': r.read().decode('utf-8')}))
except urllib.error.HTTPError as e:
    print(json.dumps({'status': e.code, 'body': e.read().decode('utf-8')}))
`;
      const out = execFileSync(process.argv[1], ["-c", py, url, method, body === null ? "__NONE__" : body], { encoding: "utf8" });
      const parsed = JSON.parse(out);
      return { getStatus: () => parsed.status, getContentAsString: () => parsed.body };
    },
    unreached: () => false,
  };
  const ctx = vm.createContext({
    require: (name) => { if (name === "openhab") return fakeOpenhab; throw new Error("unexpected require " + name); },
    console: { info: (m) => logs.push(m), warn: (m) => logs.push(m), error: (m) => logs.push(m), debug() {} },
    Date, JSON, Number, String, Array, Object, Boolean, Math, Error, RegExp, Set, Map,
    thermiteIO,
  });
  vm.runInContext(source, ctx);
  const poll = rules.find((r) => r.id.endsWith("_poll"));
  const cmd = rules.find((r) => r.id.endsWith("_command"));
  return { states, poll, cmd, logs };
}

// 1. Live write-enabled rule: poll, setpoint write, mode write.
const live = runRule(process.argv[2]);
live.poll.execute();
assert.equal(live.states.example_west_floor_temperature, "21.4");
assert.equal(live.states.example_west_floor_climate_setpoint, "2200");
assert.equal(live.states.example_west_floor_climate_mode, "Auto");
assert.equal(live.states.example_west_floor_heating_call, "ON");
assert.equal(live.states.example_west_floor_online, "ON");
assert.equal(live.states.example_west_floor_has_error, "OFF");
assert.equal(live.states.example_west_floor_stale, "OFF");
assert.equal(live.states.example_west_floor_write_state, "idle");
assert.equal(live.states.example_west_floor_climate_status, "ok");

// Command setpoint -> POST /mode -> acknowledged, then next poll -> confirmed.
live.cmd.execute({ itemName: "example_west_floor_climate_setpoint", receivedCommand: "2300" });
assert.equal(live.states.example_west_floor_write_state, "acknowledged");
live.poll.execute();
assert.equal(live.states.example_west_floor_climate_setpoint, "2300");
assert.equal(live.states.example_west_floor_climate_mode, "Manual");
assert.equal(live.states.example_west_floor_write_state, "confirmed");

// 2. Read-only rule (SEND = false) withholds writes and keeps tally clean.
const ro = runRule(process.argv[3]);
ro.poll.execute();
ro.cmd.execute({ itemName: "example_west_floor_climate_mode", receivedCommand: "Auto" });
assert.equal(ro.states.example_west_floor_climate_mode, "Manual");
const tally = JSON.parse(ro.states.example_west_floor_nuheat_http_commands);
assert.equal(tally.outcomes.dropped.count, 1);
assert.ok(ro.logs.some((l) => l.includes("outcome=dropped code=read_only")));
process.stdout.write(JSON.stringify({ ok: true }));
"""
                # Run the rule script, then verify indeterminate write propagation too.
                node = shutil.which("node")
                assert node is not None
                run = subprocess.run([node, "-e", script, sys.executable, aimed_node, readonly_rule],
                                     capture_output=True, text=True, timeout=30)
                self.assertEqual(run.returncode, 0, run.stderr or run.stdout)
                self.assertEqual(json.loads(run.stdout), {"ok": True})

                # Verify indeterminate write updates write_state=indeterminate and records unknown outcome.
                server.cloud.next_write_fault = "http_500"
                indet_script = script.split("// 1. Live write-enabled rule:")[0] + r"""
const live = runRule(process.argv[2]);
live.poll.execute();
live.cmd.execute({ itemName: "example_west_floor_climate_mode", receivedCommand: "Auto" });
assert.equal(live.states.example_west_floor_write_state, "indeterminate");
const tally = JSON.parse(live.states.example_west_floor_nuheat_http_commands);
assert.equal(tally.outcomes.unknown.count, 1);
assert.ok(live.logs.some((l) => l.includes("outcome=unknown code=no_answer")));
process.stdout.write(JSON.stringify({ ok: true }));
"""
                run2 = subprocess.run([node, "-e", indet_script, sys.executable, aimed_node],
                                      capture_output=True, text=True, timeout=30)
                self.assertEqual(run2.returncode, 0, run2.stderr or run2.stdout)
                self.assertEqual(json.loads(run2.stdout), {"ok": True})

                # Advance clock > 120 s with read fault -> stale=ON and climate_status=stale.
                server.cloud.next_read_fault = "timeout"
                now_box[0] = now_box[0] + timedelta(seconds=180)
                stale_script = script.split("// 1. Live write-enabled rule:")[0] + r"""
const live = runRule(process.argv[2]);
live.poll.execute();
assert.equal(live.states.example_west_floor_stale, "ON");
assert.equal(live.states.example_west_floor_climate_status, "stale");
process.stdout.write(JSON.stringify({ ok: true }));
"""
                run3 = subprocess.run([node, "-e", stale_script, sys.executable, aimed_node],
                                      capture_output=True, text=True, timeout=30)
                self.assertEqual(run3.returncode, 0, run3.stderr or run3.stdout)
                self.assertEqual(json.loads(run3.stdout), {"ok": True})

    def test_web_and_tui_climate_controls_render_and_command(self):
        house = House(_thermostat_house(extra_room_climate=True))
        model = remote_model.build(house)
        script = r"""
"use strict";
const assert = require("assert/strict");
const fs = require("fs"), path = require("path"), vm = require("vm");
const webDir = process.argv[1];
const tuiDir = process.argv[2];
const testsDir = process.argv[3];
const house = JSON.parse(process.argv[4]);
const read = (name) => fs.readFileSync(path.join(webDir, name), "utf8");
const DOM = require(path.join(testsDir, "remote_dom"));

// 1. Web RemoteBackend + RemoteSession + RemoteClimate
const quiet = { log() {}, info() {}, warn() {}, error() {} };
const ctx = vm.createContext({ console: quiet, setTimeout, clearTimeout });
ctx.globalThis = ctx;
for (const name of ["remotes.js", "backend.js", "backend-mock.js", "session.js", "climate.js"]) {
  vm.runInContext(read(name), ctx);
}
const backend = ctx.RemoteBackend.create(house, {
  backend: "mock",
  latency: 0,
  climate: {
    example_west_floor: {
      temperature: "70.5", setpoint: "7200", mode: "Hold", heating: "ON",
      hold_until: "2026-11-25T20:30:00+00:00", online: "ON", error: "OFF",
      stale: "ON", write_state: "indeterminate", status: "stale"
    }
  }
});
const session = ctx.RemoteSession.create({ backend, house, store: ctx.RemoteSession.memoryStore() });
(async () => {
  await session.start("example_west");
  const doc = new DOM.Document(read("index.html"));
  const box = doc.querySelector("[data-climate]");
  ctx.RemoteClimate.render(box, session);
  assert.equal(box.hidden, false, "climate card visible on room with thermostat");
  assert.equal(doc.querySelector('[data-climate-temp="example_west_floor"]').textContent, "70.5°F");
  assert.equal(doc.querySelector('[data-climate-target="example_west_floor"]').textContent, "72°F");
  assert.equal(doc.querySelector("[data-climate-heating]").textContent, "Heating");
  assert.ok(doc.querySelector("[data-climate-hold]").textContent.includes("Hold until"));
  assert.ok(doc.querySelector('[data-climate-banner="stale"]'), "stale banner rendered");
  assert.ok(doc.querySelector('[data-climate-banner="indeterminate"]'), "indeterminate banner rendered");

  // Step setpoint +1 (+100 raw -> 7300) and switch mode to Auto.
  await session.stepClimateSetpoint("example_west_floor", 1);
  await new Promise((r) => setTimeout(r, 20));
  assert.equal(session.climateOf("example_west_floor").setpoint, "7300");
  await session.setClimateMode("example_west_floor", "Auto");
  await new Promise((r) => setTimeout(r, 20));
  assert.equal(session.climateOf("example_west_floor").mode, "Auto");
  assert.equal(session.climateOf("example_west_floor").writeState, "confirmed");

  // Switch to room without thermostat -> hidden completely.
  session.selectRoom("example_east");
  ctx.RemoteClimate.render(box, session);
  assert.equal(box.hidden, true, "climate card hidden on AV-only room");
  assert.equal(box.children.length, 0);

  // 2. TUI Climate renderer + Controller interaction
  session.selectRoom("example_west");
  const View = require(path.join(tuiDir, "view"));
  const Controller = require(path.join(tuiDir, "controller"));
  const ctrl = Controller.create({
    session, Session: ctx.RemoteSession, Remote: ctx.RemoteBackend,
    layouts: ctx.RemoteLayouts.forHouse(house), house,
    store: ctx.RemoteSession.memoryStore(), screen: "remote",
    backend
  });
  await ctrl.start("example_west");
  const frame = ctrl.frame(120, 40);
  const text = frame.grid.rows.map((r) => r.map((c) => c.ch).join("")).join("\n");
  assert.ok(text.includes("Climate"), "TUI draws Climate section");
  assert.ok(text.includes("West Floor"), "TUI draws thermostat label");
  assert.ok(text.includes("70.5°F"), "TUI draws temperature");
  assert.ok(text.includes("Heating"), "TUI draws heating indicator");
  assert.ok(frame.focusables.some((h) => h.key === "climate-step:up:example_west_floor"), "TUI exposes + hit");
  assert.ok(frame.focusables.some((h) => h.key === "climate-mode:Manual:example_west_floor"), "TUI exposes Manual hit");

  // Clicking TUI + hit steps setpoint.
  const upHit = frame.focusables.find((h) => h.key === "climate-step:up:example_west_floor");
  assert.ok(upHit);
  const before = Number(session.climateOf("example_west_floor").setpoint);
  const coords = [];
  frame.grid.hits.forEach((h) => { if (h.hit.key === upHit.key) coords.push(h); });
  assert.ok(coords.length > 0);
  ctrl.mouse({ action: "mousedown", button: "left", x: coords[0].x, y: coords[0].y });
  await new Promise((r) => setTimeout(r, 20));
  assert.equal(Number(session.climateOf("example_west_floor").setpoint), before + 100);

  // AV-only room in TUI draws NO Climate heading or climate hits.
  session.selectRoom("example_east");
  const eastFrame = ctrl.frame(120, 40);
  const eastText = eastFrame.grid.rows.map((r) => r.map((c) => c.ch).join("")).join("\n");
  assert.ok(!eastText.includes("Climate"), "AV-only room has no Climate section in TUI");
  assert.ok(!eastFrame.focusables.some((h) => String(h.key).startsWith("climate-")));
  ctrl.stop();
  process.stdout.write(JSON.stringify({ ok: true }));
})().catch((err) => { process.stderr.write(err.stack + "\n"); process.exit(1); });
"""
        node = shutil.which("node")
        assert node is not None
        run = subprocess.run(
            [node, "-e", script, str(AREA / "web/remote"), str(AREA / "tui/remote"),
             str(AREA / "tests"), json.dumps(model)],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(run.returncode, 0, run.stderr or run.stdout)
        self.assertEqual(json.loads(run.stdout), {"ok": True})


if __name__ == "__main__":
    unittest.main()
