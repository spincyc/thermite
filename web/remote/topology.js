/* Configured AV topology. Filtering and observation never operate a device. */
(function (global) {
  "use strict";
  const ID = /^[a-z][a-z0-9_]{0,79}$/;
  const text = (value, max = 160) => typeof value === "string" ? value.replace(/[\u0000-\u001f\u007f-\u009f]/g, "").slice(0, max) : "";
  const port = value => typeof value === "number" && Number.isFinite(value) ? String(value) : text(value, 32);
  const own = (object, key) => Object.prototype.hasOwnProperty.call(object, key);

  function catalogue(raw) {
    if (!raw || raw.version !== 1 || !Array.isArray(raw.nodes) || raw.nodes.length > 512
        || !Array.isArray(raw.rooms) || raw.rooms.length > 128 || !Array.isArray(raw.edges) || raw.edges.length > 4096
        || !raw.routes || typeof raw.routes !== "object") throw new Error("The topology map is unavailable.");
    const seen = new Set();
    const nodes = raw.nodes.map(node => {
      if (!node || typeof node.id !== "string" || !ID.test(node.id) || seen.has(node.id)) throw new Error("The topology map is unavailable.");
      seen.add(node.id);
      return { id: node.id, label: text(node.label) || node.id, room: text(node.room), kind: text(node.kind), make: text(node.make), model: text(node.model) };
    });
    const roomIds = new Set();
    const rooms = raw.rooms.map(room => {
      if (!room || typeof room.id !== "string" || !ID.test(room.id) || roomIds.has(room.id) || !Array.isArray(room.devices)) throw new Error("The topology map is unavailable.");
      roomIds.add(room.id);
      return { id: room.id, label: text(room.label) || room.id, location: text(room.location), devices: room.devices.filter(id => seen.has(id)) };
    });
    const routes = Object.create(null);
    for (const [room, sources] of Object.entries(raw.routes)) {
      if (!roomIds.has(room) || !sources || typeof sources !== "object") throw new Error("The topology map is unavailable.");
      routes[room] = Object.create(null);
      for (const [source, route] of Object.entries(sources)) {
        if (!ID.test(source) || !route || !Array.isArray(route.nodes) || route.nodes.length > 32) throw new Error("The topology map is unavailable.");
        function readNodes(nodes) { return nodes.map(node => {
          if (!node || !seen.has(node.device)) throw new Error("The topology map is unavailable.");
          return { device: node.device, label: text(node.label) || node.device, role: text(node.role), input: port(node.input), output: port(node.output), zone: port(node.zone) };
        }); }
        const path = readNodes(route.nodes);
        if (route.audioNodes != null && (!Array.isArray(route.audioNodes) || route.audioNodes.length > 32)) throw new Error("The topology map is unavailable.");
        const audioNodes = readNodes(route.audioNodes || []);
        const usb = (Array.isArray(route.usb) ? route.usb : []).filter(entry => entry && seen.has(entry.device)).map(entry => ({
          device: entry.device, host: port(entry.host), ports: (Array.isArray(entry.ports) ? entry.ports : []).map(port).filter(Boolean)
        }));
        routes[room][source] = { label: text(route.label) || source, nodes: path, usb, ...(audioNodes.length ? { audioNodes } : {}) };
      }
    }
    const edgeIds = new Set();
    const edges = raw.edges.map(edge => {
      if (!edge || !text(edge.id) || edgeIds.has(edge.id) || !seen.has(edge.from) || !seen.has(edge.to)
          || !["video", "audio", "usb"].includes(edge.kind) || !Array.isArray(edge.rooms) || !Array.isArray(edge.sources)) throw new Error("The topology map is unavailable.");
      edgeIds.add(edge.id);
      return { id: text(edge.id), from: edge.from, to: edge.to, kind: edge.kind, fromPort: port(edge.fromPort), toPort: port(edge.toPort),
        rooms: edge.rooms.filter(id => roomIds.has(id)), sources: edge.sources.filter(id => ID.test(id)) };
    });
    return { version: 1, nodes, rooms, routes, edges };
  }

  function carries(edge, route) {
    if (edge.kind === "usb") return route.nodes[0] && route.nodes[0].device === edge.from
      && route.usb.some(entry => entry.device === edge.to && entry.host === edge.toPort);
    const chain = edge.kind === "audio" ? route.audioNodes || [] : route.nodes;
    return chain.some((node, index) => {
      const next = chain[index + 1];
      return next && node.device === edge.from && next.device === edge.to
        && node.output === edge.fromPort && next.input === edge.toPort;
    });
  }

  function project(model, room, source, states = {}) {
    const routeRooms = room ? [room] : Object.keys(model.routes);
    const paths = [];
    for (const roomId of routeRooms) {
      const confirmed = states[roomId] && states[roomId].value;
      for (const [sourceId, route] of Object.entries(model.routes[roomId] || {})) {
        if (source && source !== sourceId) continue;
        paths.push({ room: roomId, source: sourceId, route, confirmed: confirmed === sourceId });
      }
    }
    const edges = model.edges.filter(edge => room || source ? paths.some(path => carries(edge, path.route)) : true)
      .map(edge => ({ ...edge, confirmed: paths.some(path => path.confirmed && carries(edge, path.route)) }));
    const included = new Set(edges.flatMap(edge => [edge.from, edge.to]));
    paths.forEach(path => path.route.nodes.concat(path.route.audioNodes || []).forEach(node => included.add(node.device)));
    const active = new Set(paths.filter(path => path.confirmed).flatMap(path => path.route.nodes.concat(path.route.audioNodes || []).map(node => node.device)));
    return { nodes: model.nodes.filter(node => included.has(node.id)).map(node => ({ ...node, confirmed: active.has(node.id) })), edges, paths };
  }

  function layout(model, view, vertical = false) {
    const ranks = new Map(), levels = { source: 0, matrix: 1, switch: 1, receiver: 2, observer: 3, display: 4 };
    for (const sources of Object.values(model.routes)) for (const route of Object.values(sources)) {
      route.nodes.concat(route.audioNodes || []).forEach(node => { if (own(levels, node.role)) ranks.set(node.device, levels[node.role]); });
      route.usb.forEach(entry => ranks.set(entry.device, 1));
    }
    const columns = Array.from({ length: 5 }, () => []);
    view.nodes.forEach(node => columns[ranks.get(node.id) ?? 1].push(node));
    columns.forEach(column => column.sort((a, b) => a.label.localeCompare(b.label)));
    const used = columns.map((column, rank) => ({ column, rank })).filter(entry => entry.column.length);
    const spans = used.map(entry => !vertical && entry.rank === 0 && entry.column.length > 8 ? 2 : 1);
    const rowCounts = used.map((entry, index) => Math.ceil(entry.column.length / spans[index]));
    const widest = Math.max(0, ...rowCounts);
    const width = vertical ? Math.max(310, widest * 236 + 24) : Math.max(310, spans.reduce((sum, span) => sum + span, 0) * 250 + 20);
    const height = vertical ? used.length * 110 + 52 : Math.max(340, widest * 86 + 78);
    const nodes = [];
    let offset = 0;
    const headings = [];
    used.forEach(({ column, rank }, col) => {
      headings.push({ rank, x: offset + 24 });
      column.forEach((node, row) => {
      nodes.push({ ...node, rank, x: vertical ? (width - column.length * 236) / 2 + row * 236 + 10 : offset + Math.floor(row / rowCounts[col]) * 250 + 22,
        y: vertical ? col * 110 + 36 : 58 + row % rowCounts[col] * 86 + (height - 78 - rowCounts[col] * 86) / 2 });
      });
      offset += spans[col] * 250;
    });
    return { nodes, width, height, vertical, columns: headings };
  }

  function sourcesOf(model, room = "") {
    const sources = new Map();
    for (const [roomId, routes] of Object.entries(model.routes)) for (const [id, route] of Object.entries(routes)) {
      if (!sources.has(id)) sources.set(id, { id, label: route.label, node: model.nodes.find(node => node.id === id), rooms: [] });
      sources.get(id).rooms.push(roomId);
    }
    return [...sources.values()].filter(source => !room || source.rooms.includes(room))
      .map(source => ({ ...source, shared: source.rooms.length > 1 })).sort((a, b) => a.label.localeCompare(b.label));
  }

  function chains(model, view) {
    const groups = new Map();
    for (const path of view.paths) {
      if (!groups.has(path.room)) groups.set(path.room, { room: path.room, video: [], data: [] });
      const group = groups.get(path.room);
      group.video.push(path);
      for (const usb of path.route.usb) {
        const edge = view.edges.find(edge => edge.kind === "usb" && edge.to === usb.device && edge.toPort === usb.host && carries(edge, path.route));
        if (!edge) continue;
        let link = group.data.find(link => link.edge.id === edge.id);
        if (!link) { link = { edge, assignments: [] }; group.data.push(link); }
        link.assignments.push({ source: path.source, label: path.route.label, host: usb.host, ports: usb.ports, sourceReported: path.confirmed });
      }
    }
    return [...groups.values()];
  }

  const VIDEO_COLUMNS = [["room", "Room"], ["status", "Status"], ["source", "Source"], ["matrix", "Matrix"], ["switch", "Switch"], ["receiver", "Receiver"], ["observer", "Link observer"], ["display", "Display"], ["audio", "Separate audio"]];
  const DATA_COLUMNS = [["source", "Source"], ["usb", "USB matrix / HOST"], ["room", "Room"], ["ports", "DEVICE ports"], ["status", "Status"]];

  function compactLabel(model, node) {
    const recorded = model.nodes.find(entry => entry.id === node.device || entry.id === node.id);
    const label = node.label || recorded?.label || node.device;
    const suffix = recorded ? [recorded.make, recorded.model].filter(Boolean).join(" ") : "";
    return suffix && label.endsWith(" (" + suffix + ")") ? label.slice(0, -suffix.length - 3) : label;
  }

  function routeStatus(model, room, source, state, connected) {
    const routes = model.routes[room] || {};
    if (connected === false) return { label: "Live unavailable", detail: "Live room updates are unavailable; retained source values may be old." };
    const pending = state && own(routes, state.pending) ? state.pending : null;
    if (pending === source) return { label: "Switching", detail: "Waiting for the room to report " + routes[pending].label + "." };
    if (state?.value === "off") return { label: "Room off", detail: "The room source is Off; device power is not inferred." };
    if (!state || !own(routes, state.value)) return { label: "Unknown", detail: "No source report, no answer, or an input this room does not offer." };
    if (state.value === source) return { label: "Reported", detail: pending ? "Last reported source; switching to " + routes[pending].label + "." : "The room reports this configured source." };
    return { label: "Configured", detail: "The room reports " + routes[state.value].label + ", not this source." };
  }

  function nativePorts(node) {
    return [node.input ? "in " + node.input : "", node.output ? "out " + node.output : "", node.zone ? "zone " + node.zone : ""].filter(Boolean).join(" · ");
  }

  function videoRows(model, view, states = {}, connected) {
    return view.paths.map(path => {
      const roles = {};
      for (const role of ["matrix", "switch", "receiver", "observer", "display"]) roles[role] = path.route.nodes.filter(node => node.role === role);
      roles.audio = (path.route.audioNodes || []).filter(node => node.role !== "source");
      const status = routeStatus(model, path.room, path.source, states[path.room], connected);
      const room = model.rooms.find(entry => entry.id === path.room)?.label || path.room;
      const sort = { room, source: path.route.label, status: status.label };
      for (const [role, nodes] of Object.entries(roles)) sort[role] = nodes.map(node => compactLabel(model, node) + " " + nativePorts(node)).join(" → ");
      return { id: path.room + ":" + path.source, room: path.room, source: path.source, roomLabel: room, sourceLabel: path.route.label,
        sourceNode: path.route.nodes[0], roles, status, sort, path };
    });
  }

  function dataRows(model, view) {
    return chains(model, view).flatMap(group => group.data.flatMap(link => link.assignments.map(assignment => {
      const node = model.nodes.find(entry => entry.id === link.edge.to);
      const room = model.rooms.find(entry => entry.id === group.room)?.label || group.room;
      return { id: group.room + ":" + assignment.source + ":" + link.edge.id, room: group.room, source: assignment.source,
        sourceNode: model.nodes.find(entry => entry.id === link.edge.from), matrix: node, host: assignment.host, ports: assignment.ports,
        roomLabel: room, sourceLabel: assignment.label, status: { label: "Configured", detail: "Configured USB assignment; USB state is not observed." },
        sort: { room, source: assignment.label, usb: compactLabel(model, node) + " HOST " + assignment.host, ports: assignment.ports.join(", "), status: "Configured" } };
    })));
  }

  function sortRows(rows, key, direction = 1) {
    const compare = (a, b) => a.localeCompare(b, "en", { numeric: true, sensitivity: "base" });
    return rows.slice().sort((a, b) => compare(a.sort[key] || "", b.sort[key] || "") * (direction === -1 ? -1 : 1) || compare(a.id, b.id));
  }

  global.ThermiteTopology = { catalogue, carries, project, layout, sourcesOf, chains, compactLabel, routeStatus, videoRows, dataRows, sortRows };
  if (typeof document === "undefined") return;
  const $ = selector => document.querySelector(selector);
  const element = (tag, attrs = {}, value) => {
    const node = document.createElement(tag);
    Object.entries(attrs).forEach(([key, val]) => node.setAttribute(key, val));
    if (value != null) node.textContent = value;
    return node;
  };
  const params = new URLSearchParams(location.search);
  let model, backend, selected = "", zoom = 1, connected = null, room = "", source = "";
  const states = Object.create(null), turns = Object.create(null);
  const sorting = { video: { key: "room", direction: 1 }, data: { key: "room", direction: 1 } };
  const roomLabel = id => model.rooms.find(entry => entry.id === id)?.label || id;
  const locationLabel = id => model.rooms.filter(entry => entry.location === id).length > 1
    ? id.split("_").map(word => word.charAt(0).toUpperCase() + word.slice(1)).join(" ") : roomLabel(id);
  const deviceLabel = id => model.nodes.find(entry => entry.id === id)?.label || id;
  const sourceLabel = (id, value) => model.routes[id]?.[value]?.label || (value === "off" ? "Off" : "Unknown");

  function fillSources(reset) {
    const select = $("[data-source]"); select.textContent = "";
    select.appendChild(element("option", { value: "" }, "All configured routes"));
    const choices = Object.create(null);
    for (const id of room ? [room] : Object.keys(model.routes)) {
      for (const [key, route] of Object.entries(model.routes[id] || {})) choices[key] = route.label;
    }
    Object.entries(choices).sort((a, b) => a[1].localeCompare(b[1])).forEach(([id, label]) => select.appendChild(element("option", { value: id }, label)));
    if (!own(choices, source)) source = "";

    select.value = source;
  }

  function status() {
    if (connected === false) return "Live room reports unavailable · configured connections remain visible";
    if (room) {
      const state = states[room];
      return roomLabel(room) + " · reported source: " + sourceLabel(room, state && state.value)
        + (state && state.pending ? " · switching to " + sourceLabel(room, state.pending) : "");
    }
    const known = Object.keys(model.routes).filter(id => own(model.routes[id], states[id] && states[id].value)).length;
    return known + " rooms with a reported source path · " + Object.keys(model.routes).length + " routed rooms";
  }

  function inspect(id) {
    selected = id;
    const node = model.nodes.find(entry => entry.id === id), detail = $("[data-device-detail]");
    if (!node) return;
    detail.textContent = "";
    detail.append(element("p", { class: "eyebrow" }, "Selected device"), element("h2", {}, node.label));
    const facts = element("dl");
    for (const [label, value] of [["Room", locationLabel(node.room)], ["Type", node.kind.replace(/_/g, " ")], ["Make", node.make], ["Model", node.model]]) {
      if (value) facts.append(element("dt", {}, label), element("dd", {}, value));
    }
    detail.appendChild(facts);
    const links = element("ul");
    model.edges.filter(edge => edge.from === id || edge.to === id).forEach(edge => {
      const direction = edge.from === id ? "To " : "From ", peer = edge.from === id ? edge.to : edge.from;
      const ports = [edge.fromPort ? "out " + edge.fromPort : "", edge.toPort ? "in " + edge.toPort : ""].filter(Boolean).join(" → ");
      links.appendChild(element("li", {}, direction + deviceLabel(peer) + " · " + (edge.kind === "usb" ? "USB" : edge.kind === "audio" ? "Audio" : "Video") + (ports ? " · " + ports : "")));
    });
    detail.appendChild(links);
    draw();
  }

  function resetScroll() {
    for (const selector of ["[data-video]", "[data-data]"]) $(selector).scrollTop = 0;
  }

  function renderSources() {
    const entries = sourcesOf(model, room);
    const shared = entries.filter(entry => entry.shared), local = entries.filter(entry => !entry.shared);
    $("[data-source-count]").textContent = shared.length + " shared · " + local.length + " room-local";
    for (const [selector, list] of [["[data-shared-sources]", shared], ["[data-local-sources]", local]]) {
      const group = $(selector); group.textContent = "";
      list.forEach(entry => {
        const card = element("button", { type: "button", class: "source-card", "data-source-chip": entry.id,
          "data-focus-key": "source:" + entry.id, "aria-pressed": source === entry.id ? "true" : "false" });
        card.append(element("strong", {}, entry.label), element("span", {}, entry.shared ? entry.rooms.length + " rooms" : roomLabel(entry.rooms[0])),
          element("small", {}, entry.node?.kind.replace(/_/g, " ") || "Source"));
        card.addEventListener("click", () => { source = source === entry.id ? "" : entry.id; $("[data-source]").value = source; selected = ""; resetScroll(); draw(); });
        group.appendChild(card);
      });
      if (!list.length) group.appendChild(element("p", { class: "muted" }, "None in this selection"));
    }
  }

  function deviceButton(node, key, label, native) {
    const id = node.device || node.id;
    const button = element("button", { type: "button", class: "cell-device" + (selected === id ? " selected" : ""), "data-node": id,
      "data-focus-key": key, title: node.label + (native ? " · " + native : "") });
    button.appendChild(element("span", {}, label || compactLabel(model, node)));
    if (native) button.appendChild(element("small", {}, native));
    button.addEventListener("click", () => inspect(id));
    return button;
  }

  function headers(kind, columns) {
    const tr = element("tr");
    for (const [key, label] of columns) {
      const th = element("th", { scope: "col", "data-sort-column": key });
      if (key === "room") th.classList.add("room-cell");
      const button = element("button", { type: "button", "data-sort": kind + ":" + key, "aria-label": "Sort " + (kind === "video" ? "video" : "USB") + " by " + label });
      button.append(element("span", {}, label), element("span", { class: "sort-indicator", "aria-hidden": "true" }, "↕"));
      button.addEventListener("click", () => {
        const state = sorting[kind]; state.direction = state.key === key ? -state.direction : 1; state.key = key; draw();
      });
      th.appendChild(button); tr.appendChild(th);
    }
    $("[data-" + kind + "-head]").appendChild(tr);
  }

  function draw() {
    const focused = document.activeElement?.getAttribute("data-focus-key");
    const video = $("[data-video]"), data = $("[data-data]");
    const scroll = [video.scrollLeft, data.scrollLeft];
    const view = project(model, room, source, connected === false ? {} : states);
    renderSources();
    const rows = { video: sortRows(videoRows(model, view, states, connected), sorting.video.key, sorting.video.direction),
      data: sortRows(dataRows(model, view), sorting.data.key, sorting.data.direction) };
    for (const kind of ["video", "data"]) {
      const tbody = $("[data-" + kind + "-body]"); tbody.textContent = "";
      for (const row of rows[kind]) {
        const tr = element("tr", { "data-row-key": row.id, "data-room": row.room, "data-source-path": row.source });
        for (const [key] of kind === "video" ? VIDEO_COLUMNS : DATA_COLUMNS) {
          const cell = element(key === "room" ? "th" : "td", { "data-column": key });
          if (key === "room") { cell.setAttribute("scope", "row"); cell.className = "room-cell"; cell.textContent = row.roomLabel; }
          else if (key === "status") {
            cell.appendChild(element("span", { class: "row-status", "data-status-label": row.status.label, title: row.status.detail }, row.status.label));
            if (row.status.label === "Switching" || row.status.label === "Unknown") cell.appendChild(element("small", { class: "status-detail", title: row.status.detail }, row.status.detail));
          } else if (key === "source") cell.appendChild(deviceButton(row.sourceNode, kind + ":" + row.id + ":source", row.sourceLabel));
          else if (key === "usb") cell.appendChild(deviceButton(row.matrix, kind + ":" + row.id + ":usb", null, "HOST " + row.host));
          else if (key === "ports") cell.textContent = row.ports.join(", ") || "—";
          else {
            const nodes = row.roles[key];
            if (!nodes.length) cell.appendChild(element("span", { class: "absent", "aria-label": "No configured " + key }, "—"));
            nodes.forEach((node, index) => {
              if (index) cell.appendChild(element("span", { class: "sequence-arrow", "aria-hidden": "true" }, "→"));
              cell.appendChild(deviceButton(node, kind + ":" + row.id + ":" + key + ":" + index, null, nativePorts(node)));
            });
          }
          tr.appendChild(cell);
        }
        tbody.appendChild(tr);
      }
      if (!rows[kind].length) {
        const tr = element("tr"), td = element("td", { colspan: kind === "video" ? "8" : "5", class: "empty-panel" }, "No configured " + (kind === "video" ? "video paths" : "USB assignments") + " for this selection.");
        tr.appendChild(td); tbody.appendChild(tr);
      }
      document.querySelectorAll("[data-" + kind + "-head] th").forEach(th => {
        const active = th.dataset.sortColumn === sorting[kind].key;
        th.setAttribute("aria-sort", active ? sorting[kind].direction === 1 ? "ascending" : "descending" : "none");
        th.querySelector(".sort-indicator").textContent = active ? sorting[kind].direction === 1 ? "↑" : "↓" : "↕";
      });
    }
    $("[data-video-count]").textContent = rows.video.length + (rows.video.length === 1 ? " source path" : " source paths");
    const hosts = new Set(chains(model, view).flatMap(group => group.data.map(link => link.edge.id))).size;
    $("[data-data-count]").textContent = rows.data.length + " assignments · " + hosts + " host cables";
    document.documentElement.style.setProperty("--table-zoom", zoom);
    video.scrollLeft = scroll[0]; data.scrollLeft = scroll[1];
    if (focused) document.querySelector('[data-focus-key="' + focused + '"]')?.focus({ preventScroll: true });
    $("[data-status]").textContent = status();
    $("[data-path-title]").textContent = room ? roomLabel(room) + " paths" : "Room paths";
    $("[data-path-note]").textContent = "Configuration only. Source previews never switch the room.";
    const list = element("ul", { class: "route-list" });
    view.paths.forEach(path => {
      const row = element("li", { class: "route-row" });
      row.append(element("h3", {}, roomLabel(path.room) + " · " + path.route.label), element("p", {}, path.route.nodes.map(node => node.label + (nativePorts(node) ? " [" + nativePorts(node) + "]" : "")).join(" → ")));
      if (path.route.audioNodes) row.appendChild(element("p", {}, "Configured audio · " + path.route.audioNodes.map(node => node.label + (nativePorts(node) ? " [" + nativePorts(node) + "]" : "")).join(" → ")));
      path.route.usb.forEach(usb => row.appendChild(element("p", {}, "Configured USB · " + deviceLabel(usb.device) + " HOST " + usb.host + " · DEVICE ports " + usb.ports.join(", "))));
      list.appendChild(row);
    });
    $("[data-paths]").replaceChildren(list);
  }

  async function start() {
    try {
      const response = await fetch("topology.json", { method: "GET", cache: "no-cache", headers: { Accept: "application/json" } });
      if (!response.ok) throw new Error("The topology map is unavailable (HTTP " + response.status + ").");
      model = catalogue(await response.json());
      room = model.rooms.some(entry => entry.id === params.get("room")) ? params.get("room") : "";

      source = text(params.get("source"));
      model.rooms.forEach(entry => $("[data-room]").appendChild(element("option", { value: entry.id }, entry.label)));
      $("[data-room]").value = room; fillSources(!params.has("source"));
      $("[data-count]").textContent = model.nodes.length + " devices · " + model.edges.length + " recorded links";
      const linked = new Set(model.edges.flatMap(edge => [edge.from, edge.to]));
      const unlinked = model.nodes.filter(node => !linked.has(node.id));
      $("[data-unlinked-count]").textContent = unlinked.length + " other inventory devices";
      unlinked.forEach(node => $("[data-unlinked]").appendChild(element("li", {}, node.label + " · " + locationLabel(node.room))));
      headers("video", VIDEO_COLUMNS); headers("data", DATA_COLUMNS);
      backend = global.RemoteBackend.create(global.REMOTE_HOUSE, { backend: params.get("backend") === "mock" ? "mock" : undefined, active: document.visibilityState !== "hidden" });
      backend.subscribe(event => {
        if (event.type === "state") { turns[event.room] = (turns[event.room] || 0) + 1; states[event.room] = event.state; draw(); }
        if (event.type === "connection") { connected = event.connected; draw(); }
      });
      global.REMOTE_HOUSE.rooms.forEach(entry => {
        const turn = turns[entry.id] || 0;
        backend.getState(entry.id).then(state => { if ((turns[entry.id] || 0) === turn) { states[entry.id] = state; draw(); } }).catch(() => { if ((turns[entry.id] || 0) === turn) { states[entry.id] = { value: "NULL" }; draw(); } });
      });
      $("[data-room]").addEventListener("change", event => { room = event.target.value; source = ""; selected = ""; zoom = 1; fillSources(true); resetScroll(); draw(); });
      $("[data-source]").addEventListener("change", event => { source = event.target.value; selected = ""; zoom = 1; resetScroll(); draw(); });
      document.querySelectorAll("[data-zoom]").forEach(button => button.addEventListener("click", () => {
        const direction = button.dataset.zoom;
        zoom = direction === "fit" ? 1 : Math.max(.7, Math.min(1.6, zoom + (direction === "in" ? .1 : -.1)));
        draw();
      }));
      const active = () => {
        const visible = document.visibilityState !== "hidden";
        if (backend.setActive) backend.setActive(visible);
        if (!visible || backend.mock) { connected = visible && backend.mock ? true : false; draw(); }
      };
      document.addEventListener("visibilitychange", active);
      global.addEventListener("pagehide", () => { if (backend.setActive) backend.setActive(false); });
      global.addEventListener("pageshow", active);
      active();
      global.addEventListener("resize", draw);
      $("[data-theme-toggle]").addEventListener("click", () => {
        const theme = document.documentElement.dataset.theme === "light" ? "dark" : "light";
        document.documentElement.dataset.theme = theme;
        try { localStorage.setItem("remote.theme", theme); } catch (_) {}
      });
      if (params.get("backend") === "mock") document.querySelectorAll("nav a, a.brand").forEach(link => { link.search = "?backend=mock"; });
      $("[data-catalogue]").open = false;
      draw();
    } catch (error) {
      $("[data-alert]").hidden = false; $("[data-alert]").textContent = "The topology map could not be loaded. Return to Remote or try reloading this page.";
      $("[data-count]").textContent = "Map unavailable";
    }
  }
  start();
})(globalThis);
