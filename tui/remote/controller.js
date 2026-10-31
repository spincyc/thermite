/*
 * The terminal remote's state and input, without a terminal: it keeps what
 * is on the screen (the screen, a sheet, the focus, toasts, the log, scroll
 * offsets) and turns keys and mouse events into RemoteSession and
 * PairingBackend calls, as remote.js and pair.js do for the page. app.js
 * draws its frame() and feeds it key() and mouse(); the tests call the
 * same three.
 *
 * The views get the time (ctx.now, ms, from options.now) and whether
 * motion is off (ctx.still: --still, THERMITE_REMOTE_STILL, or the a key).
 * A drawer that moves asks for its next frame with
 * ctx.animate = Math.min(ctx.animate || Infinity, ms); frame() hands the
 * least of those on as frame.animate, null when nothing moves or a sheet
 * covers the panels.
 */
"use strict";

const View = require("./view");
const Topology = require("./topology");
const Access = require("./administration");

const TOAST_MS = 1900;
const FLASH_MS = 140;
const TICK_MS = 500;
const LOG_KEEP = 200;
// A mouse button held on a repeating key presses at most this long: a
// terminal that loses the release must not run the volume to its limit.
const REPEAT_LIMIT_MS = 8000;
// Blessed key names for the page's KeyboardEvent.key names in RemoteSession.KEYS.
const KEY_NAMES = { up: "ArrowUp", down: "ArrowDown", left: "ArrowLeft", right: "ArrowRight", backspace: "Backspace", escape: "Escape" };
// The keys a focused volume slider takes, as a focused range input does on the page.
const RANGE = { up: 1, right: 1, down: -1, left: -1, pageup: "page", pagedown: "-page", home: "min", end: "max" };
const CHARS = { digits: /^[0-9]$/, hex: /^[0-9A-Fa-f]$/ };

function create(options) {
  const session = options.session;
  const Session = options.Session;
  const Pairing = options.Pairing;
  const store = options.store;
  const now = options.now || (() => Date.now());
  const later = options.setTimeout || ((callback, ms) => setTimeout(callback, ms));
  const cancel = options.clearTimeout || ((handle) => clearTimeout(handle));
  const onChange = options.onChange || (() => {});
  const onQuit = options.onQuit || (() => {});
  const hints = Session.keyHints();

  const ui = {
    screen: ["home", "remote", "security", "cameras", "topology", "pairing", "settings", "users", "account"].includes(options.screen) ? options.screen : "home",
    overlay: null,
    focus: null,
    focusVisible: false,
    revealFocus: true,
    pane: null,
    paneFocus: {},
    sourceCursor: {},
    revealRoom: true,
    pressed: {},
    toast: null,
    announce: "",
    log: [],
    logVisible: options.logVisible != null ? Boolean(options.logVisible) : store.get("remote.logVisible") !== false,
    scroll: {},
    bubbles: {},
    blink: false,
    theme: options.theme || store.get("remote.theme") || "dark",
    still: options.still != null ? Boolean(options.still) : store.get("remote.still") === true,
    clock: clock(),
    target: options.target || { label: "", mock: true },
    connected: null,
    error: null,
    ready: false
  };
  const access = Access.create(options.access, options.house, changed);
  const pairing = { backend: null, selected: null, codes: {}, alert: null, loaded: false, mock: Boolean(ui.target.mock), notes: {}, announce: "", sortField: "room", sortDescending: false };
  const cameras = { backend: null, selected: null, link: null };
  const topology = { backend: null, room: null, source: null, link: null };
  const timers = { toast: 0, flash: {}, repeat: null, tick: 0 };
  let frame = null;
  let size = { width: 80, height: 24 };
  let dragging = null;
  let stopped = false;
  let signalRunning = false;
  let signalContext = null;

  function syncDiagnostics() {
    if (!session.openDiagnostics) return;
    const visible = ui.ready && ui.screen === "remote" && (ui.overlay ? ui.overlay.kind === "signal"
      : View.signalInline(size.width, size.height, ui.logVisible !== false));
    if (visible && !signalRunning) {
      signalRunning = true;
      session.openDiagnostics().then(changed);
    } else if (!visible && signalRunning) {
      signalRunning = false;
      session.closeDiagnostics();
    }
  }

  function changed() {
    if (!stopped) onChange();
  }

  function clock(at) {
    const date = new Date(at == null ? now() : at);
    const two = (number) => String(number).padStart(2, "0");
    return two(date.getHours()) + ":" + two(date.getMinutes()) + ":" + two(date.getSeconds());
  }

  function context() {
    if (options.backend && "connected" in options.backend) ui.connected = options.backend.connected;
    return {
      session: session, Session: Session, Remote: options.Remote, layouts: options.layouts, house: options.house,
      Pairing: Pairing, hints: hints, ui: ui, access, pairing: pairing, cameras: cameras, topology: topology, look: options.look || null,
      now: now(), still: ui.still, animate: undefined
    };
  }

  // ---- start and stop --------------------------------------------------

  function start(roomId, chosen) {
    if (stopped) return Promise.resolve();
    session.subscribe(onSession);
    if (options.backend && options.backend.subscribe) {
      options.backend.subscribe((event) => {
        if (event.type === "connection") changed();
      });
    }
    timers.tick = (options.setInterval || setInterval)(() => {
      ui.blink = !ui.blink;
      ui.clock = clock();
      changed();
    }, TICK_MS);
    return Promise.resolve(options.access ? options.access.ready() : null).then(() => { if (ui.screen === "pairing") openPairing(); return session.start(roomId); })
      .then(() => {
        if (stopped) return;
        ui.ready = true;
        ui.pane = ui.screen === "pairing" ? "pair-list" : ui.screen === "remote" ? "rooms" : ui.screen;
        ui.focus = ui.screen === "remote" ? "room:" + session.current().id : ui.screen === "home" ? "home:remote" : null;
        ui.focusVisible = true;
        if (ui.screen === "cameras") openCameras();
        if (ui.screen === "topology") openTopology();
        if (["settings", "users", "account"].includes(ui.screen)) access.open(ui.screen, session.current()?.id);
        changed();
        if (chosen) session.choose(chosen);
      })
      .catch((error) => {
        if (stopped) return;
        ui.error = "The remote could not start: " + String((error && error.message) || error);
        if (options.access && !options.access.native) { ui.screen = "account"; ui.overlay = null; ui.pane = "account"; access.state.kind = "account"; access.state.error = ui.error; }
        changed();
      });
  }

  function stop() {
    stopped = true;
    (options.clearInterval || clearInterval)(timers.tick);
    cancel(timers.toast);
    Object.keys(timers.flash).forEach((key) => cancel(timers.flash[key]));
    stopRepeat();
    if (pairing.backend) pairing.backend.stop();
    if (cameras.backend) cameras.backend.stop();
    if (topology.backend) topology.backend.stop();
    if (signalRunning && session.closeDiagnostics) session.closeDiagnostics();
    signalRunning = false;
    if (options.backend && options.backend.stop) options.backend.stop();
  }

  function quit() {
    stop();
    onQuit();
  }

  // ---- what the session and the pairing backend say ----------------------

  function log(entry) {
    ui.log.push(Object.assign({ time: clock() }, entry));
    if (ui.log.length > LOG_KEEP) ui.log.splice(0, ui.log.length - LOG_KEEP);
  }

  function activity(event) {
    if (!["command", "state", "problem", "connection"].includes(event.kind) || !Number.isFinite(event.at)
        || !Number.isFinite(new Date(event.at).getTime())) return;
    const plain = (value) => typeof value === "string"
      ? value.replace(/[\u0000-\u001f\u007f-\u009f]/g, " ").replace(/\s+/g, " ").trim() : "";
    const title = plain(event.title), body = plain(event.body);
    if (!title && !body) return;
    log({ time: clock(event.at), title: [plain(event.room), plain(event.device)].filter(Boolean).join(" \u00b7 ") || "House",
      tag: event.kind, body: title + (title && body ? ": " : "") + body,
      tone: event.tone === "bad" || event.tone === "muted" ? event.tone : "" });
  }

  function toast(title, body, tone, tag) {
    ui.toast = { title: title, body: body || "", tone: tone || "", tag: tag || "" };
    cancel(timers.toast);
    timers.toast = later(() => {
      ui.toast = null;
      changed();
    }, TOAST_MS);
    changed();
  }

  function onSession(event) {
    if (event.type === "access" && event.unauthorized) {
      ui.log = []; ui.toast = null; ui.announce = ""; ui.error = "Your sign-in has ended. Run thermite access login again.";
      pairing.codes = {}; pairing.notes = {}; if (pairing.backend) pairing.backend.stop(); pairing.backend = null; pairing.loaded = false;
      if (cameras.backend) cameras.backend.stop();
      access.cancel(); access.state.users = []; access.state.roles = []; access.state.policy = null;
      access.state.kind = "account"; access.state.error = ui.error; ui.screen = "account"; ui.overlay = null; ui.pane = "account"; ui.focus = null;
    }
    if (event.type === "activity") {
      activity(event);
      changed();
      return;
    }
    if (event.type === "toast") {
      toast(event.title, event.body, event.tone, event.tag);
      return;
    }
    if (event.type === "announce") {
      ui.announce = event.message;
    } else if (event.type === "inputs") {
      if (ui.screen === "remote") openSheet("inputs", event.title);
      return;
    }
    changed();
  }

  function onPairing(event) {
    const backend = pairing.backend;
    if (event.type === "list") {
      pairing.alert = null;
      pairing.loaded = true;
      if (!backend.device(pairing.selected)) {
        const first = View.pairDevices(context())[0];
        pairing.selected = first ? first.id : null;
      }
    } else if (event.type === "device") {
      const device = backend.device(event.device);
      const view = backend.view(event.device);
      if (device && view && view.note && view.note !== pairing.notes[event.device]) {
        speak(device, view.note, view.noteTone === "bad" ? "bad" : "");
      }
      pairing.notes[event.device] = view ? view.note : null;
      if (view && !view.form) delete pairing.codes[event.device];
    } else if (event.type === "status") {
      const device = backend.device(event.device);
      // A status that comes with a note (Paired, not accepted) is logged by the note.
      if (device && !backend.view(event.device).note) speak(device, Pairing.STATES[event.status] + ".", "");
    } else if (event.type === "error") {
      pairing.alert = event.message;
      pairing.loaded = true;
    }
    changed();
  }

  // What pair.js's page has its screen reader say, for the status line and the log.
  function speak(device, text, tone) {
    pairing.announce = device.label + ", " + device.room + ": " + text;
  }

  function openPairing() {
    if (options.access && !options.access.native && options.access.current() && !options.access.has("pairing.manage")) {
      pairing.alert = "Only administrators can pair devices."; pairing.loaded = true; changed(); return;
    }
    if (pairing.backend) {
      pairing.backend.refresh().catch(() => null);
      return;
    }
    const backend = options.createPairing();
    pairing.backend = backend;
    backend.subscribe(event => { if (pairing.backend === backend && ui.screen === "pairing") onPairing(event); });
    backend.start().catch(() => null);
  }

  function openCameras() {
    if (!cameras.backend) {
      cameras.backend = options.createCameras();
      cameras.backend.subscribe(() => {
        if (!cameras.backend.camera(cameras.selected)) cameras.selected = (cameras.backend.list()[0] || {}).id || null;
        if (ui.screen === "cameras" || ui.overlay && ui.overlay.kind === "cameras") {
          if (!["camera:" + cameras.selected, "camera-open"].includes(ui.focus)) {
            ui.focus = cameras.selected ? "camera:" + cameras.selected : null;
          }
          changed();
        }
      });
    }
    cameras.backend.open().then(changed);
  }

  function stepCamera(delta) {
    const list = cameras.backend ? cameras.backend.list() : [];
    if (!list.length) return;
    const index = Math.max(0, list.findIndex((camera) => camera.id === cameras.selected));
    cameras.selected = list[(index + delta + list.length) % list.length].id;
    cameras.link = null;
    ui.focus = "camera:" + cameras.selected;
    ui.focusVisible = true;
    ui.revealFocus = true;
    changed();
  }

  function openCamera() {
    if (!cameras.selected || !options.openCamera) return;
    const selected = cameras.selected;
    options.openCamera(selected).then((result) => {
      if (ui.screen !== "cameras" && !(ui.overlay && ui.overlay.kind === "cameras")) return;
      if (cameras.selected === selected) cameras.link = result.url;
      toast("Camera web view", result.opened ? "Opened in your browser" : result.url);
    }).catch(() => toast("Camera web view", "Open Cameras from the web remote.", "bad"));
  }

  // ---- the frame -------------------------------------------------------

  function render(width, height) {
    size = { width: width, height: height };
    if (access.state.form && ui.overlay?.kind !== "access-form") {
      ui.returnPane = ui.pane; ui.overlay = { kind: "access-form", title: access.state.form.kind === "policy" ? "Room schedule" : (access.state.form.id ? "Edit " : "Add ") + access.state.form.kind }; ui.pane = "sheet:access-form"; ui.focus = null;
    } else if (!access.state.form && ui.overlay?.kind === "access-form") { ui.overlay = null; ui.pane = ui.screen; ui.focus = null; }
    const current = ui.ready && session.current();
    const nextContext = current ? current.id + ":" + session.state(current.id).value : null;
    if (nextContext !== signalContext) {
      ui.scroll.signal = ui.scroll["sheet:signal"] = 0;
      signalContext = nextContext;
    }
    const inline = ui.screen === "remote" && View.signalInline(width, height, ui.logVisible !== false);
    if (ui.ready && inline && ui.overlay && ui.overlay.kind === "signal") {
      ui.overlay = null; ui.pane = "signal"; ui.focus = null; ui.focusVisible = true;
    } else if (ui.ready && ui.screen === "remote" && !inline && !ui.overlay && ui.pane === "signal") {
      ui.overlay = { kind: "signal", title: "" }; ui.pane = "sheet:signal"; ui.returnPane = "remote";
    }
    syncDiagnostics();
    let ctx = context();
    frame = View.render(ctx, width, height);
    if (ui.ready) {
      // Rooms remain visible and usable when a narrow terminal replaces the
      // inline list with its existing sheet.
      if (!ui.overlay && ui.pane === "rooms" && !frame.regions.some((region) => region.key === "rooms")) {
        ui.overlay = { kind: "rooms", title: "" };
        ui.pane = "sheet:rooms";
        frame = View.render(context(), width, height);
      }
      const panes = visiblePanes();
      if (!panes.includes(ui.pane)) enterPane(panes.includes("remote") ? "remote" : panes[0]);
      else if (!ui.focus || !frame.focusables.some((hit) => hit.key === ui.focus && hit.pane === ui.pane)) enterPane(ui.pane);
      ctx = context();
      frame = View.render(ctx, width, height);
    }
    frame.animate = !ui.overlay && !ui.still && Number.isFinite(ctx.animate) && ctx.animate > 0 ? ctx.animate : null;
    ui.scroll = frame.scroll;
    ui.bubbles = frame.bubbles;
    if (ui.focus && !frame.focusables.some((hit) => hit.key === ui.focus)) {
      ui.focus = null;
    }
    return frame;
  }

  function focusedHit() {
    if (!frame || !ui.focus) return null;
    return frame.focusables.find((hit) => hit.key === ui.focus) || null;
  }

  function visiblePanes() {
    const sheet = ui.overlay ? "sheet:" + ui.overlay.kind : null;
    return frame ? frame.regions.filter((region) => !sheet || region.key === sheet).map((region) => region.key) : [];
  }

  function enterPane(pane) {
    if (!pane) return;
    if (ui.pane && ui.focus) ui.paneFocus[ui.pane] = ui.focus;
    ui.pane = pane;
    const list = frame.focusables.filter((hit) => hit.pane === pane);
    const remembered = list.find((hit) => hit.key === ui.paneFocus[pane]);
    let hit = remembered;
    if (pane === "rooms" || pane === "sheet:rooms") hit = list.find((entry) => entry.room === session.current().id);
    else if (pane === "pair-list") hit = list.find((entry) => entry.device === pairing.selected);
    else if (pane === "cameras") hit = list.find(entry => entry.camera === cameras.selected) || hit;
    else if (pane === "home") hit = list.find(entry => entry.key === ui.focus) || hit || list[0];
    else if (pane === "sources") hit = list.find((entry) => entry.item && entry.item.id === ui.sourceCursor[session.current().id])
      || hit || list.find((entry) => entry.item && entry.item.id === session.state(session.current().id).value);
    else if (pane === "remote" && !hit) hit = list.find((entry) => entry.key === "key:ok") || list.find((entry) => entry.kind === "key");
    else if (pane === "pair-card" && !hit) hit = list.find((entry) => entry.key === "code");
    if (["info", "signal", "log", "sheet:info", "sheet:help"].includes(pane)) hit = null;
    ui.focus = hit ? hit.key : list.length && !["info", "signal", "log", "sheet:info", "sheet:help"].includes(pane) ? list[0].key : null;
    ui.focusVisible = true;
    ui.revealFocus = true;
  }

  function movePane(delta) {
    render(size.width, size.height);
    const list = visiblePanes();
    if (!list.length) return;
    const index = list.indexOf(ui.pane);
    enterPane(list[(index + delta + list.length) % list.length]);
    changed();
  }

  function moveControl(delta) {
    const list = frame.focusables.filter((hit) => hit.pane === ui.pane);
    const index = list.findIndex((hit) => hit.key === ui.focus);
    if (list.length) ui.focus = list[Math.max(0, Math.min(list.length - 1, index + delta))].key;
    ui.revealFocus = true;
    changed();
  }

  // Rooms are a live selection: every arrow changes the rest of the remote.
  // Keep arrows within the room list rather than reaching All off or Pairing.
  function stepRoomFocus(delta, keepPane) {
    const rooms = session.rooms();
    if (!rooms.length) return;
    const hit = focusedHit();
    const id = !keepPane && hit && hit.kind === "room" ? hit.room : session.current().id;
    const index = rooms.findIndex((room) => room.id === id);
    const next = rooms[(index + delta + rooms.length) % rooms.length];
    session.selectRoom(next.id, true);
    ui.sourceCursor[next.id] = session.state(next.id).value;
    ui.paneFocus.sources = "bubble:" + ui.sourceCursor[next.id];
    ui.paneFocus.rooms = "room:" + next.id;
    if (!keepPane || ui.pane === "rooms") ui.focus = "room:" + next.id;
    else if (ui.pane === "sources") ui.focus = "bubble:" + (ui.sourceCursor[next.id] || session.state(next.id).value);
    ui.focusVisible = true;
    ui.revealFocus = true;
    ui.revealRoom = true;
    changed();
  }

  function focusRemote() {
    if (ui.overlay && ui.overlay.kind === "rooms") ui.overlay = null;
    render(size.width, size.height);
    enterPane("remote");
    changed();
  }

  function stepSourceFocus(delta) {
    const list = session.bubbles(session.controls());
    if (!list.length) return;
    const room = session.current().id;
    const focused = ui.pane === "sources" ? focusedHit() : null;
    const id = focused && focused.item ? focused.item.id : ui.sourceCursor[room] || session.state(room).value;
    const index = list.findIndex((candidate) => candidate.id === id);
    const start = index < 0 ? delta > 0 ? -1 : 0 : index;
    const next = list[(start + delta + list.length) % list.length];
    ui.sourceCursor[room] = next.id;
    ui.paneFocus.sources = "bubble:" + next.id;
    if (ui.pane === "sources") ui.focus = "bubble:" + next.id;
    ui.focusVisible = true;
    ui.revealFocus = true;
    changed();
  }

  function applySourceCursor() {
    if (session.controlAllowed && !session.controlAllowed()) return toast("Read-only", "Account permission or room schedule");
    const room = session.current().id;
    const list = session.bubbles(session.controls());
    const id = ui.sourceCursor[room] || session.state(room).value;
    const item = list.find((entry) => entry.id === id);
    if (!item) return;
    ui.sourceCursor[room] = item.id;
    ui.paneFocus.sources = "bubble:" + item.id;
    session.tapBubble(item);
    changed();
  }

  function flash(key) {
    ui.pressed[key] = true;
    cancel(timers.flash[key]);
    timers.flash[key] = later(() => {
      delete ui.pressed[key];
      delete timers.flash[key];
      changed();
    }, FLASH_MS);
    changed();
  }

  // ---- sheets and screens ----------------------------------------------

  function openSheet(kind, title) {
    stopRepeat();
    if (kind === "cameras") return setScreen("cameras");
    if (kind === "topology") return setScreen("topology");
    if (kind === "signal" && ui.screen === "remote" && View.signalInline(size.width, size.height, ui.logVisible !== false)) {
      ui.overlay = null;
      render(size.width, size.height);
      enterPane("signal"); changed(); return;
    }
    if (ui.overlay && ui.overlay.kind === "cameras" && kind !== "cameras" && cameras.backend) cameras.backend.close();
    if (ui.overlay && ui.overlay.kind === "topology" && kind !== "topology" && topology.backend) topology.backend.close();
    ui.returnPane = ui.pane;
    ui.overlay = { kind: kind, title: title || "" };
    ui.pane = "sheet:" + kind;
    ui.scroll["sheet:" + kind] = 0;
    ui.focusVisible = kind !== "help";
    ui.focus = null;
    if (kind === "rooms" && session.current()) ui.focus = "room:" + session.current().id;
    if (kind === "inputs") {
      const options = session.inputOptions();
      const used = options.find((entry) => entry.inUse) || options[0];
      ui.focus = used ? "input:" + used.value : null;
    }
    render(size.width, size.height);
    if (!ui.focus && ui.focusVisible && frame.focusables.length) ui.focus = frame.focusables[0].key;
    changed();
    if (kind === "cameras") { ui.revealFocus = true; openCameras(); }
  }

  function openTopology() {
    if (options.createTopology) {
      if (!topology.backend) topology.backend = options.createTopology();
      if (!topology.room) topology.room = session.current() && session.current().id;
      topology.source = null;
      topology.link = null;
      topology.backend.open().then(() => {
        if (stopped || ui.screen !== "topology") return;
        stepTopology(null, 0); changed();
      });
    }
  }

  function stepTopology(axis, delta) {
    const manifest = topology.backend && topology.backend.state().manifest;
    if (!manifest || !manifest.rooms.length) return;
    const rooms = manifest.rooms;
    let index = rooms.findIndex((room) => room.id === topology.room);
    if (index < 0) index = 0;
    if (axis === "room") { index = (index + delta + rooms.length) % rooms.length; topology.source = null; }
    topology.room = rooms[index].id;
    topology.link = null;
    const sources = Topology.orderedRows(Topology.routeRows(manifest, topology.room, session.state(topology.room) || {}, ui.connected),
      topology.sortField, topology.sortDescending).map(row => row.id);
    if (!sources.includes(topology.source)) {
      const reading = session.state(topology.room);
      topology.source = reading && sources.includes(reading.value) ? reading.value : sources[0] || null;
    }
    if (axis === "source" && sources.length) topology.source = sources[(sources.indexOf(topology.source) + delta + sources.length) % sources.length];
    ui.scroll[ui.screen === "topology" ? "topology" : "sheet:topology"] = 0;
    changed();
  }

  function closeSheet() {
    if (ui.overlay && ui.overlay.kind === "cameras" && cameras.backend) cameras.backend.close();
    if (ui.overlay && ui.overlay.kind === "topology" && topology.backend) topology.backend.close();
    ui.overlay = null;
    ui.focus = null;
    ui.pane = ui.returnPane || (ui.screen === "pairing" ? "pair-list" : "remote");
    ui.focusVisible = true;
    render(size.width, size.height);
    changed();
  }

  function setScreen(name) {
    access.cancel();
    if (stopped) return;
    if (ui.screen === name && !ui.overlay) return;
    stopRepeat();
    if (dragging) { dragging = null; session.setDragging(false); }
    if (ui.screen === "pairing" && name !== "pairing" && pairing.backend) {
      pairing.backend.stop();
      pairing.backend = null;
      pairing.loaded = false;
      pairing.codes = {};
      pairing.notes = {};
      pairing.alert = null;
    }
    if (ui.screen === "cameras" && cameras.backend) cameras.backend.close();
    if (ui.screen === "topology" && topology.backend) topology.backend.close();
    if (ui.overlay && ui.overlay.kind === "cameras" && cameras.backend) cameras.backend.close();
    if (ui.overlay && ui.overlay.kind === "topology" && topology.backend) topology.backend.close();
    ui.screen = name;
    ui.overlay = null;
    ui.focus = null;
    ui.pane = name === "pairing" ? "pair-list" : name === "remote" ? "rooms" : name;
    if (name === "home") ui.focus = "home:remote";
    ui.focusVisible = true;
    syncDiagnostics();
    if (name === "pairing") openPairing();
    if (name === "cameras") openCameras();
    if (name === "topology") openTopology();
    if (["settings", "users", "account"].includes(name)) access.open(name, session.current()?.id);
    render(size.width, size.height);
    changed();
  }

  function toggleTheme() {
    ui.theme = ui.theme === "dark" ? "light" : "dark";
    store.set("remote.theme", ui.theme);
    changed();
  }

  function toggleStill() {
    ui.still = !ui.still;
    store.set("remote.still", ui.still);
    changed();
  }

  function toggleLog() {
    ui.logVisible = !ui.logVisible;
    store.set("remote.logVisible", ui.logVisible);
    changed();
  }

  // ---- the controls ----------------------------------------------------

  function activate(hit) {
    // A page switch can outrun a terminal redraw. Old control hit boxes must
    // not operate a device once its page has been left.
    if (["bubble", "key", "volume-mode", "control", "input", "all-off", "simulate-matrix", "simulate-external", "scroll-bubbles"].includes(hit.kind)
        && ui.screen !== "remote") return;
    if (hit.kind.startsWith("pair-") || ["send-code", "new-code", "code"].includes(hit.kind)) {
      if (ui.screen !== "pairing") return;
    }
    if (session.controlAllowed && !session.controlAllowed() && (["key", "control", "input"].includes(hit.kind) || hit.kind === "bubble" && hit.item?.choose)) {
      return toast("Read-only", "Account permission or room schedule");
    }
    if (hit.kind === "access-action" || hit.kind === "access-field") {
      if (hit.action === "settings" || hit.action === "users") setScreen(hit.action);
      else { access.activate(hit); changed(); }
      return;
    }
    switch (hit.kind) {
      case "pair-sort":
        if (!pairing.backend || pairing.backend.view(pairing.selected)?.confirm || !View.PAIR_FIELDS.includes(hit.field)) return;
        pairing.sortDescending = pairing.sortField === hit.field ? !pairing.sortDescending : false;
        pairing.sortField = hit.field;
        ui.scroll["pair-list"] = 0;
        break;
      case "topology-sort":
        if (ui.screen !== "topology") return;
        Topology.sortBy(topology, hit.field);
        ui.scroll.topology = 0;
        break;
      case "topology-preview": {
        if (ui.screen !== "topology") return;
        const manifest = topology.backend && topology.backend.state().manifest;
        if (!manifest || !Object.prototype.hasOwnProperty.call(manifest.routes[topology.room] || {}, hit.source)) return;
        topology.source = hit.source;
        topology.link = null;
        break;
      }
      case "home-room":
        session.selectRoom(hit.room, true);
        setScreen("remote");
        break;
      case "room":
        session.selectRoom(hit.room, true);
        if (ui.overlay && ui.overlay.kind === "rooms") closeSheet();
        break;
      case "all-off":
        if (options.access && !options.access.has("room.control")) return toast("Read-only", "Your account cannot control rooms");
        if (session.allOff() && ui.overlay) closeSheet();
        break;
      case "screen":
        setScreen(hit.screen);
        break;
      case "open":
        openSheet(hit.sheet);
        break;
      case "theme":
        toggleTheme();
        break;
      case "camera":
        cameras.selected = hit.camera;
        cameras.link = null;
        ui.focus = "camera:" + hit.camera;
        break;
      case "camera-open":
        openCamera();
        break;
      case "bubble":
        session.tapBubble(hit.item);
        break;
      case "scroll-bubbles":
        ui.revealFocus = false;
        ui.bubbles.offset = (ui.bubbles.offset || 0) + hit.step * (ui.bubbles.step || 16);
        break;
      case "key":
        session.press(hit.button);
        flash(hit.key);
        break;
      case "volume-mode":
        session.toggleVolumeMode();
        break;
      case "control":
        session.operate(hit.control, hit.action);
        break;
      case "input":
        session.chooseInput(hit.value);
        closeSheet();
        break;
      case "simulate-matrix":
        session.toggleMatrix();
        break;
      case "simulate-external":
        session.simulateExternal();
        break;
      case "dismiss":
        closeSheet();
        break;
      case "pair-device":
        pairing.selected = hit.device;
        ui.scroll["pair-card"] = 0;
        break;
      case "pair-step":
        stepDevice(hit.step);
        break;
      case "pair-request":
        requestCode(hit.device);
        break;
      case "code":
        ui.focus = "code";
        break;
      case "send-code":
        sendCode(hit.device);
        break;
      case "new-code":
        pairing.codes[hit.device] = "";
        requestCode(hit.device);
        break;
      case "pair-confirm":
        if (!pairing.backend.view(hit.device).busy) pairing.backend.confirmAgain(hit.device).catch(() => null);
        if (ui.focus === "pair-confirm") ui.focus = null;
        break;
      case "pair-keep":
        pairing.backend.keepPairing(hit.device);
        if (ui.focus === "pair-keep") ui.focus = null;
        break;
      case "pair-cancel":
        pairing.codes[hit.device] = "";
        if (!pairing.backend.view(hit.device).busy) pairing.backend.cancelAgain(hit.device).catch(() => null);
        if (ui.focus === "pair-cancel") ui.focus = null;
        break;
      default:
        return;
    }
    changed();
  }

  function pressKey(candidates) {
    if (session.controlAllowed && !session.controlAllowed()) return toast("Read-only", "Account permission or room schedule");
    const button = session.pressKey(candidates);
    flash("key:" + button);
  }

  function startRepeat(hit) {
    stopRepeat();
    const started = now();
    session.press(hit.button);
    flash(hit.key);
    timers.repeat = later(function tick() {
      if (stopped || ui.screen !== "remote" || ui.overlay || now() - started > REPEAT_LIMIT_MS) {
        stopRepeat();
        return;
      }
      session.press(hit.button);
      flash(hit.key);
      timers.repeat = later(tick, Session.REPEAT_EVERY);
    }, Session.REPEAT_DELAY);
  }

  function stopRepeat() {
    if (timers.repeat) cancel(timers.repeat);
    timers.repeat = null;
  }

  function slideBy(name) {
    if (session.controlAllowed && !session.controlAllowed()) return;
    const view = session.audioView();
    if (view.disabled) return;
    const step = RANGE[name];
    let value = view.value;
    if (step === "min") value = view.min || 0;
    else if (step === "max") value = view.max;
    else if (step === "page") value += view.max / 10;
    else if (step === "-page") value -= view.max / 10;
    else value += step * (view.step || 1);
    session.slide(options.Remote.volumeValue({ max: view.max, native: { min: view.min, step: view.step } }, value), true);
  }

  function sliderValue(entry, x) {
    const fraction = Math.max(0, Math.min(1, (x - entry.x) / Math.max(1, entry.w - 1)));
    return options.Remote.volumeValue({ max: entry.hit.max, native: { min: entry.hit.min, step: entry.hit.step } },
      (entry.hit.min || 0) + fraction * (entry.hit.max - (entry.hit.min || 0)));
  }

  // ---- the Pairing screen ----------------------------------------------

  function selectedDevice() {
    return pairing.backend && pairing.selected ? pairing.backend.device(pairing.selected) : null;
  }

  function stepDevice(step, keyboard) {
    const devices = View.pairDevices(context());
    if (!devices.length) return;
    const index = devices.findIndex((device) => device.id === pairing.selected);
    pairing.selected = devices[(Math.max(0, index) + step + devices.length) % devices.length].id;
    ui.scroll["pair-card"] = 0;
    if (keyboard) {
      ui.focus = "pair-device:" + pairing.selected;
      ui.focusVisible = true;
    }
    changed();
  }

  function requestCode(id) {
    const view = pairing.backend.view(id);
    if (!view || view.busy) return;
    // Pair again may first ask its question (view.confirm), answered by its own two buttons.
    (view.action === "again" ? pairing.backend.again(id) : pairing.backend.request(id)).catch(() => null);
  }

  function sendCode(id) {
    const device = pairing.backend.device(id);
    const code = pairing.codes[id] || "";
    if (!device || !Pairing.codeOk(device, code) || pairing.backend.view(id).busy) return;
    pairing.codes[id] = "";
    if (ui.focus === "code" || ui.focus === "send-code") ui.focus = null;
    pairing.backend.sendCode(id, code).catch(() => null);
  }

  function pairingKey(ch, key) {
    const name = key.name;
    const device = selectedDevice();
    const view = device ? pairing.backend.view(device.id) : null;
    if ((ch === "s" || ch === "S") && !key.ctrl && !key.meta && ui.pane === "pair-list" && ui.focus !== "code" && !(view && view.confirm)) {
      if (ch === "S" || key.shift) pairing.sortDescending = !pairing.sortDescending;
      else pairing.sortField = View.PAIR_FIELDS[(View.PAIR_FIELDS.indexOf(pairing.sortField) + 1) % View.PAIR_FIELDS.length];
      ui.scroll["pair-list"] = 0;
      changed();
      return;
    }
    if (name === "pageup" || name === "pagedown") return pageScroll(name === "pageup" ? -1 : 1);
    if (name === "escape") {
      if (view && view.confirm) {
        pairing.backend.keepPairing(device.id);
        changed();
      } else {
        setScreen("remote");
      }
      return;
    }
    const hit = ui.focusVisible ? focusedHit() : null;
    if (name === "up" || name === "down") {
      const step = name === "up" ? -1 : 1;
      if (ui.pane === "log") return scroll("log", -step);
      if (ui.pane === "pair-card" && hit) return moveControl(step);
      stepDevice(step, true);
      return;
    }
    if (["left", "right"].includes(name)) return;
    if (ui.pane === "log") return;
    if (name === "space" && hit && hit.kind !== "code") {
      activate(hit);
      return;
    }
    if (name === "enter") {
      if (ui.pane === "pair-card" && hit && hit.kind !== "code") activate(hit);
      else if (view && view.form) sendCode(device.id);
      else if (view && view.action) requestCode(device.id);
      // A question (view.confirm) is answered only by its buttons, never by Enter alone.
      changed();
      return;
    }
    if (view && view.form) {
      const code = pairing.codes[device.id] || "";
      if (name === "backspace") {
        pairing.codes[device.id] = code.slice(0, -1);
        changed();
        return;
      }
      if (ch && CHARS[device.chars] && CHARS[device.chars].test(ch)) {
        if (code.length < device.length) pairing.codes[device.id] = code + (device.chars === "hex" ? ch.toUpperCase() : ch);
        changed();
        return;
      }
    }
    if (ch === "t" || ch === "T") toggleTheme();
    else if (ch === "a" || ch === "A") toggleStill();
  }

  // ---- keys --------------------------------------------------------------

  function sheetKey(ch, key) {
    const name = key.name;
    if (ui.overlay.kind === "access-form") {
      if (name === "escape") { access.cancel(); return closeSheet(); }
      if (["up", "down"].includes(name)) return moveControl(name === "up" ? -1 : 1);
      if (name === "enter" || name === "space") { const hit = focusedHit(); if (hit) activate(hit); return; }
      if (name === "pageup" || name === "pagedown") return pageScroll(name === "pageup" ? -1 : 1);
      return;
    }
    if (name === "escape") {
      closeSheet();
      return;
    }
    if (name === "pageup" || name === "pagedown") return pageScroll(name === "pageup" ? -1 : 1);
    if (ui.overlay.kind === "remotes-menu" && ["up", "down", "left", "right"].includes(name)) return moveControl(name === "up" || name === "left" ? -1 : 1);
    if (ui.overlay.kind === "topology") {
      if (name === "up" || name === "down") return stepTopology("room", name === "up" ? -1 : 1);
      if (name === "left" || name === "right") return stepTopology("source", name === "left" ? -1 : 1);
      if (name === "enter" && options.openTopology) {
        const room = topology.room, source = topology.source;
        options.openTopology(room, source).then((result) => {
          if (room === topology.room && source === topology.source) topology.link = result.url;
          if (!result.opened) toast("Site topology", "Open the map link in your browser.");
          changed();
        }).catch(() => toast("Site topology", "The browser could not be opened."));
        return;
      }
    }
    if (ui.overlay.kind === "cameras") {
      if (["up", "down", "left", "right"].includes(name)) return stepCamera(name === "up" || name === "left" ? -1 : 1);
      if (name === "enter") return openCamera();
    }
    if (name === "up" || name === "down" || (ui.overlay.kind === "inputs" && ["left", "right"].includes(name))) {
      if (ui.overlay.kind === "rooms") stepRoomFocus(name === "up" ? -1 : 1);
      else if (ui.overlay.kind === "inputs") moveControl(name === "up" || name === "left" ? -1 : 1);
      else scroll("sheet:" + ui.overlay.kind, name === "up" ? -1 : 1);
      return;
    }
    if (name === "enter" || name === "space") {
      const hit = focusedHit();
      if (name === "enter" && hit && hit.kind === "room") {
        session.selectRoom(hit.room, true);
        focusRemote();
        return;
      }
      if (hit) activate(hit);
    }
  }

  function remoteKey(ch, key) {
    const name = key.name;
    if (!ui.ready || !session.current()) return;
    const hit = ui.focusVisible ? focusedHit() : null;
    if (ui.pane === "remote" && hit && hit.kind === "slider" && RANGE[name] !== undefined) {
      slideBy(name);
      return;
    }
    if (name === "pageup" || name === "pagedown") return pageScroll(name === "pageup" ? -1 : 1);
    if (["up", "down", "left", "right"].includes(name)) {
      if (ui.pane !== "remote") {
        if (name === "up" || name === "down") stepRoomFocus(name === "up" ? -1 : 1, true);
        else stepSourceFocus(name === "left" ? -1 : 1);
        return;
      }
    }
    if (ch && /^[0-9]$/.test(ch)) {
      if (session.controlAllowed && !session.controlAllowed()) return;
      const choice = session.chooseDigit(ch);
      if (choice) flash("bubble:" + choice);
      return;
    }
    if (ch === "[" || ch === "]") {
      session.stepRoom(ch === "]" ? 1 : -1);
      return;
    }
    if (name === "enter") {
      if (ui.pane === "remote") pressKey(["ok"]);
      else applySourceCursor();
      return;
    }
    if (name === "space") {
      if (ui.pane === "remote") pressKey(["ok"]);
      return;
    }
    const lower = ch ? ch.toLowerCase() : "";
    if (lower === "r") return openSheet("rooms");
    if (lower === "i") return openSheet("info");
    if (lower === "d") return openSheet("signal");
    if (lower === "v") {
      session.toggleVolumeMode();
      return;
    }
    if (lower === "t") return toggleTheme();
    if (lower === "a") return toggleStill();
    const buttons = Session.KEYS[KEY_NAMES[name] || ch];
    if (["backspace", "escape"].includes(name) && ui.pane !== "remote") return;
    if (buttons) pressKey(buttons);
  }

  function key(ch, event) {
    if (stopped) return;
    const keyEvent = event || {};
    const name = keyEvent.name;
    if (ui.overlay?.kind === "access-form" && access.key(ch, keyEvent, focusedHit())) return;
    // blessed reports Enter twice, as "enter" and "return".
    if (name === "return") return;
    if (keyEvent.ctrl && name === "c") return quit();
    if (!keyEvent.ctrl && !keyEvent.meta && (ch === "l" || ch === "L")) return toggleLog();
    if (ch === "q" || ch === "Q") return quit();
    if (name === "tab") return movePane(keyEvent.shift ? -1 : 1);
    if (name === "f1") return setScreen("home");
    if (name === "f2") return setScreen("remote");
    if (name === "f3") return setScreen("security");
    if (name === "f4") return setScreen("cameras");
    if (name === "f5") return setScreen("topology");
    if (name === "f7") return setScreen("settings");
    if (name === "f8") return setScreen("users");
    if (name === "f9") return setScreen("account");
    if ((name === "f6" || keyEvent.ctrl && name === "p") && ui.screen === "remote") return setScreen("pairing");
    if (ui.overlay) return sheetKey(ch, keyEvent);
    if (ch === "?") return openSheet("help");
    if (ui.screen === "pairing") return pairingKey(ch, keyEvent);
    if (ui.screen === "remote") return remoteKey(ch, keyEvent);
    return pageKey(ch, keyEvent);
  }

  function pageKey(ch, event) {
    const name = event.name;
    if (name === "escape") return setScreen("home");
    if (name === "pageup" || name === "pagedown") return pageScroll(name === "pageup" ? -1 : 1);
    if (["settings", "users", "account"].includes(ui.screen)) {
      if (["up", "down", "left", "right"].includes(name)) return moveControl(name === "up" || name === "left" ? -1 : 1);
      if (name === "enter" || name === "space") { const hit = focusedHit(); if (hit) activate(hit); }
    } else if (ui.screen === "home") {
      if (["up", "down", "left", "right"].includes(name)) return moveControl(name === "up" || name === "left" ? -1 : 1);
      if (name === "enter" || name === "space") { const hit = focusedHit(); if (hit) activate(hit); }
    } else if (ui.screen === "cameras") {
      if (["up", "down", "left", "right"].includes(name)) return stepCamera(name === "up" || name === "left" ? -1 : 1);
      if (name === "enter") return openCamera();
    } else if (ui.screen === "topology") {
      if ((ch === "s" || ch === "S") && !event.ctrl && !event.meta) {
        Topology.cycleSort(topology, ch === "S" || event.shift);
        ui.scroll.topology = 0;
        changed();
        return;
      }
      if (name === "up" || name === "down") return stepTopology("room", name === "up" ? -1 : 1);
      if (name === "left" || name === "right") return stepTopology("source", name === "left" ? -1 : 1);
      if (name === "enter") return openTopologyBrowser();
    }
    if (ch === "t" || ch === "T") toggleTheme();
    if (ch === "a" || ch === "A") toggleStill();
  }

  function openTopologyBrowser() {
    if (!options.openTopology) return;
    const room = topology.room, source = topology.source;
    options.openTopology(room, source).then(result => {
      if (ui.screen !== "topology" || room !== topology.room || source !== topology.source) return;
      topology.link = result.url;
      if (!result.opened) toast("Site topology", "Open the map link in your browser.");
      changed();
    }).catch(() => toast("Site topology", "The browser could not be opened."));
  }

  // ---- the mouse -----------------------------------------------------------

  function scroll(key, delta) {
    ui.scroll[key] = Math.max(0, (ui.scroll[key] || 0) + delta);
    changed();
  }

  function pageScroll(delta) {
    const region = frame.regions.find((entry) => entry.key === ui.pane);
    if (!region) return;
    ui.revealFocus = false;
    ui.revealRoom = false;
    scroll(region.key, delta * Math.max(1, region.h - 1) * (region.fromEnd ? -1 : 1));
  }

  function wheel(x, y, delta) {
    const entry = frame.grid.hitAt(x, y);
    const kind = entry ? entry.hit.kind : null;
    if (kind === "bubble" || kind === "bubbles-row" || kind === "scroll-bubbles") {
      if (ui.bubbles.overflow) {
        ui.revealFocus = false;
        ui.revealRoom = false;
        ui.bubbles.offset = Math.max(0, (ui.bubbles.offset || 0) + delta * 6);
        changed();
      }
      return;
    }
    if (kind === "slider" && ui.screen === "remote") {
      slideBy(delta < 0 ? "up" : "down");
      return;
    }
    const sheetKey = ui.overlay ? "sheet:" + ui.overlay.kind : null;
    const regions = frame.regions.filter((region) => !sheetKey || region.key === sheetKey);
    for (let index = regions.length - 1; index >= 0; index -= 1) {
      const region = regions[index];
      if (x >= region.x && x < region.x + region.w && y >= region.y && y < region.y + region.h) {
        ui.revealFocus = false;
        ui.revealRoom = false;
        if (region.overflow) scroll(region.key, (region.fromEnd ? -delta : delta) * 3);
        return;
      }
    }
  }

  function mouse(event) {
    if (stopped || !frame) return;
    const { action, x, y } = event;
    if (action === "mousedown") {
      const regions = frame.regions.filter((region) => !ui.overlay || region.key === "sheet:" + ui.overlay.kind);
      const region = regions.find((candidate) => x >= candidate.x - 1 && x <= candidate.x + candidate.w
        && y >= candidate.y - 1 && y <= candidate.y + candidate.h);
      if (region) enterPane(region.key);
      ui.focusVisible = true;
      stopRepeat();
      const entry = frame.grid.hitAt(x, y);
      if (!entry) {
        changed();
        return;
      }
      const hit = entry.hit;
      if (hit.focus) {
        ui.focus = hit.key;
        ui.paneFocus[ui.pane] = hit.key;
      }
      if (hit.kind === "slider" && ui.screen === "remote") {
        if (session.controlAllowed && !session.controlAllowed()) return;
        dragging = entry;
        session.setDragging(true);
        session.slide(sliderValue(entry, x), false);
        changed();
        return;
      }
      if (hit.kind === "key" && hit.repeat && ui.screen === "remote") {
        startRepeat(hit);
        return;
      }
      activate(hit);
      return;
    }
    if (action === "mousemove") {
      if (dragging) session.slide(sliderValue(dragging, x), false);
      return;
    }
    if (action === "mouseup") {
      if (dragging) {
        session.slide(sliderValue(dragging, x), true);
        dragging = null;
      }
      stopRepeat();
      return;
    }
    if (action === "wheelup" || action === "wheeldown") wheel(x, y, action === "wheelup" ? -1 : 1);
  }

  return {
    ui: ui,
    pairing: pairing,
    look: options.look || null,
    start: start,
    stop: stop,
    frame: render,
    lastFrame: () => frame,
    key: key,
    mouse: mouse,
    context: context
  };
}

module.exports = { create, TOAST_MS, FLASH_MS, REPEAT_LIMIT_MS };
