/* Home observes reported room sources, or what each room's speakers play.
   Module and room links only navigate. */
(function (global) {
  "use strict";
  const house = global.REMOTE_HOUSE, rooms = document.querySelector("[data-home-rooms]"), notice = document.querySelector("[data-home-status]");
  const switcher = document.querySelector("[data-home-modes]"), what = document.querySelector("[data-home-what]"), note = document.querySelector("[data-home-note]");
  const access = global.ThermiteAccess?.client, system = document.querySelector("[data-home-system]");
  if (access && system) access.ready().then(() => {
    system.hidden = !global.ThermiteNavigation.allowed(global.ThermiteNavigation.pages.find(page => page.id === "system"), access);
    system.href = global.ThermiteShell.href("system", location.search);
  }).catch(() => {});
  const mock = new URLSearchParams(location.search).get("backend") === "mock";
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
  // Home starts no remote; an idle session still words each room's lock as the remote does.
  const session = global.RemoteSession?.create({ backend, house });
  access?.ready().then(() => access.watchLocks(true)).catch(() => {});
  const values = Object.create(null), turns = Object.create(null), rows = new Map();
  let connected = document.visibilityState === "hidden" ? false : null, paused = connected === false;
  // Listen's sessions on a remote-view backend, made when Listen is first shown.
  let heard = null;

  function build() {
    rooms.textContent = ""; rows.clear();
    listed[mode].forEach(room => {
      const row = document.createElement("tr"), name = document.createElement("td"), link = document.createElement("a");
      link.textContent = room.label; link.href = global.ThermiteShell.href("remotes", location.search, room.id); name.appendChild(link);
      const lock = document.createElement("td"), source = document.createElement("td"), pill = document.createElement("span"), status = document.createElement("td");
      const svg = padlock(); lock.className = "home__lock-cell"; lock.appendChild(svg);
      pill.className = "home__source"; status.className = "thermite-room-state";
      pill.textContent = status.textContent = "Reading…"; source.appendChild(pill);
      row.dataset.state = "unknown"; row.append(name, lock, source, status); rooms.appendChild(row);
      rows.set(room.id, { element: row, source: pill, lock: svg, status });
    });
    rows.forEach(showLock);
  }
  // The remote's padlock, in its own column; its label says the lock.
  function padlock() {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg"), use = document.createElementNS(svg.namespaceURI, "use");
    svg.setAttribute("class", "home__lock"); svg.setAttribute("role", "img"); use.setAttribute("href", "#i-lock"); svg.appendChild(use);
    return svg;
  }
  function update(id) {
    const row = rows.get(id), value = values[id]; if (!row || mode !== "watch") return;
    const room = house.rooms.find(room => room.id === id);
    const choice = room.sources.includes(value?.value) ? house.sources[value.value] : null;
    row.source.textContent = connected === false ? "Unknown" : choice?.label || (value?.value === "off" ? "Off" : "Unknown");
    row.status.textContent = connected === false ? "Live unavailable" : value?.pending ? "Switching" : choice ? "Reported" : value?.value === "off" ? "Room off" : "Unknown";
    row.element.dataset.state = connected === false ? "unavailable" : value?.pending ? "switching" : choice ? "reported" : value?.value === "off" ? "off" : "unknown";
  }
  // What a room's speakers play (listen-session.js status): its title, else nothing or not known.
  function hear(id) {
    const row = rows.get(id); if (!row || mode !== "listen" || !heard) return;
    const shown = heard.sessions.listen.status(id), reading = shown.short === "…";
    row.source.textContent = shown.playing ? shown.playing.replace(/^♪ /, "") : reading || shown.led === "bad" ? "Unknown" : "Nothing playing";
    row.status.textContent = reading ? "Reading…" : shown.short;
    row.element.dataset.state = shown.led === "ok" ? "reported" : shown.led === "bad" ? "unavailable" : reading ? "unknown" : "off";
  }
  function showLock(row, id) {
    const lock = session ? session.roomLocked(id) : { locked: false, text: "" };
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
  function words() {
    if (!listed[mode].length) return "No rooms configured";
    if (mode === "listen") return mock ? "Sample speakers · no device commands" : "What each room's speakers play";
    return connected === false ? (paused ? "Live room reports paused" : "Live room reports unavailable")
      : mock ? "Sample room sources · no device commands" : "Reported room sources";
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
    notice.textContent = words();
    live();
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
    if (event.type === "connection") { connected = event.connected; paused = false; house.rooms.forEach(room => update(room.id)); notice.textContent = words(); }
  });
  house.rooms.forEach(room => {
    const turn = turns[room.id] || 0;
    backend.getState(room.id).then(state => { if ((turns[room.id] || 0) === turn) { values[room.id] = state; update(room.id); } })
      .catch(() => { if ((turns[room.id] || 0) === turn) { values[room.id] = null; update(room.id); } });
  });
  // Each mode's streams run only while it is on show in a visible page.
  function live() {
    const visible = document.visibilityState !== "hidden";
    backend.setActive?.(visible && mode === "watch");
    if (heard) { heard.backend.setActive?.(visible && mode === "listen"); heard.sessions.setActive(visible && mode === "listen"); }
  }
  function setActive(visible) {
    if (!visible || backend.mock) {
      connected = visible; paused = !visible;
      house.rooms.forEach(room => update(room.id));
      notice.textContent = words();
    }
    live();
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
  show(mode, false);
  const active = () => setActive(document.visibilityState !== "hidden");
  document.addEventListener("visibilitychange", active);
  global.addEventListener("pagehide", () => setActive(false));
  global.addEventListener("pageshow", active);
})(globalThis);
