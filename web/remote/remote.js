/*
 * The remote page: draws a RemoteSession (session.js) in the browser and
 * hands it taps, clicks and keys. Everything the remote does without a page
 * lives in the session; this file holds only the DOM, the sheets, the
 * theme and the update check.
 */
(function () {
  "use strict";

  const house = globalThis.REMOTE_HOUSE;
  const layouts = globalThis.REMOTE_LAYOUTS;
  const Remote = globalThis.RemoteBackend;
  const Session = globalThis.RemoteSession;
  const params = new URLSearchParams(window.location.search);
  const backend = Remote.create(house, {
    backend: { mock: "mock", openhab: "openhab" }[params.get("backend")],
    active: document.visibilityState !== "hidden",
    latency: numberParam("latency"),
    answering: params.get("matrix") !== "down"
  });
  const store = storage();
  const activityLog = globalThis.RemoteLog.create({ document: document, store: store });
  const session = Session.create({ backend: backend, house: house, layouts: layouts, store: store, volume: params.get("volume") });
  // Bind before asynchronous startup reads: a tab can become hidden before
  // the first room snapshot finishes loading.
  const syncLive = () => {
    if (backend.setActive) backend.setActive(document.visibilityState !== "hidden");
  };
  document.addEventListener("visibilitychange", syncLive);
  window.addEventListener("pagehide", () => { if (backend.setActive) backend.setActive(false); });
  window.addEventListener("pageshow", syncLive);

  const KIND_ICON = { off: "i-off", computer: "i-pc", streamer: "i-streamer", disc_player: "i-disc", game_console: "i-game" };
  const PLAY = { play_pause: true, play: true };
  const CORNERS = ["tl", "tr", "bl", "br"];
  const UPDATE_EVERY = 60000;
  const RANGE_KEYS = { ArrowUp: true, ArrowDown: true, ArrowLeft: true, ArrowRight: true, Home: true, End: true, PageUp: true, PageDown: true };

  const hints = Session.keyHints();
  let ready = false;
  let drawn = "";
  let lastInput = "pointer";
  let repeat = null;
  let toastTimer = 0;
  let checkedAt = 0;
  let infoView = "devices";
  let signalRunning = false;
  let signalFocus = false;

  session.subscribe(onSession);
  session.start(params.get("room"))
    .then(() => {
      buildRoomLists();
      buildInfo();
      buildShortcuts();
      bind();
      applyTheme(document.documentElement.getAttribute("data-theme"), false);
      ready = true;
      update();
      document.documentElement.classList.add("is-ready");
      const chosen = params.get("choose");
      if (chosen) session.choose(chosen);
      checkForUpdate();
    })
    .catch(fail);

  function onSession(event) {
    if (event.type === "activity") {
      activityLog.capture(event);
      return;
    }
    if (event.type === "toast") {
      toast(event.title, event.body, event.tone, event.tag);
      return;
    }
    if (event.type === "announce") {
      announce(event.message);
      return;
    }
    if (event.type === "all-off") {
      showAllOff(event.armed);
      return;
    }
    if (!ready) return;
    if (event.type === "diagnostics") return updateSignal();
    if (event.type === "change" || event.type === "access") update();
    else if (event.type === "audio") updateAudio();
    else if (event.type === "device") {
      updateAudio();
      if (session.inputsOf() === event.device && $("#input-sheet").open) fillInputs();
    } else if (event.type === "matrix") updateSimulation();
    else if (event.type === "inputs") openInputs(event.title);
  }

  // ---- rendering ---------------------------------------------------------

  function update() {
    const current = session.current();
    if (!current) return;
    all("[data-room-name]").forEach((node) => { node.textContent = current.label; });
    all('.thermite-nav-link[data-view="settings"]').forEach((link) => { link.href = globalThis.ThermiteShell.href("settings", location.search, current.id); });
    document.title = current.label + " · thermite";
    $("#room-switch").setAttribute("aria-label", "Room: " + current.label + ". Choose another room");
    updateRoomLists();
    updateAlert();
    updateControls();
    updateAudio();
    updateSimulation();
    updateSignal();
    syncSignal();
    const allowed = session.controlAllowed();
    all('[data-action="all-off"]').forEach((button) => { button.disabled = Boolean(backend.access && !backend.access.has("room.control")); });
    all('[data-button], [data-choose], [data-action="control"], [data-action="input"]').forEach((button) => {
      if (!allowed && !button.hasAttribute("data-policy-disabled")) { button.dataset.policyDisabled = String(button.disabled); button.disabled = true; }
      else if (allowed && button.hasAttribute("data-policy-disabled")) { button.disabled = button.dataset.policyDisabled === "true"; delete button.dataset.policyDisabled; }
    });
    if (!allowed) { $("[data-fader-input]").disabled = true; $("#alert").hidden = false; $("#alert-text").textContent = [$("#alert-text").textContent, "Read-only · account permission or room schedule"].filter(Boolean).join(" · "); }
  }

  function buildRoomLists() {
    all("[data-room-list]").forEach((list) => {
      list.textContent = "";
      session.rooms().forEach((room) => {
        list.appendChild(node("li", null, [
          node("button", { type: "button", class: "room", "data-action": "room", "data-room": room.id }, [
            node("span", { class: "room__label" }, room.label),
            node("span", { class: "room__source" }, [
              node("span", { class: "led", "aria-hidden": "true" }),
              node("span", { class: "room__source-text" })
            ])
          ])
        ]));
      });
    });
  }

  function updateRoomLists() {
    const current = session.current();
    all("[data-room-list] .room").forEach((button) => {
      const room = session.room(button.getAttribute("data-room"));
      const shown = session.view(room, session.state(room.id));
      button.setAttribute("aria-current", room === current ? "true" : "false");
      button.setAttribute("data-led", shown.led);
      button.setAttribute("aria-label", room.label + ", " + shown.spoken);
      button.querySelector(".room__source-text").textContent = shown.short;
    });
  }

  // The bubbles show what is on; only a source that cannot be known needs words.
  function updateAlert() {
    const current = session.current();
    const shown = session.view(current, session.state(current.id));
    $("#alert").hidden = !shown.problem;
    $("#alert-text").textContent = shown.problem || "";
  }

  function updateControls() {
    const list = session.controls();
    const target = session.activeTarget(list);
    updateTargets(list, target);
    const key = session.current().id + "/" + (target ? target.id : "");
    if (key !== drawn) {
      drawn = key;
      drawLayout(target);
    }
    updateKeys(target);
    updateInfo();
  }

  function bubble(item) {
    const remote = item.remote ? layouts.remotes[item.remote] : null;
    const tab = node("button", {
      type: "button", role: "tab", class: "target", id: "target-" + item.id, "aria-controls": "controls",
      "data-action": "bubble", "data-device": item.id, "data-role": item.role, "data-choose": item.choose ? "" : null,
      "aria-keyshortcuts": item.digit || null,
      title: remote ? item.label + " · " + remote.label : "Turn the room off"
    }, [
      node("span", { class: "led", "aria-hidden": "true" }),
      node("span", { class: "target__label" }, item.label)
    ]);
    tab.item = item;
    return tab;
  }

  function updateTargets(list, target) {
    const current = session.current();
    const box = $("[data-targets]");
    const items = session.bubbles(list);
    const signature = current.id + "|" + items.map((item) => item.id + ":" + item.role).join(",");
    const fresh = box.signature !== signature;
    if (fresh) {
      box.signature = signature;
      box.textContent = "";
      items.forEach((item, index) => {
        if (index && Session.DEVICE_ROLE[item.role] && !Session.DEVICE_ROLE[items[index - 1].role]) {
          box.appendChild(node("span", { class: "targets__gap", "aria-hidden": "true" }));
        }
        box.appendChild(bubble(item));
      });
    }
    box.parentNode.hidden = !items.length;
    const state = session.state(current.id);
    const selected = target ? target.id : null;
    let lit = null;
    all("[data-targets] .target").forEach((tab) => {
      const shown = session.bubbleState(tab.item, state);
      tab.setAttribute("data-state", shown);
      tab.setAttribute("aria-selected", tab.item.id === selected ? "true" : "false");
      tab.setAttribute("aria-label", session.bubbleLabel(tab.item, shown));
      if (shown !== "idle") lit = tab;
    });
    const litId = lit ? lit.item.id : null;
    if (fresh || box.lit !== litId) {
      box.lit = litId;
      reveal(box, lit, fresh);
    }
    markOverflow();
  }

  // Keep what the room shows in sight, clear of the fade at the row's ends
  // (remote.css): a new room's row starts at Off, and a change scrolls only
  // as far as it must.
  function reveal(box, tab, fromStart) {
    const edge = 24;
    let left = fromStart ? 0 : box.scrollLeft;
    if (tab && box.scrollWidth > box.clientWidth) {
      if (tab.offsetLeft - edge < left) left = tab.offsetLeft - edge;
      else if (tab.offsetLeft + tab.offsetWidth + edge > left + box.clientWidth) left = tab.offsetLeft + tab.offsetWidth + edge - box.clientWidth;
    }
    const still = fromStart || window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    box.scrollTo({ left: Math.max(0, left), behavior: still ? "auto" : "smooth" });
  }

  // A mouse wheel moves the row sideways (a trackpad's sideways swipe already
  // does), until it reaches an end.
  function wheelTargets(event) {
    const box = event.currentTarget;
    if (event.ctrlKey || Math.abs(event.deltaX) >= Math.abs(event.deltaY)) return;
    const delta = event.deltaY * (event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? box.clientWidth : 1);
    const end = box.scrollWidth - box.clientWidth;
    if (end <= 0 || (delta < 0 && box.scrollLeft <= 0) || (delta > 0 && box.scrollLeft >= end)) return;
    event.preventDefault();
    box.scrollLeft += delta;
  }

  function scrollTargets(step) {
    const box = $("[data-targets]");
    const still = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    box.scrollBy({ left: step * Math.max(80, box.clientWidth - 96), behavior: still ? "auto" : "smooth" });
  }

  // The row overflows when its bubbles are wider than the whole well, arrows
  // included, so showing the arrows never keeps them up by itself.
  function markOverflow() {
    const box = $("[data-targets]");
    const wrap = box.parentNode;
    wrap.toggleAttribute("data-overflow", !wrap.hidden && box.scrollWidth > wrap.clientWidth + 1);
    const left = box.scrollLeft > 2;
    const right = box.scrollLeft + box.clientWidth < box.scrollWidth - 2;
    if (left || right) box.setAttribute("data-more", left && right ? "both" : left ? "left" : "right");
    else box.removeAttribute("data-more");
    wrap.querySelector('[data-step="-1"]').disabled = !left;
    wrap.querySelector('[data-step="1"]').disabled = !right;
  }

  // The remote of the device the buttons control, in the frame every remote
  // shares: corners and D-pad, a row of keys, labelled keys, and beside the
  // volume the CH rocker and, under a D-pad, up to three playback keys.
  function drawLayout(target) {
    const box = $("[data-layout]");
    const playback = $("[data-playback]");
    const side = $("[data-side]");
    const panel = $("#controls");
    const auxiliary = $("[data-aux-layout]") || node("div", {
      class: "labels labels--grouped", "data-aux-layout": "", role: "group", "aria-label": "More buttons"
    });
    [box, playback, side, auxiliary].forEach((slot) => { slot.textContent = ""; });
    if (target) panel.setAttribute("aria-labelledby", "target-" + target.id);
    else panel.removeAttribute("aria-labelledby");
    const remote = session.remoteOf(target);
    if (!remote) {
      box.appendChild(note("i-off", "Nothing to control", "Choose a source above"));
    } else if (remote.note) {
      box.appendChild(note(KIND_ICON[target.kind] || "i-pc", target.label, remote.note.replace("{device}", target.label)));
    } else {
      if (remote.corners) box.appendChild(node("div", { class: "cluster-fit" }, [cluster(remote.corners)]));
      if (remote.top) box.appendChild(row(remote.top, "keyrow", "Buttons", ""));
      const keys = remote.playback || [];
      if (remote.corners && keys.length <= 3) {
        keys.forEach((id) => playback.appendChild(key(id, PLAY[id] ? "key--play" : "key--sm")));
      } else if (keys.length) {
        const dense = keys.length > 5 ? " keyrow--dense" : "";
        box.appendChild(row(keys, "keyrow keyrow--playback" + dense, "Playback", remote.corners ? "key--sm" : ""));
      }
      if (remote.keys) {
        if (remote.keyGroups) {
          const primary = node("div", { class: "labels labels--grouped" });
          remote.keyGroups.forEach((group) => {
            const grid = node("div", { class: "keygroup", role: "group", "aria-label": group.label,
              "data-key-group": group.label, style: "--key-columns:" + group.columns }, group.keys.map((id) => key(id)));
            (group.primary ? primary : auxiliary).appendChild(grid);
          });
          if (primary.childNodes.length) box.appendChild(primary);
        } else {
          box.appendChild(node("div", { class: "labels", role: "group", "aria-label": "More buttons" }, remote.keys.map((id) => key(id))));
        }
      }
      if (remote.channel) side.appendChild(rocker("channel_up", "channel_down", "CH", "Channel", "channel"));
    }
    auxiliary.hidden = !auxiliary.childNodes.length;
    panel.appendChild(auxiliary);
    // Labelled keys fit about four to a row.
    const labelRows = remote && remote.keyGroups
      ? remote.keyGroups.filter((group) => group.primary).reduce((sum, group) => sum + Math.ceil(group.keys.length / group.columns), 0)
      : Math.ceil(((remote && remote.keys) || []).length / 4);
    const rows = box.querySelectorAll(".keyrow").length + labelRows;
    const dpad = Boolean(remote && remote.corners);
    box.classList.toggle("layout--tall", dpad && rows === 2);
    box.classList.toggle("layout--taller", dpad && rows > 2);
    playback.hidden = !playback.childNodes.length;
    $("[data-lower]").classList.toggle("lower--playback", !playback.hidden);
    all(".remote [data-button]").forEach((element) => {
      const shortcuts = hints[element.getAttribute("data-button")];
      if (shortcuts) element.setAttribute("aria-keyshortcuts", shortcuts.join(" "));
    });
  }

  function updateKeys(target) {
    const audio = session.current().audio;
    const remote = session.remoteOf(target);
    all(".remote [data-button]").forEach((element) => {
      const id = element.getAttribute("data-button");
      const label = session.spec(id).label;
      const to = session.drives(id, target);
      element.setAttribute("aria-label", label + ". " + to);
      element.setAttribute("title", label + " → " + to);
    });
    $("[data-caption=audio]").textContent = audio ? audio.short : "No audio";
    all("[data-caption=channel]").forEach((caption) => { caption.textContent = target ? target.short : ""; });
    $("[data-caption=source]").textContent = remote && !remote.note ? target.label + " · " + remote.label : "";
    $("#remote").setAttribute("data-audio", audio ? "live" : "idle");
  }

  // The volume keys or the slider, and Mute lit while the room's audio is muted.
  function updateAudio() {
    if (!session.current()) return;
    const view = session.audioView();
    const mute = $(".key--mute");
    if (view.muted !== null) mute.setAttribute("aria-pressed", view.muted ? "true" : "false");
    else mute.removeAttribute("aria-pressed");
    all("[data-action=volume-mode]").forEach((toggle) => {
      toggle.setAttribute("aria-checked", view.slider ? "true" : "false");
    });
    $("[data-lower]").classList.toggle("lower--fader", view.slider);
    $(".rocker-wrap--volume").hidden = view.slider;
    $("[data-fader]").hidden = !view.slider;
    if (!view.slider) return;
    const range = $("[data-fader-input]");
    range.max = String(view.max);
    range.min = String(view.min);
    range.step = String(view.step);
    range.disabled = view.disabled || !session.controlAllowed();
    if (view.value !== null && !view.dragging) range.value = String(view.value);
    range.style.setProperty("--fill", view.fill + "%");
    range.setAttribute("aria-label", view.label);
    range.setAttribute("aria-valuetext", view.text);
    $("[data-caption=fader]").textContent = view.caption;
  }

  function key(id, extra) {
    const button = session.spec(id);
    const classes = ["key"].concat(button.text ? ["key--text"] : [], extra ? [extra] : []);
    return node("button", {
      type: "button", class: classes.join(" "), "data-button": id, "data-repeat": button.repeat ? "" : null,
      "aria-label": button.label
    }, button.icon ? icon("i-" + button.icon) : node("span", { class: "key__text" }, button.text));
  }

  function cluster(corners) {
    return node("div", { class: "cluster" }, corners.map((id, index) => (id
      ? key(id, "corner corner--" + CORNERS[index])
      : node("span", { class: "corner corner--" + CORNERS[index], "aria-hidden": "true" }))).concat([dpad()]));
  }

  function dpad() {
    const spec = session.spec;
    return node("div", { class: "dpad", role: "group", "aria-label": "Navigation" }, ["up", "right", "down", "left"].map((id) =>
      node("button", {
        type: "button", class: "dpad__dir dpad__dir--" + id, "data-button": id, "data-repeat": "", "aria-label": spec(id).label
      }, [icon("i-" + spec(id).icon, "dpad__glyph")])).concat([
      node("button", { type: "button", class: "dpad__ok", "data-button": "ok", "aria-label": spec("ok").label }, spec("ok").text)
    ]));
  }

  function row(ids, className, label, size) {
    return node("div", { class: className, role: "group", "aria-label": label },
      ids.map((id) => key(id, PLAY[id] && !size ? "key--play" : size)));
  }

  function rocker(up, down, text, label, name) {
    const half = (id, end) => node("button", {
      type: "button", class: "rocker__half rocker__half--" + end, "data-button": id, "data-repeat": "", "aria-label": session.spec(id).label
    }, [icon("i-" + session.spec(id).icon)]);
    return node("div", { class: "rocker-wrap rocker-wrap--" + name }, [
      node("div", { class: "rocker rocker--" + name, role: "group", "aria-label": label }, [
        half(up, "up"),
        node("span", { class: "rocker__label", "aria-hidden": "true" }, text),
        half(down, "down")
      ]),
      node("p", { class: "caption caption--small", "data-caption": name })
    ]);
  }

  function note(iconId, title, body) {
    return node("div", { class: "note-card" }, [
      icon(iconId),
      node("p", { class: "note-card__title" }, title),
      node("p", { class: "note-card__body" }, body)
    ]);
  }

  function buildInfo() {
    all("[data-info]").forEach((info) => {
      info.textContent = "";
      info.appendChild(node("section", { class: "info__section" }, [
        node("h3", { class: "info__title" }, "What the buttons control"),
        node("dl", { class: "map", "data-map": "" })
      ]));
      info.appendChild(node("section", { class: "info__section" }, [
        node("h3", { class: "info__title" }, "Volume"),
        node("button", { type: "button", class: "switch switch--setting", role: "switch", "aria-checked": "false", "data-action": "volume-mode" }, [
          node("span", { class: "switch__text" }, [
            node("span", { class: "switch__label" }, "Volume slider"),
            node("span", { class: "switch__hint" }, "Off: the + and \u2212 keys")
          ]),
          node("span", { class: "switch__track", "aria-hidden": "true" })
        ])
      ]));
      info.appendChild(node("section", { class: "info__section", "data-room-controls-section": "" }, [
        node("h3", { class: "info__title" }, "Room controls"),
        node("div", { class: "room-controls", "data-room-controls": "" }),
        node("p", { class: "info__note" }, "Not connected yet. Later, turning the room on and off will run them; these buttons stay as a backup.")
      ]));
      info.prepend(node("section", { class: "info__section" }, [
        node("h3", { class: "info__title" }, "Devices in the room"),
        node("table", { class: "devices" }, [
          node("thead", null, node("tr", null, [node("th", { scope: "col" }, "Device"), node("th", { scope: "col" }, "Role")])),
          node("tbody", { "data-devices": "" })
        ])
      ]));
      if (!session.demo) return;
      info.appendChild(node("section", { class: "info__section" }, [
        node("h3", { class: "info__title" }, "Demo: mock backend"),
        node("button", { type: "button", class: "switch", role: "switch", "aria-checked": "true", "data-action": "simulate-matrix" }, [
          node("span", { class: "switch__text" }, [
            node("span", { class: "switch__label" }, "HDMI matrix answers"),
            node("span", { class: "switch__hint" }, "Off: rooms read back UNDEF")
          ]),
          node("span", { class: "switch__track", "aria-hidden": "true" })
        ]),
        node("button", { type: "button", class: "button", "data-action": "simulate-external" }, "Switch at the matrix"),
        node("p", { class: "info__note" }, [
          "Source Item ",
          node("code", { "data-item-state": "" })
        ])
      ]));
    });
    all("[data-info]").forEach((info) => {
      const devices = node("div", { "data-info-devices": "" });
      while (info.firstChild) devices.appendChild(info.firstChild);
      info.appendChild(node("div", { class: "info__tabs", role: "group", "aria-label": "Room information" }, [
        node("button", { type: "button", "data-action": "info-view", "data-view": "devices", "aria-pressed": "true" }, "Devices"),
        node("button", { type: "button", "data-action": "info-view", "data-view": "signal", "aria-pressed": "false" }, "Signal & audio")
      ]));
      info.appendChild(devices);
      info.appendChild(node("section", { class: "signal", "data-signal": "", tabindex: "0", "aria-label": "Signal and audio information", hidden: "" }));
      if (info.closest(".panel--info")) info.prepend(node("button", { type: "button", class: "info__signal-link",
        "data-action": "info-view", "data-view": "signal" }, "Signal & audio · D"));
    });
  }

  function inlineSignal() {
    const panel = $(".panel--signal");
    return panel && getComputedStyle(panel).display !== "none";
  }

  function signalVisible() {
    const sheet = $("#info-sheet");
    const dialogs = all("dialog[open]");
    return document.visibilityState !== "hidden" && (dialogs.length
      ? dialogs.every((dialog) => dialog === sheet) && infoView === "signal" : inlineSignal());
  }

  function syncSignal() {
    const visible = signalVisible();
    if (visible && !signalRunning) {
      signalRunning = true;
      session.openDiagnostics().then(updateSignal);
    } else if (!visible && signalRunning) {
      signalRunning = false;
      session.closeDiagnostics();
    }
  }

  function setInfoView(view, open) {
    infoView = view === "signal" ? "signal" : "devices";
    if (inlineSignal() && infoView === "signal") {
      if ($("#info-sheet").open) $("#info-sheet").close();
      $(".panel--signal [data-signal]").focus({ preventScroll: true });
      infoView = "devices";
    }
    if (view === "signal" && open) signalFocus = true;
    all("[data-info-devices]").forEach((element) => { element.hidden = Boolean(element.closest("#info-sheet")) && infoView !== "devices"; });
    all("[data-info] [data-signal]").forEach((element) => { element.hidden = !element.closest("#info-sheet") || infoView !== "signal"; });
    all("[data-action=info-view]").forEach((button) => {
      button.setAttribute("aria-pressed", button.getAttribute("data-view") === infoView ? "true" : "false");
    });
    if (open && view === "signal" && !inlineSignal()) openSheet("info-sheet");
    syncSignal();
    updateSignal();
    if (open) all("[data-signal]").find((element) => element.offsetParent !== null)?.focus({ preventScroll: true });
  }

  function updateSignal() {
    const model = session.diagnostics();
    all("[data-signal]").forEach((panel) => {
      const context = session.current().id + ":" + session.state(session.current().id).value;
      if (panel.dataset.context !== context) {
        const scroller = panel.closest(".panel--signal") || panel.closest(".sheet");
        if (scroller) scroller.scrollTop = 0;
        panel.dataset.context = context;
      }
      panel.textContent = "";
      panel.appendChild(node("h3", { class: "info__title" }, "Signal & audio"));
      panel.appendChild(node("p", { class: "signal__room" }, session.current().label));
      if (!model) {
        panel.appendChild(node("p", { class: "info__note" }, "Reading the confirmed signal path\u2026"));
        return;
      }
      const confirmed = session.state(session.current().id).value;
      const source = session.current().options.find((option) => option.id === confirmed);
      panel.appendChild(node("p", { class: "signal__source" }, model.source ? model.source.label
        : source ? source.label : confirmed === "off" ? "Off" : "Source unknown"));
      if (model.route && model.route.length) panel.appendChild(node("p", { class: "signal__route" }, model.route.map((step) => step.label).join(" → ")));
      if (model.audioRoute && model.audioRoute.length) panel.appendChild(node("p", { class: "signal__route" }, "Audio · " + model.audioRoute.map((step) => step.label).join(" → ")));
      if (model.pending) panel.appendChild(node("p", { class: "info__note" }, "Switching to " + model.pending.label + "; readings still describe the confirmed source."));
      const groups = [["video", "Current video"], ["audio", "Current audio"], ["path", "Configured path"],
        ["edid", "EDID capabilities"], ["processing", "Processing & settings"]];
      groups.forEach(([id, title]) => {
        const section = node("section", { class: "signal__section" }, node("h4", { class: "info__title" }, title));
        if (id === "edid") section.appendChild(node("p", { class: "info__note" }, "Advertised support, not the current format."));
        const body = node("tbody");
        const list = node("table", { class: "signal__table" }, [
          node("thead", null, node("tr", null, [node("th", { scope: "col" }, "Report"), node("th", { scope: "col" }, "Value"), node("th", { scope: "col" }, "Age")])), body]);
        (model.sections[id] || []).forEach((entry) => {
          const value = entry.availability === "reported" ? entry.value : entry.availability === "unavailable" ? "Unavailable"
            : entry.availability === "not_reported" && entry.value === "Awaiting fresh report" ? "Awaiting fresh report" : "Not reported";
          const detail = node("td", { class: "signal__value", "data-availability": entry.availability }, node("span", null, value));
          if (entry.detail) detail.appendChild(node("small", { class: "signal__meta" }, entry.detail));
          const age = entry.basis === "configured" ? "Config." : entry.availability !== "reported" ? "—"
            : !Number.isFinite(entry.observedAt) ? "Unknown" : Math.max(0, Math.floor((Date.now() - entry.observedAt) / 1000)) + "s" + (entry.stale ? " · stale" : "");
          body.appendChild(node("tr", null, [node("th", { scope: "row" }, entry.label), detail,
            node("td", { class: "signal__age" + (entry.stale ? " signal__meta--stale" : "") }, age)]));
        });
        if (!body.children.length) body.appendChild(node("tr", null, node("td", { colspan: "3" }, "Not reported")));
        section.appendChild(list);
        panel.appendChild(section);
      });
    });
  }

  function buildShortcuts() {
    all("[data-shortcuts]").forEach((list) => {
      list.textContent = "";
      Session.SHORTCUTS.concat([[["D"], "Signal & audio"], [["L"], "Log"]]).forEach((entry) => {
        const item = node("li");
        entry[0].forEach((key, index) => {
          if (index && entry[2]) item.appendChild(document.createTextNode(entry[2]));
          item.appendChild(node("kbd", null, key));
        });
        item.appendChild(node("span", null, entry[1]));
        list.appendChild(item);
      });
    });
  }

  function updateInfo() {
    const shown = session.info();
    all("[data-map]").forEach((map) => {
      map.textContent = "";
      shown.rows.forEach((row) => {
        map.appendChild(node("dt", null, row[0]));
        map.appendChild(node("dd", null, row[1]));
      });
    });
    all("[data-room-controls-section]").forEach((section) => { section.hidden = !shown.controls.length; });
    all("[data-room-controls]").forEach((box) => {
      box.textContent = "";
      shown.controls.forEach((control) => {
        box.appendChild(node("div", { class: "room-control", role: "group", "aria-label": control.label + ", " + control.device }, [
          node("div", { class: "room-control__head" }, [
            node("span", { class: "room-control__label" }, control.label),
            node("span", { class: "room-control__meta" }, control.device)
          ]),
          node("div", { class: "room-control__actions" }, control.actions.map((action) => node("button", {
            type: "button", class: "button", "data-action": "control", "data-control": control.id, "data-step": action.id,
            "aria-disabled": control.connected ? null : "true",
            "aria-label": control.label + " " + action.label + (control.connected ? "" : ", not connected")
          }, action.label)))
        ]));
      });
    });
    all("[data-devices]").forEach((list) => {
      list.textContent = "";
      if (!shown.devices.length) list.appendChild(node("tr", null, node("td", { colspan: "2" }, "No devices in the inventory")));
      shown.devices.forEach((device) => {
        list.appendChild(node("tr", { class: "device" }, [
          node("td", { class: "device__name" }, [device.label, node("small", { class: "device__meta" }, device.meta)]),
          node("td", { class: "device__roles" }, device.roles.join(" · ") || "—")
        ]));
      });
    });
  }

  function updateSimulation() {
    all("[data-action=simulate-matrix]").forEach((button) => {
      button.setAttribute("aria-checked", session.isAnswering() ? "true" : "false");
    });
    if (!session.current()) return;
    all("[data-item-state]").forEach((code) => { code.textContent = session.itemStateText(); });
  }

  // All off asks for its second press on the button itself: on a phone the
  // room sheet covers the toast.
  function showAllOff(armed) {
    all("[data-action=all-off]").forEach((button) => {
      button.toggleAttribute("data-armed", armed);
      button.querySelector("[data-all-off-text]").textContent = armed ? "Press again to confirm" : "All off";
    });
  }

  function openInputs(title) {
    $("#input-sheet-title").textContent = title;
    fillInputs();
    openSheet("input-sheet");
  }

  function fillInputs() {
    const list = $("[data-input-list]");
    list.textContent = "";
    session.inputOptions().forEach((entry) => {
      list.appendChild(node("li", null, [
        node("button", {
          type: "button", class: "room", "data-action": "input", "data-input": entry.value,
          "aria-current": entry.inUse ? "true" : "false", "data-led": entry.inUse ? "ok" : "off"
        }, [
          node("span", { class: "room__label" }, entry.label),
          node("span", { class: "room__source" }, entry.inUse ? [
            node("span", { class: "led", "aria-hidden": "true" }),
            node("span", { class: "room__source-text" }, "In use")
          ] : [])
        ])
      ]));
    });
  }

  // ---- input -------------------------------------------------------------

  function bind() {
    if (session.demo) ["cameras.html", "topology.html"].forEach((page) => {
      all('a[href="' + page + '"]').forEach((link) => link.setAttribute("href", page + "?backend=mock"));
    });
    document.addEventListener("click", onClick);
    document.addEventListener("pointerdown", onPointerDown);
    ["pointerup", "pointercancel"].forEach((type) => document.addEventListener(type, stopRepeat));
    document.addEventListener("keydown", onKey);
    document.addEventListener("focusin", (event) => {
      signalFocus = Boolean(event.target.closest && event.target.closest("[data-signal]"));
    });
    document.addEventListener("pointerdown", (event) => {
      if (!event.target.closest(".panel--signal, #info-sheet")) signalFocus = false;
    });
    // iOS Safari only applies :active styles once a touch listener exists.
    document.addEventListener("touchstart", () => {}, { passive: true });
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible") checkForUpdate();
      syncSignal();
    });

    $("#room-switch").addEventListener("click", () => openSheet("room-sheet"));
    const range = $("[data-fader-input]");
    range.addEventListener("input", () => session.slide(range.value, false));
    range.addEventListener("change", () => session.slide(range.value, true));
    range.addEventListener("pointerdown", () => session.setDragging(true));
    ["pointerup", "pointercancel"].forEach((type) => range.addEventListener(type, () => session.setDragging(false)));
    $("#info-open").addEventListener("click", () => openSheet("info-sheet"));
    $("[data-targets]").addEventListener("scroll", markOverflow, { passive: true });
    $("[data-targets]").addEventListener("wheel", wheelTargets, { passive: false });
    window.addEventListener("resize", markOverflow);
    window.addEventListener("resize", () => {
      if (inlineSignal() && $("#info-sheet").open && infoView === "signal") {
        $("#info-sheet").close();
        setInfoView("signal", true);
      } else if (!inlineSignal() && signalFocus && !$("#info-sheet").open) setInfoView("signal", true);
      syncSignal(); updateSignal();
    });
    window.addEventListener("pagehide", () => { signalRunning = false; session.closeDiagnostics(); });
    window.addEventListener("pageshow", syncSignal);
    $("#theme-toggle").addEventListener("click", () => {
      applyTheme(document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark", true);
    });
    all("dialog").forEach((dialog) => {
      dialog.addEventListener("close", () => {
        if (dialog.id === "info-sheet" && !inlineSignal()) signalFocus = false;
        syncSignal();
      });
      dialog.addEventListener("click", (event) => {
        if (event.target === dialog || event.target.closest("[data-close]")) dialog.close();
      });
    });
    const scheme = window.matchMedia("(prefers-color-scheme: light)");
    if (scheme.addEventListener) {
      scheme.addEventListener("change", (event) => {
        if (!store.get("remote.theme") && !params.get("theme")) applyTheme(event.matches ? "light" : "dark", false);
      });
    }
  }

  function onClick(event) {
    const key = event.target.closest("[data-button]");
    if (key) {
      // A held key pressed on pointerdown already; a click with no pointerdown
      // before it (keyboard, screen reader) presses here.
      const pressedAlready = key.pressedByPointer && event.detail > 0;
      key.pressedByPointer = false;
      if (pressedAlready) return;
      session.press(key.getAttribute("data-button"));
      if (event.detail === 0) flash(key);
      return;
    }
    const action = event.target.closest("[data-action]");
    if (!action) return;
    switch (action.getAttribute("data-action")) {
      case "info-view":
        setInfoView(action.getAttribute("data-view"), false);
        break;
      case "room": {
        session.selectRoom(action.getAttribute("data-room"), true);
        const dialog = action.closest("dialog");
        if (dialog) dialog.close();
        break;
      }
      case "bubble":
        session.tapBubble(action.item);
        break;
      case "control":
        session.operate(action.getAttribute("data-control"), action.getAttribute("data-step"));
        break;
      case "input":
        session.chooseInput(action.getAttribute("data-input"));
        action.closest("dialog").close();
        break;
      case "volume-mode":
        session.toggleVolumeMode();
        break;
      case "all-off":
        if (session.allOff()) {
          const dialog = action.closest("dialog");
          if (dialog) dialog.close();
        }
        break;
      case "simulate-matrix":
        session.toggleMatrix();
        break;
      case "simulate-external":
        session.simulateExternal();
        break;
      case "scroll-targets":
        scrollTargets(Number(action.getAttribute("data-step")));
        break;
    }
  }

  function onPointerDown(event) {
    lastInput = "pointer";
    if (event.pointerType === "mouse" && event.button !== 0) return;
    const target = event.target.closest("[data-button], [data-action=bubble], [data-action=room], [data-action=all-off]");
    if (!target) return;
    haptic();
    if (!target.hasAttribute("data-button")) return;
    target.classList.add("is-pressed");
    if (target.hasAttribute("data-repeat")) startRepeat(target);
  }

  function startRepeat(key) {
    stopRepeat();
    const button = key.getAttribute("data-button");
    key.pressedByPointer = true;
    session.press(button);
    repeat = { key: key, timer: setTimeout(function tick() {
      session.press(button);
      repeat.timer = setTimeout(tick, Session.REPEAT_EVERY);
    }, Session.REPEAT_DELAY) };
    key.addEventListener("pointerleave", stopRepeat, { once: true });
  }

  function stopRepeat() {
    all(".is-pressed").forEach((pressed) => {
      if (!pressed.pressTimer) pressed.classList.remove("is-pressed");
    });
    if (!repeat) return;
    clearTimeout(repeat.timer);
    repeat = null;
  }

  function onKey(event) {
    if (event.defaultPrevented) return;
    if (event.ctrlKey || event.metaKey || event.altKey) return;
    if (event.key === "d" || event.key === "D") {
      event.preventDefault();
      setInfoView("signal", true);
      return;
    }
    if (document.querySelector("dialog[open]")) return;
    if (event.target.closest && event.target.closest("[data-signal], [data-action=info-view]")) return;
    if (event.target.closest && event.target.closest("[data-log-history]")) return;
    if (RANGE_KEYS[event.key] && event.target === $("[data-fader-input]")) return;
    if (event.key === "Tab") {
      lastInput = "keyboard";
      return;
    }
    if (!session.current()) return;
    if (/^[0-9]$/.test(event.key)) {
      const choice = session.chooseDigit(event.key);
      if (!choice) return;
      event.preventDefault();
      all('[data-targets] [data-device="' + choice + '"]').forEach(flash);
      return;
    }
    if (event.key === "[" || event.key === "]") {
      event.preventDefault();
      session.stepRoom(event.key === "]" ? 1 : -1);
      return;
    }
    let buttons = Session.KEYS[event.key];
    if (event.key === "Enter") {
      const active = document.activeElement;
      const onControl = active && active !== document.body && active.matches("button, a, input, select, textarea, [tabindex]");
      // Keyboard users activate the focused control; after a click, Enter means OK.
      if (onControl && lastInput === "keyboard") return;
      buttons = ["ok"];
    }
    if (buttons) {
      event.preventDefault();
      const button = session.pressKey(buttons);
      const key = all('.remote [data-button="' + button + '"]').find((element) => element.offsetParent !== null);
      if (key) flash(key);
    }
  }

  function flash(element) {
    element.classList.add("is-pressed");
    clearTimeout(element.pressTimer);
    element.pressTimer = setTimeout(() => {
      element.pressTimer = 0;
      element.classList.remove("is-pressed");
    }, 140);
  }

  function haptic() {
    if (typeof navigator.vibrate === "function") {
      try { navigator.vibrate(10); } catch (error) { /* not allowed before a user gesture */ }
    }
  }

  // ---- chrome ------------------------------------------------------------

  function applyTheme(theme, save) {
    const root = document.documentElement;
    root.setAttribute("data-theme", theme);
    if (save) store.set("remote.theme", theme);
    const toggle = $("#theme-toggle");
    toggle.setAttribute("aria-label", "Switch to " + (theme === "dark" ? "light" : "dark") + " theme");
    toggle.querySelector("use").setAttribute("href", theme === "dark" ? "#i-sun" : "#i-moon");
    const chrome = getComputedStyle(root).getPropertyValue("--chrome").trim();
    const meta = $('meta[name="theme-color"]');
    if (meta && chrome) meta.setAttribute("content", chrome);
  }

  // openHAB serves /static/ with a year's max-age, and no openHAB setting
  // changes that, so a phone would keep running these files long after an
  // update. Whenever the page starts or comes back into view, compare each
  // file with the server's copy: the "no-cache" fetch also stores the new
  // copy, so one reload runs it.
  function checkForUpdate() {
    if (!/^https?:$/.test(window.location.protocol) || typeof fetch !== "function") return;
    if (Date.now() - checkedAt < UPDATE_EVERY) return;
    checkedAt = Date.now();
    const urls = [window.location.href.split("#")[0]]
      .concat(all("script[src], link[rel=stylesheet][href]").map((element) => element.src || element.href));
    Promise.all(urls.map(changedOnServer))
      .then((changed) => {
        if (!changed.some(Boolean)) return;
        // A reload that did not bring the new files must not become a loop.
        if (Date.now() - Number(store.get("remote.updated") || 0) < UPDATE_EVERY) return;
        store.set("remote.updated", String(Date.now()));
        window.location.reload();
      })
      .catch(() => { /* offline or openHAB restarting: the next check tries again */ });
  }

  function changedOnServer(url) {
    return fetch(url, { cache: "force-cache" })
      .then((cached) => cached.text())
      .then((running) => fetch(url, { cache: "no-cache" })
        .then((latest) => (latest.ok ? latest.text() : running))
        .then((text) => text !== running));
  }

  function openSheet(id) {
    const dialog = document.getElementById(id);
    if (typeof dialog.showModal === "function") dialog.showModal();
    else dialog.setAttribute("open", "");
    const selected = dialog.querySelector('[aria-current="true"]');
    if (selected) selected.focus();
    syncSignal();
  }

  function toast(title, body, tone, tag) {
    const box = $("#toast");
    box.textContent = "";
    box.appendChild(node("span", { class: "toast__title" }, [node("strong", null, title), tag ? node("span", { class: "toast__tag" }, tag) : null]));
    if (body) box.appendChild(node("span", { class: "toast__body" }, body));
    box.setAttribute("data-tone", tone || "");
    box.classList.add("is-visible");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => box.classList.remove("is-visible"), 1900);
  }

  function announce(message) {
    const region = $("#announcer");
    region.textContent = "";
    setTimeout(() => { region.textContent = message; }, 60);
  }

  function fail(error) {
    console.error(error);
    $("#alert-text").textContent = "The remote could not start: " + String(error && error.message || error);
    $("#alert").hidden = false;
  }

  // ---- helpers -----------------------------------------------------------

  function numberParam(name) {
    const raw = params.get(name);
    if (raw === null || raw === "") return undefined;
    const value = Number(raw);
    return Number.isFinite(value) && value >= 0 ? value : undefined;
  }

  function storage() {
    try {
      const local = window.localStorage;
      local.getItem("remote.theme");
      return {
        get: (key) => local.getItem(key),
        set: (key, value) => { try { local.setItem(key, value); } catch (error) { /* quota or private mode */ } }
      };
    } catch (error) {
      return Session.memoryStore();
    }
  }

  function $(selector) {
    return document.querySelector(selector);
  }

  function all(selector) {
    return Array.prototype.slice.call(document.querySelectorAll(selector));
  }

  function node(tag, attributes, children) {
    const element = document.createElement(tag);
    Object.keys(attributes || {}).forEach((name) => {
      if (attributes[name] !== null && attributes[name] !== undefined) element.setAttribute(name, attributes[name]);
    });
    [].concat(children === undefined ? [] : children).forEach((child) => {
      if (child === null || child === undefined) return;
      element.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
    });
    return element;
  }

  function icon(id, extra) {
    const holder = document.createElement("span");
    holder.innerHTML = '<svg class="icon' + (extra ? " " + extra : "") + '" aria-hidden="true" focusable="false"><use href="#'
      + id + '"></use></svg>';
    return holder.firstChild;
  }
})();
