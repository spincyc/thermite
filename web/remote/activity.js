/* Public house activity projected from typed openHAB events. No raw event,
 * command, source, response text or pairing credential leaves this module.
 * at is the client's receipt time. History belongs to each visible client.
 */
(function (global) {
  "use strict";
  const STATES = {
    paired: "Paired", unpaired: "Not paired", code_shown: "Showing a code", repairing: "Pairing again",
    checking: "Checking", failed: "Pairing failed", closed: "Cannot ask for a code", unreachable: "Not answering",
    unavailable: "Cannot pair now"
  };
  const PRIVATE_ITEM = /(?:^|_)(?:password|passwd|passphrase|secret|token|psk|authorization|credential|auth)(?:_|$)/i;
  const TOPIC = /^openhab\/items\/([A-Za-z][A-Za-z0-9_]*)\/(command|stateupdated|statechanged)$/;
  const KINDS = new Set(["switch", "enum", "volume", "key", "redacted", "problem", "pairing_status", "pairing_result"]);
  const own = (object, key) => Object.prototype.hasOwnProperty.call(object, key);
  function clean(value, limit) {
    return typeof value === "string"
      ? value.replace(/[\x00-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]/g, " ").slice(0, limit) : "";
  }
  function numeric(value) {
    return typeof value === "string" && /^(?:\d+(?:\.\d+)?|\.\d+)$/.test(value) ? Number(value) : NaN;
  }
  function create(options) {
    const config = options || {};
    const emit = config.emit || (() => {});
    const now = config.now || (() => Date.now());
    const descriptors = Object.create(null);
    const previous = Object.create(null);
    const watched = new Set(config.watched || []);
    Object.keys(config.items || {}).forEach((name) => {
      const desc = config.items[name];
      if (!/^[A-Za-z][A-Za-z0-9_]*$/.test(name) || PRIVATE_ITEM.test(name) || !desc || !KINDS.has(desc.kind)) return;
      const pairing = name.includes("_pairing");
      if (pairing && !((name.endsWith("_pairing_status") && desc.kind === "pairing_status")
          || (name.endsWith("_pairing_result") && desc.kind === "pairing_result"))) return;
      const stateEvent = watched.has(name) && desc.kind !== "problem" ? "stateupdated" : "statechanged";
      descriptors[name] = Object.assign({}, desc, { commands: !pairing && desc.commands === true, stateEvent });
    });
    function push(desc, kind, body, tone) {
      emit({ type: "activity", at: now(), kind, room: clean(desc.room, 80) || null,
        device: clean(desc.device, 120) || null, title: clean(desc.label, 120), body: clean(body, 320), tone: tone || "" });
    }
    function valueOf(desc, value, command) {
      if (typeof value !== "string") return null;
      if (value === "UNDEF" || value === "NULL") return command ? null : { valueKey: value, text: "Unknown", tone: "muted" };
      if (desc.kind === "switch") return value === "ON" || value === "OFF"
        ? { valueKey: value, text: value === "ON" ? "On" : "Off" } : null;
      if (desc.kind === "volume") {
        const number = numeric(value);
        const maximum = command ? desc.commandMax : 100;
        if (!Number.isFinite(number) || !Number.isFinite(maximum) || number < 0 || number > maximum) return null;
        const offset = Number.isFinite(desc.offset) ? desc.offset : 0;
        return { valueKey: String(number), text: String(Math.round((number + offset) * 10) / 10) + (desc.unit === "dB" ? " dB" : "") };
      }
      if (desc.kind === "enum") {
        const key = own(desc.options || {}, value) ? value : String(numeric(value));
        return own(desc.options || {}, key) ? { valueKey: key, text: clean(desc.options[key], 160) } : null;
      }
      if (desc.kind === "key") {
        const buttons = (config.layouts || {}).buttons || {};
        return own(buttons, value) ? { valueKey: value, text: clean(buttons[value].label, 120) } : null;
      }
      if (desc.kind === "pairing_status") return own(STATES, value) ? { valueKey: value, text: STATES[value] } : null;
      return { valueKey: "redacted", text: "Value hidden" };
    }
    function result(desc, text) {
      let answer;
      try { answer = JSON.parse(text); } catch (error) { return; }
      if (!answer || typeof answer.state !== "string" || !own(STATES, answer.state)) return;
      let body = STATES[answer.state];
      if (answer.state === "failed") {
        const error = answer.error;
        if (error && typeof error === "object") {
          body += error.code === 1 ? ": Sony error 1 (Internal Server Error)" : ": registration rejected";
          if (Number.isInteger(error.http) && error.http >= 100 && error.http <= 599) body += ", HTTP " + error.http;
        }
      }
      push(desc, "state", body, answer.state === "failed" || answer.state === "unreachable" ? "bad" : "");
    }
    function accept(topic, payloadText) {
      const match = TOPIC.exec(typeof topic === "string" ? topic : "");
      if (!match || !own(descriptors, match[1])) return;
      const desc = descriptors[match[1]];
      const verb = match[2];
      if (verb === "command" ? !desc.commands : !desc.states || verb !== desc.stateEvent) return;
      let payload;
      try { payload = typeof payloadText === "string" ? JSON.parse(payloadText) : null; } catch (error) { return; }
      if (!payload || typeof payload.value !== "string") return;
      if (verb === "command") {
        const value = valueOf(desc, payload.value, true);
        if (value) push(desc, "command", "Requested: " + value.text);
        return;
      }
      if (verb === "statechanged" && (typeof payload.oldValue !== "string" || payload.oldValue === payload.value)) return;
      if (desc.kind === "pairing_result") { result(desc, payload.value); return; }
      if (desc.kind === "problem") {
        const problem = (value) => value !== "UNDEF" && value !== "NULL" && value.trim() !== "";
        const bad = problem(payload.value);
        if (bad === problem(payload.oldValue)) return;
        push(desc, "problem", bad ? "Device control unavailable" : "Device control available again", bad ? "bad" : "");
        return;
      }
      const value = valueOf(desc, payload.value, false);
      if (verb === "stateupdated") {
        const valueKey = value ? value.valueKey : "unknown";
        const before = previous[match[1]];
        previous[match[1]] = valueKey;
        if (before === undefined || before === valueKey) return;
      } else if (value && desc.kind !== "redacted") {
        const old = valueOf(desc, payload.oldValue, false);
        if (old && old.valueKey === value.valueKey) return;
      }
      push(desc, "state", value ? "Reported: " + value.text : "Reported value changed (value hidden)", value && value.tone);
    }
    function topics() {
      const out = [];
      Object.keys(descriptors).forEach((name) => {
        const desc = descriptors[name];
        if (desc.states) out.push("openhab/items/" + name + "/" + desc.stateEvent);
        if (desc.commands) out.push("openhab/items/" + name + "/command");
      });
      return out;
    }
    function seed(name, value) {
      if (!own(descriptors, name) || previous[name] !== undefined) return;
      const projected = valueOf(descriptors[name], value, false);
      previous[name] = projected ? projected.valueKey : "unknown";
    }
    return { topics, accept, seed };
  }
  function preview(house) {
    const items = Object.create(null);
    (house.rooms || []).forEach((room) => {
      const options = { off: "Off" };
      room.sources.forEach((id) => { options[id] = house.sources[id].label; });
      items["loc_" + room.id + "_source"] = { label: "Source", room: room.label, kind: "enum", options, states: true, commands: true };
    });
    Object.keys(house.devices || {}).forEach((id) => {
      const device = house.devices[id];
      const control = device.control || {};
      if (device.problem) items[device.problem] = { label: "Device control", device: device.label,
        kind: "problem", states: true, commands: false };
      for (const field of ["power", "mute", "key"]) {
        if (control[field]) items[control[field]] = { label: field, device: device.label,
          kind: field === "key" ? "key" : "switch", states: field !== "key", commands: true };
      }
      if (control.volume) items[control.volume.item] = Object.assign({ label: "Volume", device: device.label,
        kind: "volume", states: true, commands: true, commandMax: control.volume.max }, control.volume);
      if (control.input) items[control.input.item] = { label: "Input", device: device.label, kind: "enum",
        states: true, commands: true, options: Object.fromEntries(control.input.options) };
    });
    items.house_all_off = { label: "All off", kind: "switch", states: false, commands: true };
    return { version: 1, items };
  }
  global.RemoteActivity = { create, clean, preview };
})(globalThis);
