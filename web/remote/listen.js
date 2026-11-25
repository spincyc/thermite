/*
 * Listen on the remote page: draws RemoteListen (listen-session.js) for the
 * room on show, its speakers, what the chosen one plays, its keys, volume,
 * input and group, its settings under Info and Music's library, and hands
 * the sessions its taps. remote.js makes the sessions (RemoteListen.sessions,
 * listen-session.js), draws Listen through create() while a room is in it,
 * and keeps what both
 * modes share: the room lists, the switch, the alert line, the keys
 * (data-button) and their hold-to-repeat, the sheets, toasts and theme.
 */
(function (global) {
  "use strict";

  const KIND_ICON = { track: "i-note", album: "i-album", artist: "i-artist", playlist: "i-playlist", folder: "i-folder" };
  // A cover that failed is asked for once more this long after, and up to
  // ART_JITTER_MS more, so a page of them does not come back at once.
  const ART_RETRY_MS = 2000;
  const ART_JITTER_MS = 1000;
  // A setting with this many options or fewer, each short, is a row of
  // buttons; a longer one is a list.
  const SEGMENTS = 4;
  const SEGMENT_LABEL = 12;
  // What a member's locked room holds: the keys that move the whole group.
  const MOVES_GROUP = { shuffle: true, previous: true, play_pause: true, next: true, repeat: true };
  const SHORTCUT = { shuffle: "S", previous: "<", play_pause: "P", next: ">", repeat: "R" };
  // From here the library sits beside the remote, in the right panel
  // (listen.css), which a phone on its side 500 px tall or less never shows (shell.css).
  const WIDE = "(min-width: 1200px) and (min-height: 501px)";

  // options: {session, sessions (RemoteListen.sessions), ui: remote.js's $, all, node, icon,
  // openSheet, flash}. update() draws Listen, or hides it in Watch.
  function create(options) {
    const session = options.session;
    const listen = options.sessions.listen, audio = options.sessions.audio, music = options.sessions.music;
    const ui = options.ui;
    const $ = ui.$, all = ui.all, node = ui.node, icon = ui.icon;
    const query = $("[data-query]"), seek = $("[data-seek]"), level = $("[data-listen-level]"), groupLevel = $("[data-group-level]");
    const wide = global.matchMedia ? global.matchMedia(WIDE) : null;
    const dragging = { volume: false, group: false, seek: false };
    // Each block's last drawing, by what it showed: a block is built again only
    // when that changes, so a report elsewhere keeps an open list, a tap under
    // way and a screen reader's place.
    const drawn = new WeakMap();
    // The entries the library lists, by their buttons' data-item, and the one a sheet acts on.
    const shown = new Map();
    let chosenItem = null, itemOpener = null;
    // The page shown: its own entry (its header's buttons play it) and its artists.
    let pageHead = null;
    // Whether a page moved (one opened, or Back) and its focus is to follow,
    // and the tab it moved on.
    let browsing = false, browsingTab = null;
    // What opened each of a tab's pages (by data-focus, at the depth it
    // opened from), and where Back is taking the focus: back to it.
    const openedFrom = {};
    let returnTo = null;
    // The speakers ticked in the group sheet, kept across its redraws.
    const picked = new Set();
    // Each cover image's address already asked for twice.
    const retried = new WeakMap();
    let listening = false;

    cover($("[data-art-image]"));
    bind();
    placeLibrary();
    if (wide && wide.addEventListener) wide.addEventListener("change", placeLibrary);
    // While it plays, the time gone moves on each second.
    const tick = global.setInterval(() => {
      const view = listening && document.visibilityState !== "hidden" && listen.view();
      if (view && view.playing && view.progress) drawProgress(view);
    }, 1000);

    function stop() {
      global.clearInterval(tick);
      if (wide && wide.removeEventListener) wide.removeEventListener("change", placeLibrary);
    }

    // ---- drawing ------------------------------------------------------------

    function update() {
      listening = session.mode() === "listen";
      $("#listen").hidden = !listening;
      $("[data-listen-info]").hidden = !listening;
      $("#info-sheet [data-info]").hidden = listening;
      if (!listening) {
        $("[data-speakers-wrap]").hidden = true;
        ["library-sheet", "item-sheet", "group-sheet"].forEach((id) => { if ($("#" + id).open) $("#" + id).close(); });
        return;
      }
      const at = document.activeElement;
      const focused = at && at.dataset ? at.dataset.focus : null;
      const view = listen.view();
      drawSpeakers(view);
      $("[data-listen-reading]").hidden = Boolean(view);
      $("[data-listen-face]").hidden = !view;
      if (view) {
        drawNow(view);
        drawTransport(view);
        drawChips(view);
        drawVolume(view);
      }
      drawInfo(view);
      drawLibrary(view);
      drawTidal(music && view && view.library ? music.tidal() : null);
      if ($("#group-sheet").open) drawGroupSheet();
      if ($("#input-sheet").open && $("[data-input-list]").hasAttribute("data-listen")) fillInputs(view);
      // A redrawn control keeps the focus it had; a step at its end, its group's other.
      if (focused && (!document.activeElement || document.activeElement === document.body)) {
        const again = all('[data-focus="' + CSS.escape(focused) + '"]').find((each) => !each.closest("[hidden]"));
        const target = again && again.disabled ? again.parentNode.querySelector("button:not(:disabled)") : again;
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

    // The room's speakers as bubbles, when it has more than one: lit while
    // one plays, the chosen one marked, its padlock while its room is locked.
    function drawSpeakers(view) {
      const speakers = listen.speakers();
      const box = $("[data-speakers]");
      box.parentNode.hidden = speakers.length < 2;
      const fresh = stale(box, speakers.map((speaker) => [speaker.id, speaker.name, speaker.label]));
      if (fresh) {
        box.replaceChildren(...speakers.map((speaker) => node("button", {
          type: "button", class: "target", "data-speaker": speaker.id, "data-focus": "speaker:" + speaker.id,
          "aria-keyshortcuts": speaker.digit || null, title: speaker.label
        }, [node("span", { class: "led", "aria-hidden": "true" }), icon("i-lock", "target__lock"), node("span", { class: "target__label" }, speaker.name)])));
      }
      let currentTab = null;
      all("[data-speakers] .target").forEach((bubble) => {
        const speaker = speakers.find((entry) => entry.id === bubble.dataset.speaker);
        if (!speaker) return;
        const locked = speaker.current && Boolean(view && view.locked);
        bubble.setAttribute("data-state", speaker.playing ? "current" : "idle");
        bubble.toggleAttribute("data-locked", locked);
        if (speaker.current) { bubble.setAttribute("aria-current", "true"); currentTab = bubble; }
        else bubble.removeAttribute("aria-current");
        bubble.setAttribute("aria-label", speaker.name + (speaker.playing ? ", playing" : "") + (locked ? ". " + view.locked.text : ""));
      });
      const currentId = currentTab ? currentTab.dataset.speaker : null;
      if (ui.reveal && (fresh || box.lit !== currentId)) {
        box.lit = currentId;
        ui.reveal(box, currentTab, fresh);
      }
      if (ui.markOverflow) ui.markOverflow(box);
    }

    function drawNow(view) {
      $("[data-listen-now]").dataset.playing = view.playing ? "true" : "false";
      $("[data-listen-title]").textContent = view.now;
      text($("[data-listen-line]"), view.line);
      text($("[data-listen-queue]"), view.queueOf ? "Plays " + view.queueOf + "'s queue" : "");
      const badges = $("[data-listen-badges]");
      if (stale(badges, view.badges)) {
        badges.replaceChildren(...view.badges.map((badge) => node("span", { class: "badge badge--quiet" }, badge)));
        badges.hidden = !view.badges.length;
      }
      drawArt(view);
      drawProgress(view);
    }

    function text(element, value) {
      element.textContent = value || "";
      element.hidden = !value;
    }

    // The cover, only through the gateway's art route; a note while there is none.
    function drawArt(view) {
      const image = $("[data-art-image]");
      const url = music && view.art ? music.art(view.art, 256) : null;
      if (!stale(image, url)) return;
      if (!url) {
        image.hidden = true;
        image.removeAttribute("src");
        return;
      }
      image.hidden = false;
      image.src = url;
    }

    // A cover from the gateway's art route, invisible (its placeholder shows
    // through) while it fails. The gateway fetches only so many at once and
    // answers the rest 503, which a browser never asks again: a cover that
    // fails is asked for once more ART_RETRY_MS later, once for each address
    // it has. Laid out still, and asked for eagerly: a lazy image the browser
    // takes for out of view is never asked for.
    function cover(image) {
      image.addEventListener("load", () => { delete image.dataset.failed; });
      image.addEventListener("error", () => {
        const src = image.getAttribute("src");
        image.dataset.failed = "";
        if (!src || retried.get(image) === src) return;
        retried.set(image, src);
        global.setTimeout(() => {
          if (image.getAttribute("src") !== src) return;
          image.loading = "eager";
          image.setAttribute("src", src);
        }, ART_RETRY_MS + Math.random() * ART_JITTER_MS);
      });
      return image;
    }

    function drawProgress(view) {
      const progress = view.progress;
      const box = $("[data-progress]");
      box.hidden = !progress;
      if (!progress) return;
      box.dataset.pending = progress.pending ? "true" : "false";
      seek.disabled = progress.duration === null || Boolean(view.locked || view.groupLocked);
      seek.max = String(progress.duration || 1);
      if (dragging.seek) return;
      seek.value = String(progress.elapsed);
      seek.style.setProperty("--fill", progress.fill + "%");
      seek.setAttribute("aria-valuetext", progress.text);
      $("[data-elapsed]").textContent = progress.at;
      $("[data-duration]").textContent = progress.left;
    }

    // Shuffle, previous, play, next and repeat (or the device's own keys),
    // each a key remote.js presses (data-button); a lock disables what it holds.
    function drawTransport(view) {
      const box = $("[data-listen-transport]");
      if (stale(box, view.transport.map((entry) => entry.id))) {
        box.replaceChildren(...view.transport.map((entry) => node("button", {
          type: "button", class: "key " + (entry.play ? "key--play" : "key--sm") + (entry.toggle ? " key--toggle" : ""),
          "data-button": entry.id, "data-focus": "key:" + entry.id, "aria-keyshortcuts": SHORTCUT[entry.id] || null
        }, icon("i-" + entry.icon))));
      }
      view.transport.forEach((entry) => {
        const key = box.querySelector('[data-button="' + entry.id + '"]');
        const name = entry.id === "repeat" && entry.mode === "one" ? "repeat-one" : entry.icon;
        key.setAttribute("aria-label", entry.label);
        key.title = entry.label + (SHORTCUT[entry.id] ? " (" + SHORTCUT[entry.id] + ")" : "");
        key.querySelector("use").setAttribute("href", "#i-" + name);
        if (entry.toggle) key.setAttribute("aria-pressed", String(Boolean(entry.on)));
        if (entry.pending) key.dataset.pending = "true"; else delete key.dataset.pending;
        key.disabled = Boolean(entry.disabled || view.locked || view.groupLocked && MOVES_GROUP[entry.id]);
      });
    }

    // Group, Input and Library, as the speaker has them.
    function drawChips(view) {
      const group = $('[data-action="listen-group"]'), input = $('[data-action="listen-input"]'), library = $('[data-action="listen-library"]');
      group.hidden = !view.canGroup;
      $("[data-group-text]").textContent = view.group ? view.group.text : "Group";
      group.setAttribute("aria-label", view.group ? "Group: " + view.group.text : "Group");
      group.disabled = Boolean(view.locked);
      input.hidden = !view.input;
      if (view.input) {
        $("[data-input-text]").textContent = view.input.label;
        input.setAttribute("aria-label", "Input: " + view.input.label);
        if (view.input.pending) input.dataset.pending = "true"; else delete input.dataset.pending;
      }
      input.disabled = Boolean(view.locked);
      library.hidden = !view.library;
      $("[data-listen-chips]").hidden = group.hidden && input.hidden && library.hidden;
    }

    // The speaker's volume on its own scale, Mute, and a group's volume under it.
    function drawVolume(view) {
      const volume = view.volume, locked = Boolean(view.locked);
      const fader = $("[data-listen-volume] .fader");
      fader.hidden = !volume;
      if (volume) slider(level, fader, volume, dragging.volume, locked);
      const caption = $("[data-listen-caption]");
      const words = [view.speaker.name, volume ? volume.text : null, view.muted ? "muted" : null].filter(Boolean).join(" · ");
      // Without the bubbles to carry it, a locked speaker's padlock is beside its name.
      const lock = locked && $("[data-speakers-wrap]").hidden;
      if (stale(caption, [words, lock])) caption.replaceChildren(...[lock ? icon("i-lock", "caption__lock") : null, words].filter(Boolean));
      const mute = $("[data-listen-volume] [data-button=mute]");
      mute.hidden = view.muted === null;
      mute.setAttribute("aria-pressed", String(Boolean(view.muted)));
      mute.disabled = locked;
      const group = view.group;
      $("[data-group-volume-wrap]").hidden = !group;
      if (!group) return;
      slider(groupLevel, $("[data-group-volume-wrap] .fader"), group.volume, dragging.group, locked || Boolean(view.groupLocked));
      $("[data-group-caption]").textContent = "Group · " + group.volume.text;
    }

    function slider(input, fader, volume, held, locked) {
      input.min = String(volume.min);
      input.max = String(volume.max);
      input.step = String(volume.step || 1);
      input.disabled = volume.disabled || locked;
      // A slider being dragged shows the finger, not the speaker.
      if (!held && volume.value !== null) input.value = String(volume.value);
      const fill = held && volume.max > volume.min ? (Number(input.value) - volume.min) / (volume.max - volume.min) * 100 : volume.fill;
      input.style.setProperty("--fill", fill + "%");
      input.setAttribute("aria-valuetext", volume.text);
      fader.dataset.pending = volume.pending ? "true" : "false";
      fader.querySelectorAll(".fader__step").forEach((step) => { step.disabled = volume.disabled || locked; });
    }

    // ---- Info: the speaker's power, settings and facts ------------------------

    function drawInfo(view) {
      const box = $("[data-listen-info]");
      const status = music ? music.status() : null;
      const blocks = !view ? [["reading", null, () => node("p", { class: "info__note" }, "Reading…")]] : [
        ["speaker", [view.speaker, view.power, Boolean(view.locked)], speakerBlock],
        ...view.sections.map((section) => ["section:" + section.id, [section, Boolean(view.locked)], sectionBlock]),
        ["facts", view.facts, factsBlock]
      ];
      if (status && status.state !== "ready" && status.text) blocks.push(["music", status.text, (words) => infoSection("Music", node("p", { class: "info__note" }, words))]);
      place(box, blocks.filter(([, value]) => !Array.isArray(value) || value.length));
    }

    // Each block of box ([key, value, build(value)]) drawn again only when its
    // value changed, in place, unless which blocks show changed.
    function place(box, blocks) {
      const children = Array.from(box.children);
      const same = children.length === blocks.length && blocks.every(([key], index) => children[index].dataset.block === key);
      const made = blocks.map(([key, value, build], index) => {
        const old = same ? children[index] : children.find((child) => child.dataset.block === key);
        if (old && drawn.get(old) === JSON.stringify(value)) return old;
        const element = build(value);
        element.dataset.block = key;
        drawn.set(element, JSON.stringify(value));
        if (same) old.replaceWith(element);
        return element;
      });
      if (!same) box.replaceChildren(...made);
    }

    function infoSection(title, body) {
      return node("section", { class: "info__section" }, [node("h3", { class: "info__title" }, title)].concat(body));
    }

    // The speaker, what it is and where, and its power.
    function speakerBlock([speaker, power, locked]) {
      return node("section", { class: "info__section" }, [
        node("p", { class: "listen-info__speaker" }, [speaker.label,
          node("small", { class: "device__meta" }, [speaker.model, speaker.place].filter(Boolean).join(" · "))]),
        power === null ? null : node("button", { type: "button", class: "switch switch--setting", role: "switch", "aria-checked": String(power),
          "data-power": "", "data-focus": "power", disabled: locked ? "" : null }, [
          node("span", { class: "switch__text" }, node("span", { class: "switch__label" }, "Power")),
          node("span", { class: "switch__track", "aria-hidden": "true" })
        ])
      ]);
    }

    function sectionBlock([section, locked]) {
      return infoSection(section.title, node("div", { class: "settings" }, section.settings.map((setting) => settingRow(setting, locked))));
    }

    function settingRow(setting, locked) {
      const id = "listen-setting-" + setting.item, off = locked ? "" : null;
      if (setting.kind === "switch") {
        return node("button", { type: "button", class: "switch switch--setting", role: "switch", "aria-checked": String(setting.on),
          "data-setting": setting.item, "data-focus": setting.item, "data-pending": setting.pending ? "true" : null, disabled: off }, [
          node("span", { class: "switch__text" }, node("span", { class: "switch__label" }, setting.label)),
          node("span", { class: "switch__track", "aria-hidden": "true" })
        ]);
      }
      const row = node("div", { class: "setting setting--" + setting.kind, "data-pending": setting.pending ? "true" : null });
      if (setting.kind === "number") {
        row.append(node("span", { class: "setting__label", id: id }, setting.label),
          node("div", { class: "stepper", role: "group", "aria-labelledby": id }, [
            node("button", { class: "stepper__step", type: "button", "data-setting": setting.item, "data-step": "-1",
              "data-focus": setting.item + ":-1", "aria-label": setting.label + " down", disabled: setting.atMin || locked ? "" : null }, icon("i-minus")),
            node("output", { class: "stepper__value", "aria-live": "off" }, setting.text),
            node("button", { class: "stepper__step", type: "button", "data-setting": setting.item, "data-step": "1",
              "data-focus": setting.item + ":1", "aria-label": setting.label + " up", disabled: setting.atMax || locked ? "" : null }, icon("i-plus"))
          ]));
      } else if (setting.options.length <= SEGMENTS && setting.options.every((option) => option.label.length <= SEGMENT_LABEL)) {
        row.classList.add("setting--wide");
        row.append(node("span", { class: "setting__label", id: id }, setting.label),
          node("div", { class: "segmented", role: "radiogroup", "aria-labelledby": id }, setting.options.map((option) => node("button", {
            class: "segmented__option", type: "button", role: "radio", "data-setting": setting.item, "data-value": option.value,
            "data-focus": setting.item + ":" + option.value, "aria-checked": String(option.current), disabled: off
          }, option.label))));
      } else {
        const select = node("select", { class: "setting__select", "data-setting": setting.item, "data-focus": setting.item, "aria-labelledby": id, disabled: off });
        // A value it only reads (a mode the player reports but takes no command for) shows, not offered.
        if (!setting.options.some((option) => option.current)) {
          select.appendChild(node("option", { value: setting.value, disabled: "", selected: "" }, setting.text));
        }
        setting.options.forEach((option) => select.appendChild(node("option", { value: option.value, selected: option.current ? "" : null }, option.label)));
        row.append(node("label", { class: "setting__label", id: id }, setting.label), select);
      }
      return row;
    }

    function factsBlock(facts) {
      return infoSection("About this speaker", node("dl", { class: "map" }, facts.flatMap((fact) => [node("dt", null, fact.label), node("dd", null, fact.text)])));
    }

    // ---- the input list -------------------------------------------------------

    // Remotes' input sheet, listing the speaker's inputs.
    function openInputs() {
      const view = listen.view();
      if (!view || !view.input) return;
      $("#input-sheet-title").textContent = "Input";
      // Watch fills the same list (remote.js): drawn afresh, whatever this last drew there.
      drawn.delete($("[data-input-list]"));
      fillInputs(view);
      ui.openSheet("input-sheet");
    }

    function fillInputs(view) {
      const list = $("[data-input-list]");
      list.setAttribute("data-listen", "");
      if (!view || !view.input || !stale(list, view.input.options)) return;
      list.replaceChildren(...view.input.options.map((option) => node("li", null, node("button", {
        type: "button", class: "room", "data-listen-input": option.value,
        "aria-current": option.current ? "true" : "false", "data-led": option.current ? "ok" : "off"
      }, [
        node("span", { class: "room__label" }, option.label),
        node("span", { class: "room__source" }, option.current ? [
          node("span", { class: "led", "aria-hidden": "true" }),
          node("span", { class: "room__source-text" }, "In use")
        ] : [])
      ]))));
    }

    // ---- the library ----------------------------------------------------------

    // Beside the remote from WIDE, else in its sheet.
    function placeLibrary() {
      const library = $("[data-library]");
      const home = wide && wide.matches ? $("[data-library-wide]") : $("[data-library-narrow]");
      if (library.parentNode === home) return;
      if ($("#library-sheet").open) $("#library-sheet").close();
      home.appendChild(library);
    }

    // The library on a tab: in its sheet, or where it already shows beside the remote.
    function openLibrary(tab) {
      const view = listen.view();
      if (!music || !view || !view.library) return false;
      if (tab) music.setTab(tab);
      update();
      if (!(wide && wide.matches)) ui.openSheet("library-sheet");
      const to = tab === "search" ? query : $('[data-tab][aria-selected="true"]');
      if (to) to.focus();
      return true;
    }

    // Why there is no library to show, or nothing.
    function libraryNote(view) {
      if (!music) return "";
      const status = music.status();
      if (status.state !== "ready") return status.text;
      return view && !view.library ? "Music Assistant does not play " + view.speaker.name + "." : "";
    }

    function drawLibrary(view) {
      const library = music && view && view.library ? music.library() : null;
      text($("[data-library-note]"), library ? "" : libraryNote(view));
      $("[data-tabs]").hidden = !library;
      if (!library) {
        $("[data-blind]").hidden = true;
        all("[data-panel]").forEach((panel) => { panel.hidden = true; });
        $("[data-page]").hidden = true;
        if ($("#library-sheet").open) $("#library-sheet").close();
        return;
      }
      all("[data-tab]").forEach((button) => {
        const current = button.dataset.tab === library.tab;
        button.setAttribute("aria-selected", String(current));
        button.tabIndex = current ? 0 : -1;
      });
      // Another tab: a page still reading no longer takes the focus.
      if (library.tab !== browsingTab) browsing = false;
      browsingTab = library.tab;
      text($("[data-blind]"), library.blind);
      all("[data-panel]").forEach((panel) => {
        panel.hidden = Boolean(library.blind) || panel.dataset.panel !== library.tab || Boolean(library.page);
      });
      const pagePanel = $("[data-page]");
      pagePanel.hidden = Boolean(library.blind) || !library.page;
      pagePanel.setAttribute("aria-labelledby", "library-tab-" + library.tab);
      if (library.search) drawSearch(library.search);
      if (library.page) drawPage(library.page);
      if (library.queue) drawQueue(library.queue);
      // Back to the tab's own list: to the row that opened the page, else the tab.
      if (browsing && !library.page) {
        browsing = false;
        const row = returnTo && all('[data-panel="' + library.tab + '"] [data-focus]').find((each) => each.dataset.focus === returnTo);
        returnTo = null;
        (row || $('[data-tab][aria-selected="true"]')).focus({ preventScroll: true });
      }
    }

    // A page opened: what opened it, for Back.
    function opening(from) {
      const library = music.library();
      if (library) {
        const depth = library.page ? library.page.depth : 0;
        const opener = (openedFrom[library.tab] || []).slice(0, depth);
        opener[depth] = (from && from.dataset.focus) || null;
        openedFrom[library.tab] = opener;
      }
      returnTo = null;
      browsing = true;
    }

    // Back a page, and the focus back to what opened the one left.
    function goBack() {
      const library = music.library();
      const depth = library && library.page ? library.page.depth : 0;
      if (!depth) return;
      const opener = openedFrom[library.tab] || [];
      returnTo = opener[depth - 1] || null;
      openedFrom[library.tab] = opener.slice(0, depth - 1);
      browsing = true;
      music.back();
    }

    function drawSearch(search) {
      if (document.activeElement !== query && query.value !== search.query) query.value = search.query;
      fill($("[data-kind]"), search.kinds);
      fill($("[data-source]"), search.sources);
      $("[data-search-text]").textContent = search.text;
      const results = $("[data-results]");
      if (!stale(results, search.sections)) return;
      forget("search:");
      results.replaceChildren(...search.sections.map((section) => {
        const title = "library-results-" + section.kind;
        return node("section", { class: "library-section", "aria-labelledby": title }, [
          node("div", { class: "library-section__head" }, [
            node("h3", { class: "panel__title", id: title }, section.title),
            section.more ? node("button", { class: "library-section__all", type: "button", "data-all": section.kind,
              "data-focus": "all:" + section.kind }, "All " + section.total) : null
          ]),
          node("ul", { class: "library-items" }, section.items.map((item, index) => itemRow(item, "search:" + section.kind + ":" + index)))
        ]);
      }));
    }

    function fill(select, list) {
      if (!stale(select, list)) return;
      select.replaceChildren(...list.map((option) => node("option", { value: option.value, selected: option.current ? "" : null }, option.label)));
    }

    // A page: Back to what it opened from (Browse's roots have none) and its
    // title, an album's, artist's or playlist's header, an artist's
    // sections, and its items.
    function drawPage(page) {
      $("[data-page-bar]").hidden = !page.back;
      $("[data-page-back-label]").textContent = page.back || "";
      $("[data-page-back]").setAttribute("aria-label", "Back to " + (page.back || ""));
      text($("[data-page-title]"), page.header ? "" : page.name);
      $("[data-page-head]").hidden = !page.header;
      pageHead = page.header;
      if (page.header) drawHead(page.header);
      $("[data-page-text]").textContent = page.text;
      $("[data-page] [data-more]").hidden = !page.more;
      const sections = $("[data-page-sections]");
      if (stale(sections, page.sections)) {
        forget("section:");
        sections.replaceChildren(...page.sections.map((section) => {
          const title = "library-page-" + section.id;
          return node("section", { class: "library-section", "aria-labelledby": title }, [
            node("div", { class: "library-section__head" }, [
              node("h3", { class: "panel__title", id: title }, section.title),
              section.more ? node("button", { class: "library-section__all", type: "button", "data-section": section.id,
                "data-focus": "section:" + section.id }, "All " + section.total) : null
            ]),
            section.text ? node("p", { class: "listen__note" }, section.text) : null,
            node("ul", { class: "library-items" }, section.items.map((item, index) => itemRow(item, "section:" + section.id + ":" + index)))
          ]);
        }));
      }
      const list = $("[data-page-items]");
      if (stale(list, [page.numbered, page.items])) {
        forget("page:");
        list.replaceChildren(...page.items.map((item, index) => itemRow(item, "page:" + index, page.numbered)));
      }
      // A page opened, or Back: the focus waits on its title while it
      // reads, then goes to its header, or else to its first entry (with
      // none, it stays on the title), and the page lets it be, unless it
      // was taken elsewhere meanwhile (not left on what went, or on what was
      // hidden or disabled). It never scrolls the page there: a tap that
      // moved no focus (iOS) left the reader where they are.
      if (!browsing) return;
      const title = page.header ? $("[data-head-name]") : page.back ? $("[data-page-title]") : null;
      const first = $("[data-page]").querySelector("[data-item]"), at = document.activeElement;
      const read = page.state === "done" || page.state === "failed";
      // Back: to what opened the page left, once it is drawn.
      const opener = returnTo && all("[data-page] [data-focus]").find((each) => each.dataset.focus === returnTo && !each.closest("[hidden]"));
      if (opener || read) returnTo = null;
      if (opener) {
        browsing = false;
        opener.focus({ preventScroll: true });
        return;
      }
      if (at && at !== document.body && at !== title && !at.disabled && !at.closest("[hidden]")) browsing = false;
      else {
        browsing = !read;
        const to = page.header ? title : read && first || title;
        if (to) to.focus({ preventScroll: true });
      }
    }

    // An album's, artist's or playlist's header: its cover, name, artists
    // (each opens), what it is, and Play, Shuffle and the rest in a sheet;
    // a locked room's speaker plays nothing from it.
    function drawHead(head) {
      const art = $("[data-head-art]");
      art.dataset.kind = head.kind;
      if (stale(art, [head.kind, head.art])) {
        const url = music.art(head.art, 256);
        art.replaceChildren(...[icon(KIND_ICON[head.kind] || "i-note"),
          url ? cover(node("img", { src: url, alt: "", width: "88", height: "88", decoding: "async" })) : null].filter(Boolean));
      }
      $("[data-head-name]").textContent = head.name;
      const by = $("[data-head-by]");
      by.hidden = !head.artists.length;
      if (stale(by, head.artists)) {
        by.replaceChildren(...head.artists.flatMap((artist, index) => [index ? ", " : null,
          artist.ref ? node("button", { class: "library-head__link", type: "button", "data-head-artist": String(index),
            "data-focus": "head-artist:" + index }, artist.name)
            : artist.name]).filter(Boolean).map((part) => (typeof part === "string" ? document.createTextNode(part) : part)));
      }
      text($("[data-head-line]"), [head.line, head.source].filter(Boolean).join(" · "));
      const view = listen.view();
      const held = Boolean(view && (view.locked || view.groupLocked));
      const plays = !held && music.actions(head.item).some((action) => action.id === "next" && !action.disabled);
      all("[data-head-action]").forEach((button) => {
        button.disabled = !plays;
        button.setAttribute("aria-label", (button.dataset.headAction === "shuffle" ? "Shuffle " : "Play ") + head.name);
      });
      $("[data-head-more]").setAttribute("aria-label", "More for " + head.name);
    }

    function drawQueue(queue) {
      text($("[data-queue-owner]"), queue.owner ? "The queue of " + queue.owner + "'s group" : "");
      $("[data-queue-text]").textContent = queue.text;
      $("#library-panel-queue [data-more]").hidden = !queue.more;
      const list = $("[data-queue-items]");
      if (!stale(list, queue.items)) return;
      forget("queue:");
      list.replaceChildren(...queue.items.map((item, index) => itemRow(item, "queue:" + index)));
    }

    function forget(prefix) {
      Array.from(shown.keys()).forEach((key) => { if (key.startsWith(prefix)) shown.delete(key); });
    }

    // One entry: its cover (or its kind), or its number on an album, its name
    // and line, its source and length.
    function itemRow(item, id, numbered) {
      shown.set(id, item);
      const thumb = numbered ? null : music.art(item.art, 80);
      const image = thumb ? cover(node("img", { src: thumb, alt: "", width: "40", height: "40", loading: "lazy", decoding: "async" })) : null;
      const label = [item.name, item.current ? "playing now" : null, item.line, item.source, item.duration].filter(Boolean).join(", ");
      return node("li", null, node("button", {
        class: "library-item", type: "button", "data-item": id, "data-focus": "item:" + item.key,
        "aria-current": item.current ? "true" : null, "aria-label": label, "aria-haspopup": item.opens ? null : item.plays ? "dialog" : null
      }, [
        numbered ? node("span", { class: "library-item__number", "aria-hidden": "true" }, item.number ? String(item.number) : "")
          : node("span", { class: "library-item__art", "aria-hidden": "true" }, [icon(KIND_ICON[item.kind] || "i-note"), image]),
        node("span", { class: "library-item__text" }, [node("span", { class: "library-item__name" }, item.name),
          item.line ? node("span", { class: "library-item__line" }, item.line) : null]),
        node("span", { class: "library-item__side", "aria-hidden": "true" }, [
          item.source ? node("span", { class: "badge badge--quiet" }, item.source) : null,
          item.duration ? node("span", null, item.duration) : null,
          item.opens && !item.plays ? icon("i-right") : null
        ])
      ]));
    }

    // What an item offers, in a sheet: Play from here, Play now, Play next,
    // Add to queue, Open, Go to its album or artists. from: what was pressed
    // (Safari focuses no button a tap presses).
    function openItem(item, from) {
      chosenItem = item;
      itemOpener = from || null;
      const view = listen.view();
      const held = Boolean(view && (view.locked || view.groupLocked));
      $("[data-item-name]").textContent = item.name;
      $("[data-item-line]").textContent = [item.line, item.source, item.duration].filter(Boolean).join(" · ");
      $("[data-item-actions]").replaceChildren(...music.actions(item).map((action) => node("button", {
        class: "button", type: "button", "data-item-action": action.id,
        disabled: action.disabled || held && action.id !== "open" && !action.id.startsWith("go:") ? "" : null
      }, action.label)));
      $("[data-item-target]").textContent = view ? "On " + view.speaker.name + (view.queueOf ? ", in " + view.queueOf + "'s group" : "")
        + ". Play now replaces the queue." : "";
      ui.openSheet("item-sheet");
      const first = $("[data-item-actions] button:not(:disabled)");
      if (first) first.focus();
    }

    // ---- the group ------------------------------------------------------------

    function openGroup() {
      const view = listen.view();
      if (!view || !view.canGroup) return false;
      picked.clear();
      ui.openSheet("group-sheet");
      drawGroupSheet();
      return true;
    }

    // Who plays together, who may join, and what the last join did. A lock
    // on a speaker the group moves holds every change.
    function drawGroupSheet() {
      const grouping = music && music.grouping();
      const view = listen.view();
      if (!grouping || !view || !view.canGroup) { $("#group-sheet").close(); return; }
      const held = Boolean(view.locked || view.groupLocked);
      $("[data-group-title]").textContent = "Group with " + grouping.leaderName;
      $("[data-members-title]").hidden = !grouping.grouped;
      const members = $("[data-members]");
      if (stale(members, [grouping.members, grouping.grouped, held])) {
        members.replaceChildren(...(grouping.grouped ? grouping.members : []).map((member) => node("li", { class: "listen-member" }, [
          node("span", { class: "listen-member__name" }, member.name),
          member.leader ? node("span", { class: "badge badge--quiet" }, "Leads") : null,
          node("button", { class: "button", type: "button", "data-leave": member.id, "data-focus": "leave:" + member.id, disabled: held ? "" : null,
            "aria-label": (member.leader ? "End the group led by " : "Remove ") + member.name }, member.leader ? "End" : "Remove")
        ])));
      }
      // Picked speakers no longer offered are forgotten.
      Array.from(picked).forEach((id) => { if (!grouping.candidates.some((candidate) => candidate.id === id && candidate.available)) picked.delete(id); });
      const candidates = $("[data-candidates]");
      const offered = grouping.candidates.map((candidate) => Object.assign({ picked: picked.has(candidate.id), held: held }, candidate));
      if (stale(candidates, offered)) {
        const focused = document.activeElement && document.activeElement.value;
        candidates.replaceChildren(...offered.map((candidate) => node("li", { class: "listen-member", "data-disabled": candidate.available ? null : "" },
          node("label", null, [
            node("input", { type: "checkbox", value: candidate.id, checked: candidate.picked ? "" : null, disabled: candidate.available && !held ? null : "" }),
            node("span", { class: "listen-member__name" }, candidate.name),
            candidate.note ? node("span", { class: "listen-member__note" }, candidate.note) : null
          ]))));
        const again = focused && candidates.querySelector('input[value="' + CSS.escape(focused) + '"]');
        if (again) again.focus();
      }
      const join = $("[data-join]");
      join.disabled = held || grouping.pending || !picked.size;
      join.textContent = grouping.pending ? "Joining…" : picked.size ? "Join " + picked.size : "Join";
      const result = $("[data-group-result]");
      result.hidden = !grouping.result;
      if (grouping.result && stale(result, grouping.result)) {
        result.replaceChildren(...[grouping.result.text, grouping.result.waiting].filter(Boolean).map((line) => node("p", null, line))
          .concat(grouping.result.refused.map((entry) => node("p", { class: "listen-result__refused" }, entry.reason))));
      }
      const end = $("[data-group-end]");
      end.hidden = !grouping.grouped;
      end.disabled = held;
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

    // ---- input ----------------------------------------------------------------

    function bind() {
      const on = (selector, type, handler) => all(selector).forEach((element) => element.addEventListener(type, handler));
      on("[data-speakers]", "click", (event) => {
        const bubble = event.target.closest("[data-speaker]");
        if (bubble) listen.choose(bubble.dataset.speaker);
      });
      on('[data-action="listen-group"]', "click", openGroup);
      on('[data-action="listen-input"]', "click", openInputs);
      on('[data-action="listen-library"]', "click", () => openLibrary(null));
      on("[data-input-list]", "click", (event) => {
        const button = event.target.closest("[data-listen-input]");
        if (!button) return;
        audio.chooseInput(button.dataset.listenInput);
        button.closest("dialog").close();
      });
      slide(level, "volume", (value) => listen.setVolume(value));
      slide(groupLevel, "group", (value, final) => music && music.setGroupVolume(value, final));
      // A position is sent once let go; while dragged, only the time shown moves.
      seek.addEventListener("input", () => {
        dragging.seek = true;
        $("[data-elapsed]").textContent = global.RemoteMusic.clock(seek.value);
        seek.style.setProperty("--fill", (Number(seek.max) ? Number(seek.value) / Number(seek.max) * 100 : 0) + "%");
      });
      seek.addEventListener("change", () => { dragging.seek = false; listen.seek(Number(seek.value)); });
      seek.addEventListener("pointerup", () => { dragging.seek = false; });
      seek.addEventListener("blur", () => { dragging.seek = false; });

      // Info: power and the settings.
      const info = $("[data-listen-info]");
      info.addEventListener("click", (event) => {
        if (event.target.closest("[data-power]")) { audio.togglePower(); return; }
        const button = event.target.closest("button[data-setting]");
        if (!button) return;
        if (button.dataset.step) audio.step(button.dataset.setting, Number(button.dataset.step));
        else if (button.dataset.value) audio.set(button.dataset.setting, button.dataset.value);
        else audio.toggle(button.dataset.setting);
      });
      info.addEventListener("change", (event) => {
        if (event.target.matches("select[data-setting]")) audio.set(event.target.dataset.setting, event.target.value);
      });
      // The arrows move along a row of choices; Enter or Space takes one.
      info.addEventListener("keydown", (event) => {
        if (!["ArrowLeft", "ArrowRight"].includes(event.key) || !event.target.matches("[role=radio]")) return;
        const row = Array.from(event.target.parentNode.querySelectorAll("[role=radio]:not(:disabled)"));
        const next = row[row.indexOf(event.target) + (event.key === "ArrowRight" ? 1 : -1)];
        if (next) next.focus();
        event.preventDefault();
      });

      // The library.
      $("[data-tabs]").addEventListener("click", (event) => {
        const button = event.target.closest("[data-tab]");
        if (button) music.setTab(button.dataset.tab);
      });
      // The arrows move along the tabs, and choose as they go.
      $("[data-tabs]").addEventListener("keydown", (event) => {
        if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
        const tabs = all("[data-tab]");
        const index = tabs.indexOf(event.target);
        const next = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1
          : (index + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
        tabs[next].focus();
        // The tab shown again (Home or End on it) keeps its pages.
        if (next !== index) music.setTab(tabs[next].dataset.tab);
        event.preventDefault();
      });
      query.addEventListener("input", () => music.setQuery(query.value));
      $("[data-search-form]").addEventListener("submit", (event) => { event.preventDefault(); music.searchNow(); });
      $("[data-kind]").addEventListener("change", (event) => music.setKind(event.target.value));
      $("[data-source]").addEventListener("change", (event) => music.setSource(event.target.value));
      $("[data-page-back]").addEventListener("click", goBack);
      on("[data-more]", "click", () => music.more());
      $("[data-library]").addEventListener("click", (event) => {
        const every = event.target.closest("[data-all]");
        if (every) { music.setKind(every.dataset.all); return; }
        const section = event.target.closest("[data-section]");
        if (section) { opening(section); music.openSection(section.dataset.section); return; }
        const play = event.target.closest("[data-head-action]");
        if (play && pageHead) { music.act(pageHead.item, play.dataset.headAction); return; }
        const more = event.target.closest("[data-head-more]");
        if (more && pageHead) { openItem(pageHead.item, more); return; }
        const artist = event.target.closest("[data-head-artist]");
        if (artist && pageHead) { opening(artist); music.open(pageHead.artists[Number(artist.dataset.headArtist)]); return; }
        const button = event.target.closest("[data-item]");
        const item = button && shown.get(button.dataset.item);
        if (!item) return;
        // A folder, album, artist or playlist opens; a track offers how to play it.
        if (item.opens) {
          opening(button);
          music.open(item);
        } else openItem(item, button);
      });
      $("[data-item-actions]").addEventListener("click", (event) => {
        const button = event.target.closest("[data-item-action]");
        if (!button || !chosenItem) return;
        $("#item-sheet").close();
        if (button.dataset.itemAction === "open" || button.dataset.itemAction.startsWith("go:")) opening(itemOpener);
        else browsing = false;
        music.act(chosenItem, button.dataset.itemAction);
      });
      $("[data-tidal-start]").addEventListener("click", () => music.tidalStart());
      $("[data-tidal-cancel]").addEventListener("click", () => music.tidalCancel());

      // The group sheet.
      $("[data-candidates]").addEventListener("change", (event) => {
        if (!event.target.matches("input[type=checkbox]")) return;
        if (event.target.checked) picked.add(event.target.value);
        else picked.delete(event.target.value);
        drawGroupSheet();
      });
      $("[data-join]").addEventListener("click", () => {
        const ids = Array.from(picked);
        picked.clear();
        music.join(ids);
        drawGroupSheet();
      });
      $("[data-members]").addEventListener("click", (event) => {
        const button = event.target.closest("[data-leave]");
        if (button) music.leave(button.dataset.leave);
      });
      $("[data-group-end]").addEventListener("click", () => {
        const grouping = music.grouping();
        if (grouping) music.leave(grouping.leader);
      });
    }

    // A volume slider: a drag sends as the session lets it, the finger shown meanwhile.
    function slide(input, name, set) {
      input.addEventListener("input", () => { dragging[name] = true; set(input.value, false); });
      input.addEventListener("change", () => { dragging[name] = false; set(input.value, true); });
      input.addEventListener("pointerup", () => { dragging[name] = false; });
      input.addEventListener("blur", () => { dragging[name] = false; });
    }

    // A key of Listen's (data-button), clicked or held: the button pressed, or null.
    function press(button) {
      if (button === "group_volume_up" || button === "group_volume_down") {
        if (music) music.stepGroupVolume(button === "group_volume_up" ? 1 : -1);
        return button;
      }
      return session.pressKey([button]);
    }

    // Listen's own keys, beyond Session.KEYS: S shuffles, R repeats, G opens
    // the group, / the library's search, and Space off a control plays and
    // pauses. The button pressed, true for a sheet, or null.
    function key(event) {
      const name = event.key, view = listen.view();
      if (!view) return null;
      if (name === "/") return openLibrary("search") || null;
      if (name === "g" || name === "G") return openGroup() || null;
      if (name === "s" || name === "S") return session.pressKey(["shuffle"]);
      if (name === "r" || name === "R") return session.pressKey(["repeat"]);
      if (name === " " && !(event.target.closest && event.target.closest("button, a, input, select, textarea, summary"))) return session.pressKey(["play_pause"]);
      return null;
    }

    // The alert line's words in Listen: connection loss, the speaker not
    // answering, Music Assistant away, then a lock.
    function alert() {
      const view = listen.view();
      const away = music ? music.status() : null;
      const musicDown = Boolean(away && away.state === "ready" && away.text);
      const offline = !session.connected() || Boolean(view && view.connected === false) || musicDown;
      const parts = [];
      if (offline) parts.push(musicDown && session.connected() && (!view || view.connected !== false) ? away.text : global.RemoteSession.NOT_CONNECTED);
      if (view) {
        if (view.problem) parts.push(view.speaker.label + " " + view.problem);
        if (view.music) parts.push(view.music);
        if ((view.locked || view.groupLocked || {}).text) parts.push((view.locked || view.groupLocked).text);
      } else if (away && ["failed", "unavailable"].includes(away.state) && away.text) {
        parts.push(away.text);
      }
      return parts.filter((value, index, list) => value && list.indexOf(value) === index).join(" · ");
    }

    return { update: update, press: press, key: key, alert: alert, openInputs: openInputs, stop: stop };
  }

  global.RemoteListenPage = { create: create };
})(globalThis);
