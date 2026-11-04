/* Home observes reported room sources. Module and room links only navigate. */
(function (global) {
  "use strict";
  const house = global.REMOTE_HOUSE, rooms = document.querySelector("[data-home-rooms]"), notice = document.querySelector("[data-home-status]");
  const access = global.ThermiteAccess?.client, system = document.querySelector("[data-home-system]");
  if (access && system) access.ready().then(() => {
    system.hidden = !global.ThermiteNavigation.allowed(global.ThermiteNavigation.pages.find(page => page.id === "system"), access);
    system.href = global.ThermiteShell.href("system", location.search);
  }).catch(() => {});
  const mock = new URLSearchParams(location.search).get("backend") === "mock";
  // The "home" view: only the rooms' Sources, and no activity feed.
  const backend = global.RemoteBackend.create(house, { backend: mock ? "mock" : undefined, view: "home", active: document.visibilityState !== "hidden" });
  // Home starts no remote; an idle session still words each room's lock as the remote does.
  const session = global.RemoteSession?.create({ backend, house });
  access?.ready().then(() => access.watchLocks(true)).catch(() => {});
  const values = Object.create(null), turns = Object.create(null), rows = new Map();
  let connected = document.visibilityState === "hidden" ? false : null;
  house.rooms.forEach(room => {
    const row = document.createElement("tr"), name = document.createElement("td"), link = document.createElement("a");
    link.textContent = room.label; link.href = global.ThermiteShell.href("remotes", location.search, room.id); name.appendChild(link);
    const source = document.createElement("td"), pill = document.createElement("span"), sourceText = document.createElement("span"), status = document.createElement("td");
    pill.className = "home__source"; status.className = "thermite-room-state";
    sourceText.textContent = status.textContent = "Reading…"; pill.append(sourceText, padlock()); source.appendChild(pill);
    row.dataset.state = "unknown"; row.append(name, source, status); rooms.appendChild(row);
    rows.set(room.id, { element: row, source: sourceText, lock: pill.lastChild, status });
  });
  // The remote's padlock, after a locked room's source; its label says the lock.
  function padlock() {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg"), use = document.createElementNS(svg.namespaceURI, "use");
    svg.setAttribute("class", "home__lock"); svg.setAttribute("role", "img"); use.setAttribute("href", "#i-lock"); svg.appendChild(use);
    return svg;
  }
  function update(id) {
    const row = rows.get(id), value = values[id]; if (!row) return;
    const room = house.rooms.find(room => room.id === id);
    const choice = room.sources.includes(value?.value) ? house.sources[value.value] : null;
    row.source.textContent = connected === false ? "Unknown" : choice?.label || (value?.value === "off" ? "Off" : "Unknown");
    row.status.textContent = connected === false ? "Live unavailable" : value?.pending ? "Switching" : choice ? "Reported" : value?.value === "off" ? "Room off" : "Unknown";
    row.element.dataset.state = connected === false ? "unavailable" : value?.pending ? "switching" : choice ? "reported" : value?.value === "off" ? "off" : "unknown";
  }
  function showLock(row, id) {
    const lock = session ? session.roomLocked(id) : { locked: false, text: "" };
    row.element.toggleAttribute("data-locked", lock.locked); row.lock.setAttribute("aria-label", lock.text);
  }
  backend.subscribe(event => {
    if (event.type === "state") { turns[event.room] = (turns[event.room] || 0) + 1; values[event.room] = event.state; update(event.room); }
    if (event.type === "access") rows.forEach(showLock);
    if (event.type === "connection") { connected = event.connected; house.rooms.forEach(room => update(room.id)); notice.textContent = !house.rooms.length ? "No rooms configured" : connected ? "Reported room sources" : "Live room reports unavailable"; }
  });
  house.rooms.forEach(room => {
    const turn = turns[room.id] || 0;
    backend.getState(room.id).then(state => { if ((turns[room.id] || 0) === turn) { values[room.id] = state; update(room.id); } })
      .catch(() => { if ((turns[room.id] || 0) === turn) { values[room.id] = null; update(room.id); } });
  });
  notice.textContent = !house.rooms.length ? "No rooms configured" : mock ? "Sample room sources · no device commands" : "Reported room sources";
  function setActive(visible) {
    if (!visible) {
      connected = false;
      house.rooms.forEach(room => update(room.id));
      notice.textContent = house.rooms.length ? "Live room reports paused" : "No rooms configured";
    } else if (backend.mock) {
      connected = true;
      house.rooms.forEach(room => update(room.id));
      notice.textContent = house.rooms.length ? "Sample room sources · no device commands" : "No rooms configured";
    }
    backend.setActive?.(visible);
  }
  const active = () => setActive(document.visibilityState !== "hidden");
  document.addEventListener("visibilitychange", active);
  global.addEventListener("pagehide", () => setActive(false));
  global.addEventListener("pageshow", active);
})(globalThis);
