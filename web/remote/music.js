/*
 * The Music page: draws a RemoteMusic session (music-session.js) and hands
 * it taps and keys. What the page does without a browser lives in the
 * session; this file holds only the DOM.
 */
(function () {
  "use strict";

  const params = new URLSearchParams(window.location.search);
  const access = globalThis.ThermiteAccess && globalThis.ThermiteAccess.client;
  const backend = globalThis.MusicBackend.create({
    backend: params.get("backend") === "mock" ? "mock" : undefined,
    access: access,
    active: document.visibilityState !== "hidden"
  });
  const session = globalThis.RemoteMusic.create({ backend: backend, access: access, store: storage() });
  const syncLive = () => backend.setActive(document.visibilityState !== "hidden");
  document.addEventListener("visibilitychange", syncLive);
  window.addEventListener("pagehide", () => backend.setActive(false));
  window.addEventListener("pageshow", syncLive);

  const KIND_ICON = { track: "note", album: "album", artist: "artist", playlist: "playlist", folder: "folder" };

  const $ = (selector) => document.querySelector(selector);
  const volume = $("[data-volume]");
  const groupVolume = $("[data-group-volume]");
  const seek = $("[data-seek]");
  const query = $("[data-query]");
  const dragging = { volume: false, group: false, seek: false };
  let toastTimer = 0;
  // The entries the lists show, by their buttons' data-item, and the one a sheet acts on.
  const shown = new Map();
  let chosenItem = null;
  // Whether Browse moved (a folder opened, or Back) and its focus is to follow.
  let browsing = false;
  // The speakers ticked in the group sheet, kept across its redraws.
  const picked = new Set();
  // Each block's last drawing, by what it showed: a block is built again only
  // when that changes, so a stream's frame keeps an open list, a tap under
  // way and a screen reader's place.
  const drawn = new WeakMap();

  $("[data-mock]").hidden = !backend.mock;
  session.subscribe((event) => {
    if (event.type === "change") draw();
    else if (event.type === "toast") toast(event.title, event.body, event.tone);
    else if (event.type === "announce") announce(event.message);
  });
  session.start(params.get("speaker")).then(draw).catch((error) => {
    console.error(error);
    showAlert("The Music page could not start: " + String(error && error.message || error));
  });
  // While it plays, the time gone moves on each second.
  setInterval(() => {
    const view = document.visibilityState !== "hidden" && session.view();
    if (view && view.playing && view.progress) drawProgress(view);
  }, 1000);

  // ---- input --------------------------------------------------------------

  document.querySelectorAll("[data-speakers], [data-sheet-speakers]").forEach((list) => {
    list.addEventListener("click", (event) => {
      const button = event.target.closest("[data-speaker-id]");
      if (!button) return;
      session.select(button.dataset.speakerId);
      const sheet = button.closest("dialog");
      if (sheet) sheet.close();
    });
  });
  $("[data-switch]").addEventListener("click", () => openSheet("speaker-sheet"));
  $("[data-play]").addEventListener("click", () => session.playPause());
  $("[data-previous]").addEventListener("click", () => session.previous());
  $("[data-next]").addEventListener("click", () => session.next());
  $("[data-shuffle]").addEventListener("click", () => session.toggleShuffle());
  $("[data-repeat]").addEventListener("click", () => session.cycleRepeat());
  document.querySelectorAll("[data-volume-step]").forEach((button) => {
    button.addEventListener("click", () => session.stepVolume(Number(button.dataset.volumeStep)));
  });
  document.querySelectorAll("[data-group-step]").forEach((button) => {
    button.addEventListener("click", () => session.stepGroupVolume(Number(button.dataset.groupStep)));
  });
  slider(volume, "volume", (value) => session.setVolume(value));
  slider(groupVolume, "group", (value) => session.setGroupVolume(value));
  // A position is sent once let go; while dragged, only the time shown moves.
  seek.addEventListener("input", () => {
    dragging.seek = true;
    $("[data-elapsed]").textContent = globalThis.RemoteMusic.clock(seek.value);
    seek.style.setProperty("--fill", (Number(seek.max) ? Number(seek.value) / Number(seek.max) * 100 : 0) + "%");
  });
  seek.addEventListener("change", () => { dragging.seek = false; session.seek(Number(seek.value)); });
  seek.addEventListener("pointerup", () => { dragging.seek = false; });
  seek.addEventListener("blur", () => { dragging.seek = false; });
  $("[data-group-open]").addEventListener("click", () => { picked.clear(); openSheet("group-sheet"); drawGroupSheet(); });

  $("[data-tabs]").addEventListener("click", (event) => {
    const button = event.target.closest("[data-tab]");
    if (button) session.setTab(button.dataset.tab);
  });
  // The arrows move along the tabs, and choose as they go.
  $("[data-tabs]").addEventListener("keydown", (event) => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    const tabs = Array.from(document.querySelectorAll("[data-tab]"));
    const index = tabs.indexOf(event.target);
    const next = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1
      : (index + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
    tabs[next].focus();
    session.setTab(tabs[next].dataset.tab);
    event.preventDefault();
  });
  query.addEventListener("input", () => session.setQuery(query.value));
  $("[data-search-form]").addEventListener("submit", (event) => { event.preventDefault(); session.searchNow(); });
  $("[data-kind]").addEventListener("change", (event) => session.setKind(event.target.value));
  $("[data-source]").addEventListener("change", (event) => session.setSource(event.target.value));
  $("[data-browse-back]").addEventListener("click", () => { browsing = true; session.back(); });
  document.querySelectorAll("[data-more]").forEach((button) => button.addEventListener("click", () => session.more()));
  $("[data-library]").addEventListener("click", (event) => {
    const all = event.target.closest("[data-all]");
    if (all) { session.setKind(all.dataset.all); return; }
    const button = event.target.closest("[data-item]");
    const item = button && shown.get(button.dataset.item);
    if (!item) return;
    // A folder only opens; anything that plays offers how.
    if (item.opens && !item.plays) {
      browsing = true;
      session.open(item);
    } else openItem(item);
  });
  $("[data-item-actions]").addEventListener("click", (event) => {
    const button = event.target.closest("[data-action]");
    if (!button || !chosenItem) return;
    $("#item-sheet").close();
    browsing = button.dataset.action === "open";
    session.act(chosenItem, button.dataset.action);
  });

  $("[data-candidates]").addEventListener("change", (event) => {
    if (!event.target.matches("input[type=checkbox]")) return;
    if (event.target.checked) picked.add(event.target.value);
    else picked.delete(event.target.value);
    drawGroupSheet();
  });
  $("[data-join]").addEventListener("click", () => {
    const ids = Array.from(picked);
    picked.clear();
    session.join(ids);
    drawGroupSheet();
  });
  $("[data-members]").addEventListener("click", (event) => {
    const button = event.target.closest("[data-leave]");
    if (button) session.leave(button.dataset.leave);
  });
  $("[data-group-end]").addEventListener("click", () => {
    const grouping = session.grouping();
    if (grouping) session.leave(grouping.leader);
  });
  $("[data-tidal-start]").addEventListener("click", () => session.tidalStart());
  $("[data-tidal-cancel]").addEventListener("click", () => session.tidalCancel());

  document.querySelectorAll("dialog").forEach((dialog) => {
    dialog.addEventListener("click", (event) => {
      if (event.target === dialog || event.target.closest("[data-close]")) dialog.close();
    });
  });

  // Keys a remote has: Space or P plays and pauses, + and − step the
  // volume, [ and ] choose the previous or next speaker, < and > the
  // previous or next track, S shuffles, R repeats, / searches.
  document.addEventListener("keydown", (event) => {
    if (event.altKey || event.ctrlKey || event.metaKey || event.target.closest("input, select, textarea, dialog")) return;
    const key = event.key;
    if (key === "/") {
      session.setTab("search");
      draw();
      query.focus();
    } else if (key === "[" || key === "]") session.selectNext(key === "]" ? 1 : -1);
    else if (!session.view()) return;
    else if (key === "p" || key === "P" || key === " " && !event.target.closest("button, a")) session.playPause();
    else if (key === "+" || key === "=") session.stepVolume(1);
    else if (key === "-" || key === "_") session.stepVolume(-1);
    else if (key === "<" || key === ",") session.previous();
    else if (key === ">" || key === ".") session.next();
    else if (key === "s" || key === "S") session.toggleShuffle();
    else if (key === "r" || key === "R") session.cycleRepeat();
    else return;
    event.preventDefault();
  });

  // A volume slider: a drag sends as the session lets it, the finger shown meanwhile.
  function slider(input, name, set) {
    input.addEventListener("input", () => { dragging[name] = true; set(input.value); });
    input.addEventListener("change", () => { dragging[name] = false; set(input.value); });
    input.addEventListener("pointerup", () => { dragging[name] = false; });
    input.addEventListener("blur", () => { dragging[name] = false; });
  }

  // ---- drawing --------------------------------------------------------------

  function draw() {
    const focused = document.activeElement && document.activeElement.dataset ? document.activeElement.dataset.focus : null;
    const status = session.status();
    const view = session.view();
    const blocks = session.speakers();
    const layout = $("[data-layout]");
    layout.hidden = !["ready", "unavailable"].includes(status.state);
    layout.toggleAttribute("data-alone", !view);
    drawAlert(status, view);
    drawSwitch(view, blocks);
    drawSpeakers($("[data-speakers]"), blocks);
    drawSpeakers($("[data-sheet-speakers]"), blocks);
    $("[data-now-panel]").hidden = !view;
    if (view) drawNow(view);
    drawLibrary(session.library());
    drawTidal(session.tidal());
    if ($("#group-sheet").open) drawGroupSheet();
    // A redrawn control keeps the focus it had.
    if (focused && (!document.activeElement || document.activeElement === document.body)) {
      const again = document.querySelector('[data-focus="' + CSS.escape(focused) + '"]');
      if (again && !again.disabled) again.focus();
    }
  }

  // Whether element must be drawn again to show value; notes that it will be.
  function stale(element, value) {
    const signature = JSON.stringify(value);
    if (drawn.get(element) === signature) return false;
    drawn.set(element, signature);
    return true;
  }

  // One line for the page: no music here, Music Assistant away, a lock or a lost connection.
  function drawAlert(status, view) {
    if (status.state !== "ready") return showAlert(status.text, "warn", status.state === "reading" ? "quiet" : "");
    if (view && view.groupLocked) return showAlert(view.groupLocked.text, "lock");
    showAlert(status.text, "warn");
  }

  function drawSwitch(view, blocks) {
    const button = $("[data-switch]");
    const count = blocks.reduce((sum, block) => sum + block.speakers.length, 0);
    button.disabled = count < 2 && Boolean(view);
    $("[data-switch-name]").textContent = view ? view.speaker.name : "Music";
    button.setAttribute("aria-label", view ? "Speaker: " + view.speaker.name + ". Choose another" : "Speakers");
    if (!count) button.disabled = true;
  }

  // The speakers, a group's together under what it plays.
  function drawSpeakers(list, blocks) {
    if (!stale(list, blocks)) return;
    list.textContent = "";
    blocks.forEach((block) => {
      if (!block.group) {
        list.appendChild(node("li", null, speakerButton(block.speakers[0], true)));
        return;
      }
      list.appendChild(node("li", { class: "music-speakers__group" }, [
        node("p", { class: "music-speakers__caption" }, [icon("group"), node("span", null, "Group · " + block.line)]),
        node("ul", { class: "rooms", "aria-label": "Group: " + block.name }, block.speakers.map((speaker) => node("li", null, speakerButton(speaker, false))))
      ]));
    });
  }

  function speakerButton(speaker, solo) {
    const playing = solo ? speaker.line : speaker.leader ? "Leads the group" : "";
    return node("button", {
      class: "room music-speaker", type: "button", "data-speaker-id": speaker.id, "data-focus": "speaker:" + speaker.id,
      "aria-current": speaker.current ? "true" : "false", "data-led": speaker.playing ? "ok" : null,
      "data-available": speaker.available ? "true" : "false", "data-locked": speaker.locked ? "" : null,
      "aria-label": speaker.name + ", " + speaker.line + (speaker.locked ? ", locked" : "")
    }, [
      node("span", { class: "room__name" }, [node("span", { class: "room__label" }, speaker.name),
        node("span", { class: "room__playing" }, playing)]),
      icon("lock", "room__lock"),
      node("span", { class: "led", "aria-hidden": "true" })
    ]);
  }

  function drawNow(view) {
    // Its own room's lock disables every control; a member's, those that move the group.
    const shared = Boolean(view.groupLocked);
    $("[data-controls]").disabled = Boolean(view.locked);
    $("[data-now-name]").textContent = view.speaker.name;
    $("[data-place]").textContent = view.speaker.name;
    $("[data-model]").textContent = view.speaker.name.includes(view.speaker.label) ? view.speaker.provider : view.speaker.label;
    $("[data-title]").textContent = view.now;
    $("[data-now]").dataset.playing = view.playing ? "true" : "false";
    $("[data-line]").textContent = view.line || "";
    $("[data-line]").hidden = !view.line;
    $("[data-queue-of]").textContent = view.queueOf ? "Plays " + view.queueOf + "'s queue" : "";
    $("[data-queue-of]").hidden = !view.queueOf;
    drawArt(view);
    drawProgress(view);
    const play = $("[data-play]");
    play.setAttribute("aria-label", view.playLabel);
    play.title = view.playLabel + " (P)";
    play.querySelector("use").setAttribute("href", "#i-" + (view.playing ? "pause" : "play"));
    play.disabled = shared;
    $("[data-previous]").disabled = $("[data-next]").disabled = !view.canSkip || shared;
    const shuffle = $("[data-shuffle]");
    shuffle.setAttribute("aria-pressed", String(view.shuffle.on));
    shuffle.setAttribute("aria-label", view.shuffle.label);
    shuffle.title = view.shuffle.label + " (S)";
    shuffle.dataset.pending = view.shuffle.pending ? "true" : "false";
    shuffle.disabled = !view.shuffle.known || shared;
    const repeat = $("[data-repeat]");
    repeat.setAttribute("aria-pressed", String(view.repeat.value !== "off"));
    repeat.setAttribute("aria-label", view.repeat.label);
    repeat.title = view.repeat.label + " (R)";
    repeat.dataset.pending = view.repeat.pending ? "true" : "false";
    repeat.disabled = !view.repeat.known || shared;
    repeat.querySelector("use").setAttribute("href", "#i-" + (view.repeat.value === "one" ? "repeat-one" : "repeat"));
    drawLevel(volume, $("[data-volume-wrap] .fader"), view.volume, dragging.volume, "[data-volume-step]", false);
    $("[data-volume-text]").textContent = "Volume " + view.volume.text + (view.muted ? " · muted" : "");
    $("[data-group-text]").textContent = view.group ? view.group.text : "Not grouped";
    $("[data-group-open]").disabled = shared;
    $("[data-group-volume-wrap]").hidden = !view.group;
    $("[data-group-volume-text]").hidden = !view.group;
    if (view.group) {
      drawLevel(groupVolume, $("[data-group-volume-wrap] .fader"), view.group.volume, dragging.group, "[data-group-step]", shared);
      $("[data-group-volume-text]").textContent = "Group volume " + view.group.volume.text;
    }
  }

  function drawLevel(input, bar, level, held, steps, locked) {
    input.disabled = level.disabled || locked;
    // A slider being dragged shows the finger, not the speaker.
    if (!held && level.value !== null) input.value = level.value;
    input.style.setProperty("--fill", (held ? Number(input.value) : level.fill) + "%");
    input.setAttribute("aria-valuetext", level.text);
    bar.dataset.pending = level.pending ? "true" : "false";
    document.querySelectorAll(steps).forEach((button) => { button.disabled = level.disabled || locked; });
  }

  // The cover, only through the gateway's art route; a note while there is none.
  function drawArt(view) {
    const image = $("[data-art-image]");
    const url = session.art(view.art, 256);
    if (!stale(image, url)) return;
    if (!url) {
      image.hidden = true;
      image.removeAttribute("src");
      return;
    }
    image.hidden = false;
    image.onerror = () => { image.hidden = true; };
    image.src = url;
  }

  function drawProgress(view) {
    const progress = view.progress;
    const box = $("[data-progress]");
    box.hidden = !progress;
    if (!progress) return;
    box.dataset.pending = progress.pending ? "true" : "false";
    seek.disabled = progress.duration === null || Boolean(view.groupLocked);
    seek.max = String(progress.duration || 1);
    if (dragging.seek) return;
    seek.value = String(progress.elapsed);
    seek.style.setProperty("--fill", progress.fill + "%");
    seek.setAttribute("aria-valuetext", progress.text);
    $("[data-elapsed]").textContent = progress.at;
    $("[data-duration]").textContent = progress.left;
  }

  // ---- the library ----------------------------------------------------------

  function drawLibrary(library) {
    const section = $("[data-library]");
    section.hidden = !library;
    if (!library) return;
    document.querySelectorAll("[data-tab]").forEach((button) => {
      const current = button.dataset.tab === library.tab;
      button.setAttribute("aria-selected", String(current));
      button.tabIndex = current ? 0 : -1;
    });
    $("[data-blind]").textContent = library.blind || "";
    $("[data-blind]").hidden = !library.blind;
    document.querySelectorAll("[data-panel]").forEach((panel) => { panel.hidden = Boolean(library.blind) || panel.dataset.panel !== library.tab; });
    if (library.search) drawSearch(library.search);
    if (library.browse) drawBrowse(library.browse);
    if (library.queue) drawQueue(library.queue);
  }

  function drawSearch(search) {
    if (document.activeElement !== query && query.value !== search.query) query.value = search.query;
    options($("[data-kind]"), search.kinds);
    options($("[data-source]"), search.sources);
    $("[data-search-text]").textContent = search.text;
    const results = $("[data-results]");
    if (!stale(results, search.sections)) return;
    results.textContent = "";
    forget("search:");
    search.sections.forEach((section) => {
      const title = "music-results-" + section.kind;
      results.appendChild(node("section", { class: "music-section", "aria-labelledby": title }, [
        node("div", { class: "music-section__head" }, [
          node("h3", { class: "panel__title", id: title }, section.title),
          section.more ? node("button", { class: "music-section__all", type: "button", "data-all": section.kind,
            "data-focus": "all:" + section.kind }, "All " + section.total) : null
        ]),
        node("ul", { class: "music-items" }, section.items.map((item, index) => itemRow(item, "search:" + section.kind + ":" + index)))
      ]));
    });
  }

  function options(select, list) {
    if (!stale(select, list)) return;
    select.textContent = "";
    list.forEach((option) => select.appendChild(node("option", { value: option.value, selected: option.current ? "" : null }, option.label)));
  }

  function drawBrowse(browse) {
    const back = $("[data-browse-back]");
    back.disabled = browse.depth === 0;
    $("[data-browse-path]").textContent = browse.path.join(" › ");
    $("[data-browse-text]").textContent = browse.text;
    $("#music-panel-browse [data-more]").hidden = !browse.more;
    const list = $("[data-browse-items]");
    if (stale(list, browse.items)) {
      forget("browse:");
      list.replaceChildren(...browse.items.map((item, index) => itemRow(item, "browse:" + index)));
    }
    // A folder opened, or Back: the focus waits on the path while it reads,
    // then goes to the first entry, unless it was taken elsewhere meanwhile
    // (not left on what went, or on what was hidden or disabled).
    if (!browsing) return;
    const path = $("[data-browse-path]"), first = list.querySelector("[data-item]"), at = document.activeElement;
    if (at && at !== document.body && at !== path && !at.disabled && !at.closest("[hidden]")) browsing = false;
    else if (first) {
      browsing = false;
      first.focus();
    } else path.focus();
  }

  function drawQueue(queue) {
    $("[data-queue-owner]").textContent = queue.owner ? "The queue of " + queue.owner + "'s group" : "";
    $("[data-queue-owner]").hidden = !queue.owner;
    $("[data-queue-text]").textContent = queue.text;
    $("#music-panel-queue [data-more]").hidden = !queue.more;
    const list = $("[data-queue-items]");
    if (!stale(list, queue.items)) return;
    forget("queue:");
    list.replaceChildren(...queue.items.map((item, index) => itemRow(item, "queue:" + index)));
  }

  function forget(prefix) {
    Array.from(shown.keys()).forEach((key) => { if (key.startsWith(prefix)) shown.delete(key); });
  }

  // One entry: its cover (or its kind), its name and line, its source and length.
  function itemRow(item, id) {
    shown.set(id, item);
    const thumb = session.art(item.art, 80);
    const image = thumb ? node("img", { src: thumb, alt: "", width: "40", height: "40", loading: "lazy", decoding: "async" }) : null;
    if (image) image.addEventListener("error", () => image.remove());
    const label = [item.name, item.current ? "playing now" : null, item.line, item.source, item.duration].filter(Boolean).join(", ");
    return node("li", null, node("button", {
      class: "music-item", type: "button", "data-item": id, "data-focus": "item:" + item.key,
      "aria-current": item.current ? "true" : null, "aria-label": label,
      "aria-haspopup": item.plays ? "dialog" : null
    }, [
      node("span", { class: "music-item__art", "aria-hidden": "true" }, [icon(KIND_ICON[item.kind] || "note"), image]),
      node("span", { class: "music-item__text" }, [node("span", { class: "music-item__name" }, item.name),
        item.line ? node("span", { class: "music-item__line" }, item.line) : null]),
      node("span", { class: "music-item__side", "aria-hidden": "true" }, [
        item.source ? node("span", { class: "badge badge--quiet" }, item.source) : null,
        item.duration ? node("span", null, item.duration) : null,
        item.opens && !item.plays ? icon("right") : null
      ])
    ]));
  }

  // What an item offers, in a sheet: Open, Play now, Play next, Add to queue.
  function openItem(item) {
    chosenItem = item;
    const view = session.view();
    $("[data-item-name]").textContent = item.name;
    $("[data-item-line]").textContent = [item.line, item.source, item.duration].filter(Boolean).join(" · ");
    $("[data-item-actions]").replaceChildren(...session.actions(item).map((action) => node("button", {
      class: "button", type: "button", "data-action": action.id, disabled: action.disabled ? "" : null
    }, action.label)));
    $("[data-item-target]").textContent = view ? "On " + view.speaker.name + (view.queueOf ? ", in " + view.queueOf + "'s group" : "")
      + ". Play now replaces the queue." : "";
    openSheet("item-sheet");
    const first = $("[data-item-actions] button:not(:disabled)");
    if (first) first.focus();
  }

  // ---- the group ------------------------------------------------------------

  function drawGroupSheet() {
    const grouping = session.grouping();
    if (!grouping) { $("#group-sheet").close(); return; }
    $("[data-group-title]").textContent = "Group with " + grouping.leaderName;
    $("[data-members-title]").hidden = !grouping.grouped;
    const members = $("[data-members]");
    if (stale(members, grouping.members.concat(grouping.grouped))) {
      members.replaceChildren(...(grouping.grouped ? grouping.members : []).map((member) => node("li", { class: "music-member" }, [
        node("span", { class: "music-member__name" }, member.name),
        member.leader ? node("span", { class: "badge badge--quiet" }, "Leads") : null,
        node("button", { class: "button", type: "button", "data-leave": member.id, "data-focus": "leave:" + member.id,
          "aria-label": (member.leader ? "End the group led by " : "Remove ") + member.name }, member.leader ? "End" : "Remove")
      ])));
    }
    // Picked speakers no longer offered are forgotten.
    Array.from(picked).forEach((id) => { if (!grouping.candidates.some((candidate) => candidate.id === id && candidate.available)) picked.delete(id); });
    const candidates = $("[data-candidates]");
    const offered = grouping.candidates.map((candidate) => Object.assign({ picked: picked.has(candidate.id) }, candidate));
    if (stale(candidates, offered)) {
      const focused = document.activeElement && document.activeElement.value;
      candidates.replaceChildren(...offered.map((candidate) => node("li", { class: "music-member", "data-disabled": candidate.available ? null : "" },
        node("label", null, [
          node("input", { type: "checkbox", value: candidate.id, checked: candidate.picked ? "" : null, disabled: candidate.available ? null : "" }),
          node("span", { class: "music-member__name" }, candidate.name),
          candidate.note ? node("span", { class: "music-member__note" }, candidate.note) : null
        ]))));
      const again = focused && candidates.querySelector('input[value="' + CSS.escape(focused) + '"]');
      if (again) again.focus();
    }
    const join = $("[data-join]");
    join.disabled = grouping.pending || !picked.size;
    join.textContent = grouping.pending ? "Joining…" : picked.size ? "Join " + picked.size : "Join";
    const result = $("[data-group-result]");
    result.hidden = !grouping.result;
    if (grouping.result && stale(result, grouping.result)) {
      result.replaceChildren(...[grouping.result.text ? node("p", null, grouping.result.text) : null]
        .concat(grouping.result.refused.map((entry) => node("p", { class: "music-result__refused" }, entry.reason))).filter(Boolean));
    }
    $("[data-group-end]").hidden = !grouping.grouped;
  }

  // ---- TIDAL ----------------------------------------------------------------

  function drawTidal(tidal) {
    const box = $("[data-tidal]");
    box.hidden = !tidal;
    if (!tidal) return;
    const waiting = tidal.state === "waiting";
    $("[data-tidal-text]").textContent = tidal.text;
    $("[data-tidal-code]").textContent = tidal.code || "";
    $("[data-tidal-code]").hidden = !waiting || !tidal.code;
    const link = $("[data-tidal-link]");
    link.hidden = !waiting || !tidal.url;
    if (tidal.url && /^https:\/\/link\.tidal\.com\//.test(tidal.url)) link.href = tidal.url;
    const start = $("[data-tidal-start]");
    start.hidden = waiting;
    start.disabled = tidal.pending;
    start.textContent = tidal.state === "failed" ? "Try again" : tidal.signedIn ? "Sign in again" : "Sign in to TIDAL";
    $("[data-tidal-cancel]").hidden = !(waiting || tidal.state === "failed");
    $("[data-tidal-cancel]").textContent = waiting ? "Cancel" : "Close";
  }

  // ---- shared -------------------------------------------------------------

  function openSheet(id) {
    const dialog = document.getElementById(id);
    if (dialog.open) return;
    if (typeof dialog.showModal === "function") dialog.showModal();
    else dialog.setAttribute("open", "");
    const selected = dialog.querySelector('[aria-current="true"]');
    if (selected) selected.focus();
  }

  function showAlert(text, kind, tone) {
    const alert = $("[data-alert]");
    alert.hidden = !text;
    alert.dataset.tone = tone || "";
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
    toastTimer = setTimeout(() => box.classList.remove("is-visible"), tone === "bad" ? 4000 : 2400);
  }

  function announce(message) {
    const region = $("#announcer");
    region.textContent = "";
    setTimeout(() => { region.textContent = message; }, 60);
  }

  function storage() {
    try {
      const local = window.localStorage;
      local.getItem("music.speaker");
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
