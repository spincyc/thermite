/*
 * The Audio page: draws a RemoteAudio session (audio-session.js) and hands
 * it taps and keys. What the page does without a browser lives in the
 * session; this file holds only the DOM.
 */
(function () {
  "use strict";

  const house = globalThis.REMOTE_HOUSE;
  const Remote = globalThis.RemoteBackend;
  const params = new URLSearchParams(window.location.search);
  const access = globalThis.ThermiteAccess && globalThis.ThermiteAccess.client;
  const backend = Remote.create(house, {
    backend: { mock: "mock", openhab: "openhab" }[params.get("backend")],
    active: document.visibilityState !== "hidden"
  });
  const session = globalThis.RemoteAudio.create({ backend: backend, house: house, access: access, store: storage() });
  const syncLive = () => { if (backend.setActive) backend.setActive(document.visibilityState !== "hidden"); };
  document.addEventListener("visibilitychange", syncLive);
  window.addEventListener("pagehide", () => { if (backend.setActive) backend.setActive(false); });
  window.addEventListener("pageshow", syncLive);

  // A choice with this many options or fewer, each short, is a row of
  // buttons; a longer one is a list.
  const SEGMENTS = 4;
  const SEGMENT_LABEL = 12;

  const $ = (selector) => document.querySelector(selector);
  const volume = $("[data-volume]");
  let dragging = false;
  let toastTimer = 0;
  // Each block's last drawing, by what it showed: a block is built again only
  // when that changes, so a poll elsewhere keeps an open list, a tap under
  // way and a screen reader's place.
  const drawn = new WeakMap();

  $("[data-mock]").hidden = !backend.simulateExternalSwitch;
  session.subscribe((event) => {
    if (event.type === "change") draw();
    else if (event.type === "toast") toast(event.title, event.body, event.tone);
    else if (event.type === "announce") announce(event.message);
  });
  session.start(params.get("device")).then(draw).catch((error) => {
    console.error(error);
    showAlert("The Audio page could not start: " + String(error && error.message || error));
  });

  // ---- input --------------------------------------------------------------

  $("[data-devices]").addEventListener("click", (event) => {
    const button = event.target.closest("[data-device-id]");
    if (button) session.select(button.dataset.deviceId);
  });
  $("[data-transport]").addEventListener("click", (event) => {
    const button = event.target.closest("[data-key]");
    if (button) session.press(button.dataset.key);
  });
  document.querySelectorAll("[data-volume-step]").forEach((button) => {
    button.addEventListener("click", () => session.stepVolume(Number(button.dataset.volumeStep)));
  });
  volume.addEventListener("input", () => { dragging = true; session.setVolume(volume.value); });
  volume.addEventListener("change", () => { dragging = false; session.setVolume(volume.value); });
  volume.addEventListener("pointerup", () => { dragging = false; });
  volume.addEventListener("blur", () => { dragging = false; });
  $("[data-mute]").addEventListener("click", () => session.toggleMute());
  $("[data-power]").addEventListener("click", () => session.togglePower());
  $("[data-inputs]").addEventListener("click", (event) => {
    const button = event.target.closest("[data-value]");
    if (button) session.chooseInput(button.dataset.value);
  });
  $("[data-sections]").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-item]");
    if (!button) return;
    if (button.dataset.step) session.step(button.dataset.item, Number(button.dataset.step));
    else if (button.dataset.value) session.set(button.dataset.item, button.dataset.value);
    else session.toggle(button.dataset.item);
  });
  $("[data-sections]").addEventListener("change", (event) => {
    if (event.target.matches("select[data-item]")) session.set(event.target.dataset.item, event.target.value);
  });
  // The arrows move along a row of choices; Enter or Space takes one.
  document.addEventListener("keydown", (event) => {
    if (!["ArrowLeft", "ArrowRight"].includes(event.key) || !event.target.matches("[role=radio]")) return;
    const row = Array.from(event.target.parentElement.querySelectorAll("[role=radio]:not(:disabled)"));
    const next = row[row.indexOf(event.target) + (event.key === "ArrowRight" ? 1 : -1)];
    if (next) next.focus();
    event.preventDefault();
  });

  // Keys a remote has: Space or P plays and pauses, M mutes, + and − step
  // the volume, [ and ] choose the previous or next device.
  document.addEventListener("keydown", (event) => {
    if (event.altKey || event.ctrlKey || event.metaKey || event.target.closest("input, select, textarea, summary")) return;
    const view = session.view();
    if (!view) return;
    const key = event.key;
    if ((key === "p" || key === "P" || key === " " && !event.target.closest("button"))
      && view.transport.some((button) => button.id === "play_pause")) session.press("play_pause");
    else if (key === "m" || key === "M") session.toggleMute();
    else if (key === "+" || key === "=") session.stepVolume(1);
    else if (key === "-" || key === "_") session.stepVolume(-1);
    else if (key === "[" || key === "]") stepDevice(key === "]" ? 1 : -1);
    else return;
    event.preventDefault();
  });

  function stepDevice(direction) {
    const ids = session.devices().flatMap((group) => group.devices.map((device) => device.id));
    const index = ids.indexOf(session.selected());
    if (ids.length > 1) session.select(ids[(index + direction + ids.length) % ids.length]);
  }

  // ---- drawing --------------------------------------------------------------

  function draw() {
    const groups = session.devices();
    $("[data-empty]").hidden = groups.length > 0;
    drawDevices(groups);
    const view = session.view();
    $("[data-device]").hidden = !view;
    if (!view) return;
    const focused = document.activeElement && document.activeElement.dataset ? document.activeElement.dataset.focus : null;
    $("[data-device-name]").textContent = view.device.name;
    $("[data-controls]").disabled = Boolean(view.locked);
    drawAlert(view);
    drawNow(view);
    drawVolume(view);
    drawInputs(view);
    drawSections(view);
    drawFacts(view);
    // A redrawn control keeps the focus it had; a step at its end, its group's other.
    if (focused && (!document.activeElement || document.activeElement === document.body)) {
      const again = document.querySelector('[data-focus="' + CSS.escape(focused) + '"]');
      const target = again && again.disabled ? again.parentElement.querySelector("button:not(:disabled)") : again;
      if (target) target.focus();
    }
  }

  // Whether element must be drawn again to show value; notes that it will be.
  function stale(element, value) {
    const signature = JSON.stringify(value);
    if (drawn.get(element) === signature) return false;
    drawn.set(element, signature);
    return true;
  }

  // One bubble per device, by its place.
  function drawDevices(groups) {
    const devices = groups.flatMap((group) => group.devices);
    const list = $("[data-devices]");
    if (!stale(list, devices)) return;
    $("[data-picker]").hidden = devices.length < 2;
    list.textContent = "";
    devices.forEach((device) => {
      const state = device.playing ? "current" : device.playing === false ? "" : "unknown";
      list.appendChild(node("button", {
        class: "target", type: "button", "data-device-id": device.id, "data-focus": "device:" + device.id,
        "aria-current": device.current ? "true" : "false", "data-state": state,
        "aria-label": device.name + (device.playing ? ", playing" + (device.title ? " " + device.title : "") : "")
          + (device.locked ? ", locked" : "")
      }, [node("span", { class: "led", "aria-hidden": "true" }), device.name,
        device.locked ? icon("lock", "target__lock audio__lock") : null]));
    });
  }

  function drawAlert(view) {
    const text = view.locked ? view.locked.text
      : view.connected === false ? "Not connected to thermite. Reconnecting…"
        : view.problem ? view.device.label + " " + view.problem : "";
    showAlert(text, view.locked ? "lock" : "warn");
  }

  function drawNow(view) {
    $("[data-place]").textContent = view.device.place;
    $("[data-model]").textContent = view.device.model ? " · " + view.device.model : "";
    const track = view.track;
    $("[data-title]").textContent = view.now;
    $("[data-now]").dataset.playing = view.playing ? "true" : "false";
    $("[data-line]").textContent = track && track.line || "";
    $("[data-line]").hidden = !(track && track.line);
    const badges = $("[data-badges]");
    const marks = track ? [track.service, track.format, track.duration].filter(Boolean) : [];
    if (stale(badges, marks)) {
      badges.textContent = "";
      marks.forEach((text) => badges.appendChild(node("span", { class: "badge badge--quiet" }, text)));
      badges.hidden = !marks.length;
    }
    const transport = $("[data-transport]");
    if (!stale(transport, view.transport)) return;
    transport.textContent = "";
    view.transport.forEach((button) => {
      transport.appendChild(node("button", {
        class: "key" + (button.id === "play_pause" ? " key--play" : " key--sm"), type: "button", "data-key": button.id,
        "data-focus": "key:" + button.id, "aria-label": button.label, title: button.label
      }, icon(button.icon)));
    });
  }

  function drawVolume(view) {
    const level = view.volume;
    const bar = $(".audio__volume .fader");
    bar.hidden = !level;
    if (level) {
      volume.min = level.min;
      volume.max = level.max;
      volume.step = level.step;
      volume.disabled = level.disabled;
      // A slider being dragged shows the finger, not the device.
      if (!dragging && level.value !== null) volume.value = level.value;
      volume.style.setProperty("--fill", (dragging ? fill(level) : level.fill) + "%");
      volume.setAttribute("aria-valuetext", level.text);
      bar.dataset.pending = level.pending ? "true" : "false";
      document.querySelectorAll("[data-volume-step]").forEach((button) => { button.disabled = level.disabled; });
    }
    $("[data-volume-text]").textContent = level ? "Volume " + level.text + (view.muted ? " · muted" : "") : "";
    const mute = $("[data-mute]");
    mute.hidden = view.muted === null;
    mute.setAttribute("aria-pressed", String(Boolean(view.muted)));
    const power = $("[data-power]");
    power.hidden = view.power === null;
    power.setAttribute("aria-pressed", String(Boolean(view.power)));
  }

  function fill(level) {
    return level.max > level.min ? (Number(volume.value) - level.min) / (level.max - level.min) * 100 : 0;
  }

  function drawInputs(view) {
    const wrap = $("[data-inputs-wrap]");
    wrap.hidden = !view.input;
    const list = $("[data-inputs]");
    if (!view.input || !stale(list, view.input)) return;
    list.textContent = "";
    view.input.options.forEach((option) => list.appendChild(segment("input", option, view.input.pending)));
  }

  function segment(item, option, pending) {
    return node("button", {
      class: "segmented__option", type: "button", role: "radio", "data-value": option.value,
      "data-item": item === "input" ? null : item, "data-focus": item + ":" + option.value,
      "aria-checked": String(option.current), "data-pending": option.current && pending ? "true" : null
    }, option.label);
  }

  // Each section is built again only when one of its settings changes, in
  // place, unless which sections show changed.
  function drawSections(view) {
    const holder = $("[data-sections]");
    const shown = Array.from(holder.children);
    if (shown.length !== view.sections.length || view.sections.some((section, index) => shown[index].dataset.section !== section.id)) {
      holder.replaceChildren(...view.sections.map(sectionBlock));
      return;
    }
    view.sections.forEach((section, index) => { if (stale(shown[index], section)) shown[index].replaceWith(sectionBlock(section)); });
  }

  function sectionBlock(section) {
    const title = "audio-section-" + section.id;
    const block = node("section", { class: "audio__section", "aria-labelledby": title, "data-section": section.id }, [
      node("h3", { class: "audio__section-title", id: title }, section.title),
      node("div", { class: "settings" }, section.settings.map(settingRow))
    ]);
    stale(block, section);
    return block;
  }

  function settingRow(setting) {
    const id = "setting-" + setting.item;
    const row = node("div", { class: "setting setting--" + setting.kind, "data-pending": setting.pending ? "true" : null });
    if (setting.kind === "switch") {
      row.append(node("span", { class: "setting__label", id: id }, setting.label),
        node("button", { class: "switch", type: "button", role: "switch", "aria-checked": String(setting.on),
          "aria-labelledby": id, "data-item": setting.item, "data-focus": setting.item }, node("span", { class: "switch__knob" })));
    } else if (setting.kind === "number") {
      row.append(node("span", { class: "setting__label", id: id }, setting.label),
        node("div", { class: "stepper", role: "group", "aria-labelledby": id }, [
          node("button", { class: "stepper__step", type: "button", "data-item": setting.item, "data-step": "-1",
            "data-focus": setting.item + ":-1", "aria-label": setting.label + " down", disabled: setting.atMin ? "" : null }, icon("minus")),
          node("output", { class: "stepper__value", "aria-live": "off" }, setting.text),
          node("button", { class: "stepper__step", type: "button", "data-item": setting.item, "data-step": "1",
            "data-focus": setting.item + ":1", "aria-label": setting.label + " up", disabled: setting.atMax ? "" : null }, icon("plus"))
        ]));
    } else if (setting.options.length <= SEGMENTS && setting.options.every((option) => option.label.length <= SEGMENT_LABEL)) {
      row.classList.add("setting--wide");
      row.append(node("span", { class: "setting__label", id: id }, setting.label),
        node("div", { class: "segmented", role: "radiogroup", "aria-labelledby": id },
          setting.options.map((option) => segment(setting.item, option, setting.pending))));
    } else {
      const select = node("select", { class: "setting__select", "data-item": setting.item, "data-focus": setting.item, "aria-labelledby": id });
      // A value it only reads (a mode the player reports but takes no command for) shows, not offered.
      if (!setting.options.some((option) => option.current)) {
        select.appendChild(node("option", { value: setting.value, disabled: "", selected: "" }, setting.text));
      }
      setting.options.forEach((option) => select.appendChild(node("option", { value: option.value, selected: option.current ? "" : null }, option.label)));
      row.append(node("label", { class: "setting__label", id: id }, setting.label), select);
    }
    return row;
  }

  function drawFacts(view) {
    const wrap = $("[data-facts-wrap]");
    wrap.hidden = !view.facts.length;
    const list = $("[data-facts]");
    if (!stale(list, view.facts)) return;
    list.textContent = "";
    view.facts.forEach((fact) => list.append(node("dt", null, fact.label), node("dd", null, fact.text)));
  }

  // ---- shared -------------------------------------------------------------

  function showAlert(text, kind) {
    $("[data-alert]").hidden = !text;
    $("[data-alert-text]").textContent = text || "";
    $("[data-alert] use").setAttribute("href", "#i-" + (kind || "warn"));
  }

  function toast(title, body, tone) {
    const box = $("#toast");
    box.textContent = "";
    box.appendChild(node("span", { class: "toast__title" }, node("strong", null, title)));
    if (body) box.appendChild(node("span", { class: "toast__body" }, body));
    box.setAttribute("data-tone", tone || "");
    box.classList.add("is-visible");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => box.classList.remove("is-visible"), 2400);
  }

  function announce(message) {
    const region = $("#announcer");
    region.textContent = "";
    setTimeout(() => { region.textContent = message; }, 60);
  }

  function storage() {
    try {
      const local = window.localStorage;
      local.getItem("audio.device");
      return {
        get: (key) => local.getItem(key),
        set: (key, value) => { try { local.setItem(key, value); } catch (error) { /* quota or private mode */ } }
      };
    } catch (error) {
      return null;
    }
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
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    const use = document.createElementNS(svg.namespaceURI, "use");
    svg.setAttribute("class", "icon" + (extra ? " " + extra : ""));
    svg.setAttribute("aria-hidden", "true");
    svg.setAttribute("focusable", "false");
    use.setAttribute("href", "#i-" + id);
    svg.appendChild(use);
    return svg;
  }
})();
