/*
 * What the installer shows, drawn into a Grid as thermite tui draws its
 * pages (tui/remote): the header, the steps and the current step's panel
 * in btop boxes, the status line and the keys, in a thermite theme's
 * styles, with the torch on the welcome screen. render() only reads.
 */
"use strict";

const { Grid, box, fit, wrap, length } = require("../remote/grid");
const { METER } = require("../remote/theme");
const Torch = require("../remote/torch");

const LIT = "●";
const UNLIT = "○";
const STEPS_WIDTH = 24;
const LABEL = 18;

function render(ctx, width, height) {
  const g = new Grid(width, height, "base");
  const step = ctx.ui.step;
  let animate = null;
  header(g, ctx, width);
  const rows = Math.max(0, height - 3);
  const side = width >= 84 && rows >= 8 ? STEPS_WIDTH : 0;
  if (side) stepsPanel(g, ctx, { x: 0, y: 1, w: side, h: rows });
  const rect = { x: side, y: 1, w: width - side, h: rows };
  if (rect.w >= 20 && rect.h >= 4) {
    const current = ctx.steps[step];
    box(g, rect, { border: "border.remote", title: current.title, titleStyle: "title.remote",
      right: current.id === "install" || current.id === "done" ? null : (step + 1) + " of " + (ctx.steps.length - 2),
      footer: hints(ctx) });
    const inner = new Grid(rect.w - 2, rect.h - 2, "base");
    animate = SCREENS[current.id](inner, ctx);
    g.blit(inner, 0, rect.x + 1, rect.y + 1, rect.w - 2, rect.h - 2);
  }
  statusLine(g, ctx, width, height - 2);
  keys(g, ctx, width, height - 1);
  return { grid: g, animate: animate };
}

function header(g, ctx, width) {
  g.fill(0, 0, width, 1, "header");
  let at = g.put(0, 0, " thermite ", "header.title");
  at = g.put(at, 0, " installer ", "tab.current");
  const step = ctx.steps[ctx.ui.step];
  if (width >= 60) g.put(at + 1, 0, fit(step.title, Math.max(0, width - at - 20)), "header.dim");
  const facts = ctx.ui.facts;
  const right = facts && facts.application ? " " + facts.application + " " : "";
  if (right && width - length(right) > at + 12) g.put(width - length(right), 0, right, "header.dim");
}

function stepsPanel(g, ctx, rect) {
  box(g, rect, { border: "border.rooms", title: "steps", titleStyle: "title.rooms" });
  ctx.steps.slice(0, -1).forEach((step, index) => {
    const y = rect.y + 1 + index;
    if (y >= rect.y + rect.h - 1) return;
    const done = index < ctx.ui.step;
    const current = index === ctx.ui.step;
    const style = current ? "row.current" : "row";
    g.fill(rect.x + 1, y, rect.w - 2, 1, style);
    g.put(rect.x + 2, y, current ? "▸" : done ? LIT : UNLIT, current ? style : done ? "led.ok" : "led.off");
    g.put(rect.x + 4, y, fit(step.title, rect.w - 6), current ? style : done ? "base" : "dim");
  });
}

function hints(ctx) {
  const id = ctx.steps[ctx.ui.step].id;
  if (id === "welcome") return [["Enter", "begin"]];
  if (id === "install") return [];
  if (id === "done") return ctx.ui.result ? [["Enter", "restart"]] : [["Esc", "review"]];
  return [["Enter", "next"], ["Esc", "back"], ["Tab", "field"]];
}

function statusLine(g, ctx, width, y) {
  const ui = ctx.ui;
  if (ui.busy) {
    g.put(1, y, fit("⋯ " + ui.busy + "…", width - 2), "tone.busy");
    return;
  }
  if (ui.message) {
    g.put(1, y, fit(ui.message.text, width - 2), ui.message.tone === "bad" ? "alert" : "tone." + ui.message.tone);
    return;
  }
  const field = ctx.focused && ctx.fields[ctx.focused];
  const help = !field ? null : {
    choice: "Type to search · ↑↓ choose · Enter selects",
    radio: "←→ or Space chooses · Enter goes on",
    disk: "↑↓ chooses a disk · Enter goes on",
    networks: "↑↓ chooses a network · Enter joins it · Ctrl-R scans again",
    masked: "Typed characters are hidden · Ctrl-U clears",
    text: "Ctrl-U clears"
  }[field.kind];
  if (help) g.line(1, y, [["▸ ", "hotkey"], [fit(help, width - 4), "dim"]]);
}

function keys(g, ctx, width, y) {
  g.fill(0, y, width, 1, "footer");
  let at = 1;
  const list = hints(ctx).concat([["Ctrl-T", "Theme"]]);
  list.forEach((entry) => {
    if (at + length(entry[0]) + length(entry[1]) + 4 > width - 14) return;
    at = g.put(at, y, entry[0], "footer.key");
    at = g.put(at, y, " " + entry[1] + "   ", "footer");
  });
  if (ctx.steps[ctx.ui.step].id !== "install" && width >= 16) {
    const quit = g.put(width - 13, y, "Ctrl-C", "footer.key");
    g.put(quit, y, " Quit", "footer");
  }
}

// ---- pieces ------------------------------------------------------------------

function paragraph(g, y, text, style, width) {
  wrap(text, Math.max(8, (width || g.width) - 4)).forEach((line) => g.put(2, y++, line, style || "dim"));
  return y;
}

function row(g, y, label, value, style) {
  g.put(2, y, fit(label, LABEL - 1), "dim");
  g.put(2 + LABEL, y, fit(value, Math.max(4, g.width - LABEL - 4)), style || "base");
  return y + 1;
}

function gigabytes(bytes) {
  return (bytes / 1e9).toFixed(bytes >= 1e11 ? 0 : 1) + " GB";
}

function describeDisk(disk) {
  return [disk.path, gigabytes(disk.size), disk.model, disk.transport].filter(Boolean).join(" · ");
}

// A field's error, below the last field it covers: a password's below its confirmation.
function error(g, y, ctx, name) {
  const key = ctx.fields[name].check || name;
  const message = ctx.ui.errors[key];
  const last = ctx.current.filter((other) => (ctx.fields[other].check || other) === key).pop();
  if (!message || (last && last !== name)) return y;
  return paragraph(g, y, "! " + message, "alert");
}

function field(g, y, ctx, name) {
  const spec = ctx.fields[name];
  const ui = ctx.ui;
  const active = ctx.focused === name;
  const width = Math.max(8, Math.min(44, g.width - LABEL - 4));
  g.put(2, y, fit(spec.label, LABEL - 1), active ? "strong" : "dim");
  const x = 2 + LABEL;
  if (spec.kind === "text" || spec.kind === "masked") {
    const value = (name === "wifi_passphrase" ? ui.wifi.wifi_passphrase : ui.answers[name]) || "";
    const shown = spec.kind === "masked" ? "•".repeat(length(value)) : value;
    const tail = Array.from(shown).slice(-Math.max(1, width - 1)).join("");
    g.put(x, y, fit(tail + (active ? "█" : ""), width), active ? "field.focus" : "field");
    return error(g, y + 1, ctx, name);
  }
  if (spec.kind === "radio") {
    spec.options.forEach((option, index) => {
      const chosen = ui.answers[name] === option[0];
      const style = active && chosen ? "row.focus" : chosen ? "row.current" : "row";
      g.put(x, y + index, fit((chosen ? "◉ " : "○ ") + option[1], g.width - x - 2), style);
    });
    return error(g, y + spec.options.length, ctx, name);
  }
  if (spec.kind === "choice") {
    g.put(x, y, fit(ui.answers[name] || "", width), active ? "field.focus" : "field");
    if (!active) return error(g, y + 1, ctx, name);
    y += 1;
    g.line(x, y, [["Search: ", "dim"], [fit((ui.filters[name] || "") + "█", width - 8), "field"]]);
    const list = ctx.matches(name);
    const cursor = ctx.cursorOf(name);
    const first = Math.max(0, Math.min(cursor - Math.floor(ctx.rows / 2), list.length - ctx.rows));
    list.slice(first, first + ctx.rows).forEach((entry, index) => {
      const on = first + index === cursor;
      g.put(x, y + 1 + index, fit((on ? "▸ " : "  ") + entry, width), on ? "row.focus" : "row");
    });
    if (!list.length) g.put(x, y + 1, "No match", "dim");
    return error(g, y + 2 + Math.min(ctx.rows, Math.max(1, list.length)), ctx, name);
  }
  if (spec.kind === "disk") {
    const disks = (ui.facts && ui.facts.disks) || [];
    if (!disks.length) g.put(x, y, "No disks found", "alert");
    disks.forEach((disk, index) => {
      const chosen = ui.answers.disk === disk.path;
      const style = !disk.eligible ? "faint" : active && chosen ? "row.focus" : chosen ? "row.current" : "row";
      g.put(x, y + index * 2, fit((chosen ? "◉ " : "○ ") + describeDisk(disk), g.width - x - 2), style);
      const parts = disk.partitions.map((part) => part.name + (part.fstype ? " " + part.fstype : "")).join(", ");
      const detail = disk.eligible ? (parts ? "holds " + parts : "empty") : "not offered: " + disk.reason;
      g.put(x + 2, y + index * 2 + 1, fit(detail, g.width - x - 4), "faint");
    });
    return error(g, y + Math.max(1, disks.length * 2), ctx, name);
  }
  return y + 1;
}

// ---- screens -----------------------------------------------------------------

function welcome(g, ctx) {
  const ui = ctx.ui;
  const spec = ctx.look && ctx.look.torch;
  let torchWidth = 0;
  let animate = null;
  if (spec && g.width >= 72 && g.height >= Torch.MIN_ROWS + 2) {
    const colors = ctx.look.colors[ui.theme] || ctx.look.colors.dark;
    const cells = Torch.frame(spec, Math.min(Torch.MAX_ROWS, g.height - 2), ctx.still ? null : ctx.now, colors);
    torchWidth = cells[0].length + 3;
    const left = g.width - cells[0].length - 2;
    cells.forEach((line, top) => line.forEach((cell, column) => {
      if (cell) g.put(left + column, 1 + top, cell[0], "torch." + cell[1] + "." + (cell[2] || ""), null, cell[3]);
    }));
    if (!ctx.still) animate = Torch.FRAME_MS;
  }
  const width = g.width - torchWidth;
  let y = 1;
  g.line(2, y++, [["thermite", "header.brand+bold"], [" installer", "strong"]]);
  y = paragraph(g, y, "Installs Arch Linux with Thermite on this machine. On its first boot the host sets up "
    + "an empty Thermite house and starts the web app by itself.", "dim", width) + 1;
  const facts = ui.facts;
  if (!facts) return paragraph(g, y, "Reading this machine…", "dim", width) && animate;
  const eligible = facts.disks.filter((disk) => disk.eligible);
  const online = facts.network.interfaces.filter((entry) => entry.addresses.length);
  y = row(g, y, "Thermite", facts.application + (facts.release ? " · release " + facts.release.slice(0, 12) : ""));
  y = row(g, y, "Firmware", facts.uefi ? "UEFI" : "BIOS (legacy boot)");
  if (facts.cpu) y = row(g, y, "Processor", facts.cpu);
  y = row(g, y, "Memory", (facts.memory / 1024 ** 3).toFixed(1) + " GiB");
  y = row(g, y, "Disks", eligible.length ? eligible.map((disk) => disk.name + " " + gigabytes(disk.size)).join(", ")
    : "none can be used", eligible.length ? "base" : "alert");
  y = row(g, y, "Network", online.length ? online.map((entry) => entry.name + " " + entry.addresses[0]).join(", ")
    : "not connected yet");
  if (facts.authorized_key) y = row(g, y, "SSH key", facts.authorized_key.fingerprint || facts.authorized_key.type);
  y += 1;
  facts.warnings.forEach((warning) => { y = paragraph(g, y, "! " + warning, "alert", width); });
  y = paragraph(g, y, "Secure Boot is not supported: turn it off in the firmware if this machine uses it.", "dim", width);
  g.line(2, y + 1, [["Enter", "hotkey"], [" begins. Nothing is written to a disk before the review.", "dim"]]);
  return animate;
}

function network(g, ctx) {
  const ui = ctx.ui;
  let y = 1;
  const state = ui.network || { interfaces: [], online: false };
  state.interfaces.forEach((entry) => {
    const up = entry.addresses.length > 0;
    g.line(2, y++, [[up ? LIT : UNLIT, up ? "led.ok" : "led.off"], [" " + fit(entry.name, 12), "base"],
      [fit((entry.wireless ? "Wi-Fi " : "wired ") + (entry.addresses.join(", ") || "no address"), g.width - 20), "dim"]]);
  });
  if (!state.interfaces.length) g.put(2, y++, "No network interfaces found", "alert");
  y += 1;
  g.line(2, y++, [[state.online ? LIT : UNLIT, state.online ? "led.ok" : "led.bad"],
    [state.online ? " The Internet is reachable." : " The Internet is not reachable yet.", "base"]]);
  y = paragraph(g, y, "The installation downloads Arch Linux packages, and the first boot downloads Thermite's "
    + "runtime and container images, so the host needs the Internet now and after it restarts.", "dim") + 1;
  if (ctx.current.includes("wifi")) {
    g.put(2, y++, "Wi-Fi", "section");
    const active = ctx.focused === "wifi";
    if (!ui.wifi.networks.length) g.put(4, y++, ui.wifi.scanned ? "No networks found" : "Ctrl-R looks for networks", "dim");
    ui.wifi.networks.slice(0, 8).forEach((entry, index) => {
      const on = index === ui.wifi.cursor;
      const style = on && active ? "row.focus" : on ? "row.current" : "row";
      g.put(4, y++, fit((entry.connected ? LIT + " " : "  ") + entry.ssid + "  " + entry.security + "  "
        + "▂▄▆█".slice(0, entry.signal), g.width - 6), style);
    });
    y = field(g, y + 1, ctx, "wifi_passphrase");
  }
  const active = ctx.focused === "continue";
  g.put(2, y + 1, " Continue ", active ? "key.focus" : state.online ? "key" : "key.off");
  return null;
}

function form(intro) {
  return (g, ctx) => {
    let y = paragraph(g, 1, intro(ctx), "dim") + 1;
    ctx.current.forEach((name) => { y = field(g, y, ctx, name) + 1; });
    return null;
  };
}

function hostnameIntro(ctx) {
  const ui = ctx.ui;
  const name = ui.answers.hostname || "thermite";
  const probe = ui.probe;
  const found = !probe ? "Checking this network for the name…"
    : probe.replaced ? probe.replaced + ".local already answers on this network, so " + probe.suggestion + " is suggested."
      : probe.taken === null ? "This network could not be checked for the name."
        : probe.taken ? probe.name + ".local already answers on this network." : "No other host answers to " + probe.name + ".local.";
  return "Other computers reach this host as " + name + ".local. " + found
    + " Renaming it later re-issues the gateway's certificate.";
}

function ownerIntro(ctx) {
  const key = ctx.ui.facts && ctx.ui.facts.authorized_key;
  return "Your own login on the host, with sudo through the wheel group. thermite is reserved for the service "
    + "account Thermite creates. " + (key
    ? "The image's SSH key (" + (key.fingerprint || key.type) + ") becomes this user's key, and SSH stops accepting passwords."
    : "SSH accepts this password until you install a key.");
}

function review(g, ctx) {
  const ui = ctx.ui;
  const answers = ui.answers;
  const disk = ((ui.facts && ui.facts.disks) || []).find((entry) => entry.path === answers.disk);
  let y = 1;
  y = row(g, y, "Host name", answers.hostname + " (" + answers.hostname + ".local)");
  y = row(g, y, "Owner", answers.owner + ", in wheel; password set");
  y = row(g, y, "Root", "password set; console only, never SSH");
  y = row(g, y, "Region", [answers.timezone, answers.locale, answers.keymap + " keyboard"].join(" · "));
  y = row(g, y, "Disk", disk ? describeDisk(disk) : answers.disk || "none");
  y = row(g, y, "Filesystem", answers.filesystem === "btrfs" ? "Btrfs with subvolumes" : "ext4");
  y = row(g, y, "Kernel", answers.kernel + ", with " + (answers.kernel === "linux" ? "linux-lts" : "linux") + " as the fallback");
  if (answers.wifi_ssid) y = row(g, y, "Wi-Fi", answers.wifi_ssid);
  if (ui.facts && ui.facts.authorized_key) y = row(g, y, "SSH key", "installed for " + answers.owner + "; password SSH off");
  y = row(g, y, "First boot", "an empty Thermite house, installed as " + answers.owner);
  y += 1;
  if (disk) {
    y = paragraph(g, y, "Everything on " + disk.path + " (" + [disk.model, gigabytes(disk.size)].filter(Boolean).join(", ")
      + ") will be erased. Type " + disk.name + " to confirm, then press Enter to install.", "alert") + 1;
  }
  field(g, y, ctx, "confirm");
  return null;
}

function install(g, ctx) {
  const progress = ctx.ui.progress || { percent: 0, label: "", phases: [], lines: [] };
  let y = 1;
  const bar = Math.max(10, g.width - 12);
  const filled = Math.round(bar * progress.percent / 100);
  for (let index = 0; index < bar; index += 1) {
    g.put(2 + index, y, index < filled ? "█" : "─",
      index < filled ? "meter." + Math.floor(index * (METER - 1) / Math.max(1, bar - 1)) : "meter.empty");
  }
  g.put(3 + bar, y++, String(progress.percent).padStart(3) + "%", "meter.value");
  g.put(2, y++, fit(progress.label || "Starting", g.width - 4), "strong");
  y += 1;
  const seen = [];
  progress.phases.forEach((phase) => {
    if (!seen.includes(phase.label)) seen.push(phase.label);
  });
  seen.forEach((label, index) => {
    const done = index < seen.length - 1 || progress.percent === 100;
    g.line(2, y++, [[done ? LIT : "◐", done ? "led.ok" : "led.busy"], [" " + fit(label, g.width - 6), done ? "dim" : "base"]]);
  });
  logTail(g, y + 1, progress.lines);
  return null;
}

function logTail(g, y, lines) {
  const room = g.height - y - 1;
  if (room < 3) return;
  box(g, { x: 1, y: y, w: g.width - 2, h: room + 1 }, { border: "border.log", title: "log", titleStyle: "title.log" });
  lines.slice(-(room - 1)).forEach((line, index) => g.put(3, y + 1 + index, fit(line, g.width - 6), "dim"));
}

function done(g, ctx) {
  const ui = ctx.ui;
  const answers = ui.answers;
  let y = 1;
  if (ui.result) {
    g.line(2, y++, [[LIT, "led.ok"], [" Thermite is installed on " + answers.hostname + ".", "strong"]]);
    y += 1;
    y = paragraph(g, y, "Remove the installation medium, then press Enter to restart.", "base") + 1;
    y = paragraph(g, y, "On its first boot the host initializes an empty Thermite house and installs it as "
      + answers.owner + ". That takes a while and needs the Internet. Log in as " + answers.owner
      + " on the console or with ssh " + answers.owner + "@" + answers.hostname + ".local: the login shows its "
      + "progress, then the web app's address.") + 1;
    paragraph(g, y, "This run's answers, without passwords, and its logs are kept in /var/log/thermite-install.");
    return null;
  }
  g.line(2, y++, [[LIT, "led.bad"], [" The installation stopped.", "strong"]]);
  y = paragraph(g, y + 1, ui.failure || "", "alert") + 1;
  y = paragraph(g, y, "The logs are /run/thermite-installer/installer.log and /var/log/archinstall/install.log on this "
    + "live system. Esc returns to the review to try again; Ctrl-C leaves for a shell.") + 1;
  logTail(g, y, (ui.progress && ui.progress.lines) || []);
  return null;
}

const SCREENS = {
  welcome: welcome,
  network: network,
  hostname: form(hostnameIntro),
  owner: form(ownerIntro),
  root: form(() => "For the console and emergency mode only: root never logs in over SSH. "
    + "Use a different password from the owner's."),
  region: form(() => "The host's time zone, language and console keyboard."),
  disk: form(() => "Thermite takes the whole disk: everything on it is erased. A 1 GiB boot partition comes "
    + "first, then the system."),
  system: form(() => "Btrfs keeps the system, homes, logs and package cache in subvolumes, and Thermite's state "
    + "in its own. Both kernels are installed, so the other one can still boot if the default fails."),
  review: review,
  install: install,
  done: done
};

module.exports = { render, STEPS_WIDTH };
