/*
 * The installer's state and keys. Each step is a screen of fields; leaving
 * one asks the core to check its answers, which it alone decides. The
 * frame is drawn by view.js; app.js (tui/remote) paints it, as it paints
 * thermite tui.
 *
 *   create({core, look, label, still, now, onChange, onQuit}) -> controller
 *
 * core is core.js's (or a test's) {request(op, fields, onEvent)}; label
 * names this session for the lock the console and SSH share.
 */
"use strict";

const View = require("./view");

const STEPS = [
  { id: "welcome", title: "Welcome" },
  { id: "network", title: "Network" },
  { id: "hostname", title: "Host name", fields: ["hostname"] },
  { id: "owner", title: "Owner", fields: ["owner", "owner_password", "owner_password_confirm"] },
  { id: "root", title: "Root password", fields: ["root_password", "root_password_confirm"] },
  { id: "region", title: "Region", fields: ["timezone", "locale", "keymap"] },
  { id: "disk", title: "Disk", fields: ["disk"] },
  { id: "system", title: "System", fields: ["filesystem", "kernel"] },
  { id: "review", title: "Review", fields: ["confirm"] },
  { id: "install", title: "Install" },
  { id: "done", title: "Done" }
];
// What each field is; check names the core's answer that a field's error
// is shown under (a confirmation shares its password's).
const FIELDS = {
  hostname: { kind: "text", label: "Host name" },
  owner: { kind: "text", label: "User name" },
  owner_password: { kind: "masked", label: "Password" },
  owner_password_confirm: { kind: "masked", label: "Again", check: "owner_password" },
  root_password: { kind: "masked", label: "Root password" },
  root_password_confirm: { kind: "masked", label: "Again", check: "root_password" },
  timezone: { kind: "choice", label: "Time zone", from: "timezones" },
  locale: { kind: "choice", label: "Language", from: "locales" },
  keymap: { kind: "choice", label: "Keyboard", from: "keymaps" },
  disk: { kind: "disk", label: "Disk" },
  filesystem: { kind: "radio", label: "Filesystem", options: [
    ["btrfs", "Btrfs with subvolumes (recommended)"], ["ext4", "ext4"]] },
  kernel: { kind: "radio", label: "Kernel", options: [
    ["linux-lts", "linux-lts, long-term support (recommended)"], ["linux", "linux, the latest"]] },
  confirm: { kind: "text", label: "Erase" },
  wifi: { kind: "networks", label: "Wi-Fi network" },
  wifi_name: { kind: "text", label: "Network name" },
  wifi_passphrase: { kind: "masked", label: "Passphrase" },
  continue: { kind: "button", label: "Continue" }
};
const DEFAULTS = { hostname: "thermite", timezone: "UTC", locale: "en_US.UTF-8", keymap: "us",
  filesystem: "btrfs", kernel: "linux-lts" };
const CHOICE_ROWS = 7;

function create(options) {
  const core = options.core;
  const ui = {
    step: 0, focus: 0, theme: "dark", answers: Object.assign({}, DEFAULTS), errors: {}, busy: null,
    message: null, facts: null, choices: null, network: null, wifi: { networks: [], cursor: 0, scanned: false },
    probe: null, accepted: null, filters: {}, cursors: {}, progress: null, result: null, failure: null
  };
  const controller = { ui, look: options.look || null, key, mouse: () => {}, frame, start, step: () => STEPS[ui.step] };

  function changed() {
    if (options.onChange) options.onChange();
  }

  function busy(text, promise) {
    ui.busy = text;
    ui.message = null;
    changed();
    return promise.then((value) => {
      ui.busy = null;
      changed();
      return value;
    }, (error) => {
      ui.busy = null;
      ui.message = { tone: "bad", text: error.message };
      changed();
      throw error;
    });
  }

  function start() {
    return busy("Reading this machine", Promise.all([core.request("facts"), core.request("choices")]))
      .then(([facts, choices]) => {
        ui.facts = facts;
        ui.choices = choices;
        ui.network = facts.network;
        const eligible = facts.disks.filter((disk) => disk.eligible);
        if (eligible.length === 1) ui.answers.disk = eligible[0].path;
      }).catch(() => {});
  }

  // ---- fields --------------------------------------------------------------

  function fields() {
    const step = STEPS[ui.step];
    if (step.id === "network") {
      const wireless = ui.network && ui.network.interfaces.some((entry) => entry.wireless);
      const chosen = wireless && choices()[ui.wifi.cursor];
      return (wireless ? ["wifi"].concat(chosen && chosen.hidden ? ["wifi_name"] : [], ["wifi_passphrase"]) : [])
        .concat(["continue"]);
    }
    return step.fields || [];
  }

  function focused() {
    return fields()[ui.focus] || null;
  }

  function matches(name) {
    const field = FIELDS[name];
    const filter = (ui.filters[name] || "").toLowerCase();
    const all = (ui.choices && ui.choices[field.from]) || [];
    return filter ? all.filter((entry) => entry.toLowerCase().includes(filter)) : all;
  }

  // The highlighted entry of a choice: the current answer until the list is moved or searched.
  function cursorOf(name) {
    if (ui.cursors[name] != null) return ui.cursors[name];
    return Math.max(0, matches(name).indexOf(ui.answers[name]));
  }

  // The networks a scan found, then an entry for a hidden one.
  function choices() {
    return ui.wifi.networks.concat([{ ssid: null, hidden: true, security: "psk" }]);
  }

  function disks() {
    return (ui.facts && ui.facts.disks) || [];
  }

  function wirelessDevice() {
    const found = ui.network && ui.network.interfaces.find((entry) => entry.wireless);
    return found ? found.name : null;
  }

  function move(delta) {
    const list = fields();
    if (!list.length) return;
    ui.focus = (ui.focus + delta + list.length) % list.length;
  }

  function moveWithin(name, delta) {
    const field = FIELDS[name];
    if (field.kind === "choice") {
      const count = matches(name).length;
      if (count) ui.cursors[name] = Math.max(0, Math.min(count - 1, cursorOf(name) + delta));
      return true;
    }
    if (field.kind === "disk") {
      const list = disks();
      let at = list.findIndex((disk) => disk.path === ui.answers.disk);
      for (let step = 0; step < list.length; step += 1) {
        at = (at + delta + list.length) % list.length;
        if (list[at].eligible) {
          ui.answers.disk = list[at].path;
          return true;
        }
      }
      return true;
    }
    if (field.kind === "radio") {
      const at = field.options.findIndex((option) => option[0] === ui.answers[name]);
      ui.answers[name] = field.options[(at + delta + field.options.length) % field.options.length][0];
      return true;
    }
    if (field.kind === "networks") {
      ui.wifi.cursor = Math.max(0, Math.min(choices().length - 1, ui.wifi.cursor + delta));
      return true;
    }
    return false;
  }

  function type(name, ch) {
    const field = FIELDS[name];
    if (field.kind === "choice") {
      ui.filters[name] = (ui.filters[name] || "") + ch;
      ui.cursors[name] = 0;
    } else if (field.kind === "text" || field.kind === "masked") {
      const store = name.startsWith("wifi_") ? ui.wifi : ui.answers;
      store[name] = (store[name] || "") + ch;
      delete ui.errors[field.check || name];
    }
  }

  function erase(name, all) {
    const field = FIELDS[name];
    if (field.kind === "choice") {
      ui.filters[name] = all ? "" : (ui.filters[name] || "").slice(0, -1);
      if (ui.filters[name]) ui.cursors[name] = 0;
      else delete ui.cursors[name];
    } else if (field.kind === "text" || field.kind === "masked") {
      const store = name.startsWith("wifi_") ? ui.wifi : ui.answers;
      store[name] = all ? "" : Array.from(store[name] || "").slice(0, -1).join("");
    }
  }

  // ---- steps -----------------------------------------------------------------

  function go(index) {
    ui.step = index;
    ui.focus = 0;
    ui.message = null;
    const step = STEPS[index];
    if (step.id === "hostname" && !ui.probe) probe(ui.answers.hostname, true);
    if (step.id === "network" || step.id === "review") refreshNetwork();
    changed();
  }

  function probe(name, suggest) {
    return core.request("hostname", { name: name }).then((result) => {
      ui.probe = result;
      if (suggest && result.taken && result.suggestion && ui.answers.hostname === name) {
        ui.answers.hostname = result.suggestion;
        ui.probe = Object.assign({}, result, { replaced: name });
      }
      changed();
      return result;
    }).catch(() => null);
  }

  // The installed host joins the Wi-Fi network this live system is joined
  // to; the core reads its passphrase from iwd, so it never comes back here.
  function joined(network) {
    ui.network = network;
    ui.answers.wifi_ssid = network.wifi ? network.wifi.ssid : null;
  }

  function refreshNetwork() {
    return core.request("network").then((network) => {
      joined(network);
      const device = wirelessDevice();
      if (device && !network.online && !ui.wifi.scanned && STEPS[ui.step].id === "network") scan();
      changed();
    }).catch(() => {});
  }

  function scan() {
    const device = wirelessDevice();
    if (!device) return Promise.resolve();
    ui.wifi.scanned = true;
    return busy("Looking for Wi-Fi networks", core.request("wifi-scan", { device: device })).then((networks) => {
      ui.wifi.networks = networks;
      ui.wifi.cursor = 0;
    }).catch(() => {});
  }

  function connect() {
    const chosen = choices()[ui.wifi.cursor];
    if (!chosen) return Promise.resolve();
    const ssid = chosen.hidden ? ui.wifi.wifi_name || "" : chosen.ssid;
    const passphrase = ui.wifi.wifi_passphrase || "";
    return busy("Joining " + ssid, core.request("wifi-connect", { device: wirelessDevice(), ssid: ssid,
      passphrase: passphrase, security: chosen.security, hidden: Boolean(chosen.hidden) })).then((network) => {
      joined(network);
      ui.wifi.wifi_passphrase = "";
      ui.message = { tone: "ok", text: "Connected to " + ssid + "; the installed system joins it too." };
      ui.focus = fields().indexOf("continue");
    }).catch(() => {});
  }

  function check(names) {
    return busy("Checking", core.request("check", { answers: ui.answers, fields: names })).then((result) => {
      ui.errors = result.errors;
      Object.assign(ui.answers, result.values);
      const bad = fields().findIndex((name) => result.errors[FIELDS[name].check || name]);
      if (bad >= 0) ui.focus = bad;
      return !Object.keys(result.errors).length;
    });
  }

  // Enter: within a field first (choose from a list, move to the next
  // field), then the whole step.
  function enter() {
    const name = focused();
    const field = name && FIELDS[name];
    if (field && field.kind === "choice") {
      const list = matches(name);
      const chosen = list[cursorOf(name)];
      if (chosen) ui.answers[name] = chosen;
      ui.filters[name] = "";
      delete ui.cursors[name];
    }
    if (name === "wifi") {
      const chosen = choices()[ui.wifi.cursor];
      const wifi = ui.network && ui.network.wifi;
      if (!chosen.hidden && wifi && wifi.ssid === chosen.ssid && wifi.carried) {
        ui.message = { tone: "ok", text: "Already connected to " + chosen.ssid + "; the installed system joins it too." };
        ui.focus = fields().indexOf("continue");
        return changed();
      }
      if (!chosen.hidden && chosen.security === "open") return connect();
      ui.focus += 1;
      return changed();
    }
    if (name === "wifi_name") {
      ui.focus += 1;
      return changed();
    }
    if (name === "wifi_passphrase") return connect();
    if (field && field.kind !== "button" && ui.focus < fields().length - 1) {
      ui.focus += 1;
      return changed();
    }
    return submit();
  }

  function submit() {
    const step = STEPS[ui.step];
    if (step.id === "welcome") {
      if (!ui.facts) return start();
      return busy("Starting", core.request("claim", { label: options.label || "console" })).then((claim) => {
        if (claim.claimed) return go(1);
        ui.message = { tone: "bad", text: "In use by " + claim.holder + "; Ctrl-C for a shell, where "
          + "tail -f /run/thermite-installer/installer.log follows it." };
        changed();
      }).catch(() => {});
    }
    if (step.id === "network") {
      return refreshNetwork().then(() => {
        const wifi = ui.network && ui.network.wifi;
        if (wifi && !wifi.carried) {
          ui.message = { tone: "bad", text: "Connected to " + wifi.ssid + " without its passphrase saved here: "
            + "choose it above and enter it, so the installed system can join it too." };
        } else if (ui.network && ui.network.online) {
          return go(ui.step + 1);
        } else {
          ui.message = { tone: "bad", text: "Not connected to the Internet yet: plug in a cable or join Wi-Fi." };
        }
        changed();
      });
    }
    if (step.id === "review") {
      return check(Object.keys(FIELDS).filter((name) => isAnswer(name)).concat(["wifi_ssid", "confirm"]))
        .then((ok) => (ok ? install() : changed())).catch(() => {});
    }
    const names = step.fields.filter((name) => !FIELDS[name].check);
    return check(names).then((ok) => {
      if (!ok) return changed();
      if (step.id === "hostname") return hostname();
      return go(ui.step + 1);
    }).catch(() => {});
  }

  function isAnswer(name) {
    return !["confirm", "continue"].includes(name) && !name.startsWith("wifi") && !FIELDS[name].check;
  }

  // A name another host already answers to is allowed, after a second Enter.
  function hostname() {
    const name = ui.answers.hostname;
    return probe(name, false).then((result) => {
      if (result && result.taken && ui.accepted !== name) {
        ui.accepted = name;
        ui.message = { tone: "bad", text: name + ".local already answers on this network"
          + (result.suggestion ? "; " + result.suggestion + " is free" : "") + ". Enter again to keep " + name + "." };
        return changed();
      }
      return go(ui.step + 1);
    });
  }

  function install() {
    go(STEPS.findIndex((step) => step.id === "install"));
    ui.progress = { percent: 0, label: "Starting", phases: [], lines: [] };
    changed();
    return core.request("install", { answers: ui.answers }, (event) => {
      const progress = ui.progress;
      if (event.kind === "phase") {
        progress.percent = event.percent;
        progress.label = event.label;
        progress.phases.push(event);
      } else if (event.kind === "line") {
        progress.lines.push(event.text);
        if (progress.lines.length > 400) progress.lines.splice(0, progress.lines.length - 400);
      }
      changed();
    }).then((result) => {
      ui.result = result;
      ui.failure = null;
      go(STEPS.length - 1);
    }, (error) => {
      ui.result = null;
      ui.failure = error.message;
      go(STEPS.length - 1);
    });
  }

  function back() {
    const step = STEPS[ui.step];
    if (step.id === "install" || ui.step === 0) return;
    if (step.id === "done") {
      if (ui.result) return;
      ui.answers.confirm = "";
      return go(STEPS.findIndex((entry) => entry.id === "review"));
    }
    go(ui.step - 1);
  }

  function quit() {
    if (STEPS[ui.step].id === "install") {
      ui.message = { tone: "bad", text: "The installation is running; it cannot be stopped safely from here." };
      return changed();
    }
    if (options.onQuit) options.onQuit();
  }

  // ---- keys ------------------------------------------------------------------

  function key(ch, info) {
    const name = (info && info.name) || "";
    const full = (info && info.full) || "";
    if (full === "C-c") return quit();
    if (full === "C-t") {
      ui.theme = ui.theme === "dark" ? "light" : "dark";
      return changed();
    }
    if (ui.busy) return;
    const step = STEPS[ui.step];
    if (step.id === "done" && ui.result && (name === "enter" || name === "return")) {
      return busy("Restarting", core.request("reboot")).catch(() => {});
    }
    if (step.id === "network" && full === "C-r") {
      ui.wifi.scanned = true;
      return refreshNetwork().then(scan);
    }
    const current = focused();
    if (name === "enter" || name === "return") return enter();
    if (name === "escape") return back();
    if (name === "tab" && !info.shift) move(1);
    else if (full === "S-tab" || (name === "tab" && info.shift)) move(-1);
    else if (name === "up" || name === "down") {
      if (!current || !moveWithin(current, name === "up" ? -1 : 1)) move(name === "up" ? -1 : 1);
    } else if (name === "left" || name === "right") {
      if (current && FIELDS[current].kind === "radio") moveWithin(current, name === "left" ? -1 : 1);
    } else if (name === "pageup" || name === "pagedown") {
      if (current) moveWithin(current, name === "pageup" ? -CHOICE_ROWS : CHOICE_ROWS);
    } else if (name === "space" && current && FIELDS[current].kind === "radio") moveWithin(current, 1);
    else if (name === "backspace") {
      if (current) erase(current, false);
    } else if (full === "C-u") {
      if (current) erase(current, true);
    } else if (ch && current && ch.length === 1 && ch >= " " && ch !== "\x7f" && !(info && info.ctrl)) {
      type(current, ch);
    } else return;
    changed();
  }

  function frame(width, height) {
    return View.render({
      ui, steps: STEPS, fields: FIELDS, current: fields(), focused: focused(), matches, networks: choices(),
      look: controller.look,
      cursorOf, now: options.now ? options.now() : Date.now(), still: Boolean(options.still), rows: CHOICE_ROWS
    }, width, height);
  }

  return controller;
}

module.exports = { create, STEPS, FIELDS, DEFAULTS };
