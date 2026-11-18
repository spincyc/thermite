/* Home observes reported room sources, or what each room's speakers play:
   each row is the session's view of its room (session.js view(), in Listen
   listen-session.js status()), as Remotes' room list draws it, with one
   word for it in the last column. Above the rooms it shows the house's
   firing alerts as alerts.js reads them from the gateway, and marks the
   rows of the rooms they are about. Module and room links only navigate. */
(function (global) {
  "use strict";
  const house = global.REMOTE_HOUSE, rooms = document.querySelector("[data-home-rooms]"), notice = document.querySelector("[data-home-status]");
  const switcher = document.querySelector("[data-home-modes]"), what = document.querySelector("[data-home-what]"), note = document.querySelector("[data-home-note]");
  const access = global.ThermiteAccess?.client, system = document.querySelector("[data-home-system]");
  if (access && system) access.ready().then(() => {
    system.hidden = !global.ThermiteNavigation.allowed(global.ThermiteNavigation.pages.find(page => page.id === "system"), access);
    system.href = global.ThermiteShell.href("system", location.search);
  }).catch(() => {});
  const params = new URLSearchParams(location.search), mock = params.get("backend") === "mock";
  const store = storage();
  // Watch lists the rooms with a screen, Listen those with speakers, as the
  // remote's lists do (session.js listed()), and the choice is the remote's
  // own ("remote.mode"): a room opened from here opens in it.
  const listed = {
    watch: house.rooms,
    listen: house.rooms.concat(house.speakerRooms || []).filter(room => (room.speakers || []).length)
  };
  const kinds = ["watch", "listen"].filter(kind => listed[kind].length);
  let mode = kinds.includes(store.get("remote.mode")) ? store.get("remote.mode") : kinds[0] || "watch";
  // The "home" view: only the rooms' Sources, and no activity feed; its stream runs only in Watch (live()).
  const backend = global.RemoteBackend.create(house, { backend: mock ? "mock" : undefined, view: "home", active: document.visibilityState !== "hidden" && mode === "watch" });
  // Home starts no remote; an idle session still says what each room shows and words its lock as the remote does.
  const session = global.RemoteSession.create({ backend, house });
  access?.ready().then(() => access.watchLocks(true)).catch(() => {});
  // The rooms as the session's view reads them (backend getRooms), and what each last read.
  const described = Object.create(null), values = Object.create(null), turns = Object.create(null), rows = new Map();
  // A room not read yet, or whose read failed: "Reading…" until its next update, as on Remotes.
  const UNREAD = { value: "NULL", pending: null, status: "unknown", reason: "" };
  // The last column's word for what a room's LED says.
  const WORDS = { reported: "Reported", off: "Room off", switching: "Switching", unavailable: "Source unknown", unknown: "Reading…" };
  // Listen's sessions on a remote-view backend, made when Listen is first shown.
  let heard = null;
  // The house's alerts (alerts.js): the gateway's feed, in a preview its
  // sample (?alerts=<name>), and none on a page openHAB serves by itself.
  const panel = document.querySelector("[data-home-alerts]");
  const feed = !global.RemoteAlerts || !panel ? null : mock ? () => global.RemoteAlerts.sample(house, params.get("alerts"))
    : access && !access.native ? () => access.alerts() : null;
  const watching = feed ? global.RemoteAlerts.create({ read: feed, house, confirmMs: mock ? 0 : undefined }) : null;
  const SEVERITY = { critical: "Critical", warning: "Warning" };
  let marks = {}, drawn = "";

  function build() {
    rooms.textContent = ""; rows.clear();
    listed[mode].forEach(room => {
      const row = document.createElement("tr"), name = document.createElement("td"), link = document.createElement("a");
      const alerted = icon("home__alerted", "#i-alert");
      link.textContent = room.label; link.href = global.ThermiteShell.href("remotes", location.search, room.id); link.appendChild(alerted); name.appendChild(link);
      const lock = document.createElement("td"), source = document.createElement("td"), pill = document.createElement("span"), status = document.createElement("td");
      const svg = padlock(); lock.className = "home__lock-cell"; lock.appendChild(svg);
      pill.className = "home__source"; status.className = "thermite-room-state";
      pill.textContent = status.textContent = "Reading…"; source.appendChild(pill);
      row.dataset.state = "unknown"; row.append(name, lock, source, status); rooms.appendChild(row);
      rows.set(room.id, { element: row, source: pill, lock: svg, status, alerted });
    });
    rows.forEach(showLock);
    rows.forEach(mark);
  }
  // An icon of the page's sprite whose label says what it stands for.
  function icon(name, symbol) {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg"), use = document.createElementNS(svg.namespaceURI, "use");
    svg.setAttribute("class", name); svg.setAttribute("role", "img"); use.setAttribute("href", symbol); svg.appendChild(use);
    return svg;
  }
  // The remote's padlock, in its own column; its label says the lock.
  function padlock() { return icon("home__lock", "#i-lock"); }
  // A room an alert is about carries the worst of them beside its name, read with it.
  function mark(row, id) {
    const severity = marks[id] || "";
    if (severity) row.element.dataset.alert = severity; else delete row.element.dataset.alert;
    row.alerted.setAttribute("aria-label", severity ? SEVERITY[severity] + " alert" : "");
  }
  // A row's state, for its colours, from a view: its LED, and unavailable while the house is not heard.
  function stateOf(shown) {
    if (shown.connected === false || shown.led === "bad") return "unavailable";
    return shown.led === "ok" ? "reported" : shown.led === "busy" ? "switching" : shown.short === "…" ? "unknown" : "off";
  }
  // What a room shows (session.js view): its source as the remote's list
  // words it, and nothing of what was last read while the house is not heard.
  function update(id) {
    const row = rows.get(id); if (!row || mode !== "watch" || !described[id]) return;
    const shown = session.view(described[id], values[id] || UNREAD), state = stateOf(shown);
    row.source.textContent = shown.connected === false ? "Unknown" : state === "unknown" ? "Reading…" : shown.short;
    row.status.textContent = shown.connected === false ? "Not connected" : WORDS[state];
    row.element.dataset.state = state;
  }
  // What a room's speakers play (listen-session.js status): its title, else nothing or not known.
  function hear(id) {
    const row = rows.get(id); if (!row || mode !== "listen" || !heard) return;
    const shown = heard.sessions.listen.status(id), state = stateOf(shown);
    row.source.textContent = shown.playing ? shown.playing.replace(/^♪ /, "") : state === "unknown" || state === "unavailable" ? "Unknown" : "Nothing playing";
    row.status.textContent = state === "unknown" ? WORDS.unknown : shown.short;
    row.element.dataset.state = state;
  }
  function showLock(row, id) {
    const lock = session.roomLocked(id);
    row.element.toggleAttribute("data-locked", lock.locked); row.lock.setAttribute("aria-label", lock.text);
  }
  function hearing() {
    if (heard || !global.RemoteListen) return heard;
    const active = document.visibilityState !== "hidden";
    const speakers = global.RemoteBackend.create(house, { backend: mock ? "mock" : undefined, active });
    heard = { backend: speakers, sessions: global.RemoteListen.sessions({ backend: speakers, house, store, access, mock, active }) };
    heard.sessions.listen.subscribe(event => { if (event.type === "change") rows.forEach((row, id) => hear(id)); });
    heard.sessions.start().catch(console.warn);
    return heard;
  }
  // What the house has to say, above its rooms: the alerts it is firing,
  // or that they cannot be read, as alerts.js view() gives them. Only what
  // changed is drawn again, so a reader hears a change once and an opened
  // alert stays open.
  function alerts() {
    if (!watching) return;
    const shown = watching.view(), next = JSON.stringify(shown);
    if (next === drawn) return;
    drawn = next;
    const list = panel.querySelector("[data-home-alerts-list]"), more = panel.querySelector("[data-home-alerts-more]");
    const open = new Set(Array.from(list.querySelectorAll("details")).filter(item => item.open).map(item => item.dataset.alert));
    panel.hidden = false; panel.dataset.state = shown.state; panel.dataset.tone = shown.tone;
    panel.querySelector("[data-home-alerts-summary]").textContent = shown.summary || "Reading…";
    list.textContent = ""; list.hidden = !shown.lines.length;
    shown.lines.forEach(line => {
      const item = document.createElement("li"), details = document.createElement("details"), summary = document.createElement("summary");
      const tag = document.createElement("span"), title = document.createElement("strong"), text = document.createElement("p");
      item.dataset.severity = line.severity; details.dataset.alert = line.title + "\n" + line.where; details.open = open.has(details.dataset.alert);
      tag.className = "home__alert-tag"; tag.textContent = SEVERITY[line.severity]; title.textContent = line.title;
      summary.append(tag, title);
      if (line.where) { const where = document.createElement("span"); where.className = "home__alert-where"; where.textContent = line.where; summary.appendChild(where); }
      text.textContent = line.text + (line.since ? (line.text ? " " : "") + "Firing for " + line.since + "." : "");
      details.append(summary, text); item.appendChild(details); list.appendChild(item);
    });
    more.textContent = shown.moreText; more.hidden = !shown.moreText;
    marks = shown.marks; rows.forEach(mark);
  }
  function words() {
    if (!listed[mode].length) return "No rooms configured";
    if (mode === "listen") return mock ? "Sample speakers · no device commands" : "What each room's speakers play";
    return mock ? "Sample room sources · no device commands" : "Reported room sources";
  }
  // The line above the rooms: that the house is not heard, else what the table is.
  function say() {
    notice.textContent = mode === "watch" && !session.connected() ? global.RemoteSession.NOT_CONNECTED : words();
  }
  // Watch or Listen: its rooms, its column and note, and the switch.
  function show(next, focus) {
    mode = next; store.set("remote.mode", mode);
    switcher.hidden = kinds.length < 2;
    switcher.querySelectorAll("[data-mode]").forEach(option => {
      const on = option.dataset.mode === mode;
      option.setAttribute("aria-checked", String(on)); option.tabIndex = on ? 0 : -1;
      if (on && focus) option.focus();
    });
    what.textContent = mode === "listen" ? "Playing" : "Reported source";
    note.textContent = mode === "listen" ? "Open a room for its speakers and music." : "Open a room for sources and device controls.";
    if (mode === "listen") hearing();
    build();
    rows.forEach((row, id) => (mode === "listen" ? hear(id) : update(id)));
    live();
    say();
  }
  switcher.addEventListener("click", event => {
    const option = event.target.closest("[data-mode]");
    if (option && option.dataset.mode !== mode) show(option.dataset.mode, false);
  });
  // A radio group: the arrows move between its two options.
  switcher.addEventListener("keydown", event => {
    if (!["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(event.key)) return;
    event.preventDefault(); show(mode === "watch" ? "listen" : "watch", true);
  });
  // Y switches, as on the remote.
  document.addEventListener("keydown", event => {
    if ((event.key === "y" || event.key === "Y") && kinds.length > 1 && !event.ctrlKey && !event.metaKey && !event.altKey
      && !event.target.closest?.("input, textarea, select, [contenteditable]")) {
      // Focus on the switch or on a row the switch rebuilds stays on the switch.
      show(mode === "watch" ? "listen" : "watch", switcher.contains(document.activeElement) || rooms.contains(document.activeElement));
    }
  });
  backend.subscribe(event => {
    if (event.type === "state") { turns[event.room] = (turns[event.room] || 0) + 1; values[event.room] = event.state; update(event.room); }
    if (event.type === "access") rows.forEach(showLock);
    if (event.type === "connection") { house.rooms.forEach(room => update(room.id)); say(); }
  });
  backend.getRooms().then(list => { list.forEach(room => { described[room.id] = room; update(room.id); }); });
  house.rooms.forEach(room => {
    const turn = turns[room.id] || 0;
    backend.getState(room.id).then(state => { if ((turns[room.id] || 0) === turn) { values[room.id] = state; update(room.id); } })
      .catch(() => { if ((turns[room.id] || 0) === turn) { values[room.id] = null; update(room.id); } });
  });
  // Each mode's streams run only while it is on show in a visible page; the alerts are read in both.
  function live() {
    const visible = document.visibilityState !== "hidden";
    watching?.setActive(visible);
    backend.setActive?.(visible && mode === "watch");
    if (heard) { heard.backend.setActive?.(visible && mode === "listen"); heard.sessions.setActive(visible && mode === "listen"); }
  }
  function storage() {
    try {
      const local = global.localStorage;
      local.getItem("remote.mode");
      return { get: key => local.getItem(key), set: (key, value) => { try { local.setItem(key, value); } catch (_) { /* quota or private mode */ } } };
    } catch (_) {
      const memory = new Map();
      return { get: key => memory.get(key) ?? null, set: (key, value) => { memory.set(key, String(value)); } };
    }
  }
  watching?.subscribe(alerts);
  show(mode, false);
  // A page shown again reads the house afresh; until then its rows are what
  // was last read, and once its stream fails they say so (the connection event).
  document.addEventListener("visibilitychange", live);
  global.addEventListener("pagehide", () => { watching?.setActive(false); backend.setActive?.(false); if (heard) { heard.backend.setActive?.(false); heard.sessions.setActive(false); } });
  global.addEventListener("pageshow", live);
})(globalThis);
