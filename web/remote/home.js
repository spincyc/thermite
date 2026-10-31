/* Home observes reported room sources. Module and room links only navigate. */
(function (global) {
  "use strict";
  const house = global.REMOTE_HOUSE, rooms = document.querySelector("[data-home-rooms]"), notice = document.querySelector("[data-home-status]");
  const mock = new URLSearchParams(location.search).get("backend") === "mock";
  const backend = global.RemoteBackend.create(house, { backend: mock ? "mock" : undefined, active: document.visibilityState !== "hidden" });
  const values = Object.create(null), turns = Object.create(null), rows = new Map();
  let connected = document.visibilityState === "hidden" ? false : null;
  house.rooms.forEach(room => {
    const row = document.createElement("tr"), name = document.createElement("td"), link = document.createElement("a");
    link.textContent = room.label; link.href = global.ThermiteShell.href("remotes", location.search, room.id); name.appendChild(link);
    const source = document.createElement("td"), status = document.createElement("td"); status.className = "thermite-room-state";
    source.textContent = status.textContent = "Reading…"; row.append(name, source, status); rooms.appendChild(row);
    rows.set(room.id, { source, status });
  });
  function update(id) {
    const row = rows.get(id), value = values[id]; if (!row) return;
    const room = house.rooms.find(room => room.id === id);
    const choice = room.sources.includes(value?.value) ? house.sources[value.value] : null;
    row.source.textContent = connected === false ? "Unknown" : choice?.label || (value?.value === "off" ? "Off" : "Unknown");
    row.status.textContent = connected === false ? "Live unavailable" : value?.pending ? "Switching" : choice ? "Reported" : value?.value === "off" ? "Room off" : "Unknown";
  }
  backend.subscribe(event => {
    if (event.type === "state") { turns[event.room] = (turns[event.room] || 0) + 1; values[event.room] = event.state; update(event.room); }
    if (event.type === "connection") { connected = event.connected; house.rooms.forEach(room => update(room.id)); notice.textContent = connected ? "Reported room sources" : "Live room reports unavailable"; }
  });
  house.rooms.forEach(room => {
    const turn = turns[room.id] || 0;
    backend.getState(room.id).then(state => { if ((turns[room.id] || 0) === turn) { values[room.id] = state; update(room.id); } })
      .catch(() => { if ((turns[room.id] || 0) === turn) { values[room.id] = null; update(room.id); } });
  });
  notice.textContent = mock ? "Sample room sources · no device commands" : "Reported room sources";
  function setActive(visible) {
    if (!visible) {
      connected = false;
      house.rooms.forEach(room => update(room.id));
      notice.textContent = "Live room reports paused";
    } else if (backend.mock) {
      connected = true;
      house.rooms.forEach(room => update(room.id));
      notice.textContent = "Sample room sources · no device commands";
    }
    backend.setActive?.(visible);
  }
  const active = () => setActive(document.visibilityState !== "hidden");
  document.addEventListener("visibilitychange", active);
  global.addEventListener("pagehide", () => setActive(false));
  global.addEventListener("pageshow", active);
})(globalThis);
