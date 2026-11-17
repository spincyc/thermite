'use strict';
// stdin: a generated house model (remote_model.build) with a routed room of
// two speakers (argv[2], the room's audio argv[3]; argv[4] the room), a
// routed room without speakers (argv[5]), a room of one speaker alone
// (argv[6], argv[7]) and another (argv[8], argv[9]). Runs the remote page
// (index.html, remote.js and listen.js on every script it loads) in
// remote_dom.js's stand-in for a browser, on the mock backends, and prints
// what it drew, sent and where the focus went. Nothing leaves the process.
const fs = require('fs'), path = require('path'), vm = require('vm');
const {clock, Document} = require('./remote_dom');
const WEB = path.join(__dirname, '..', 'web', 'remote');
const house = JSON.parse(fs.readFileSync(0, 'utf8'));
const [MINI, OWNED, MIXED, WATCH, AMP, DEN, PATIO_MINI, PATIO] = process.argv.slice(2);
// The scripts a page loads, but the house (stdin's) and the shell's frame.
const scripts = (html) => [...fs.readFileSync(path.join(WEB, html), 'utf8').matchAll(/<script[^>]*\ssrc="([^"]+)"/g)].map((match) => match[1])
  .filter((name) => !['house.js', 'shell.js', 'access-config.js'].includes(name));

// An access client whose only lock is room's, as listen_session_harness.js's;
// guest, a Guest kept from free text.
function locking(room, guest) {
  return {native: false, mock: false, guestOnly: false, subscribe: () => () => {}, ready: () => Promise.resolve(), watchLocks: () => {},
    lockSummary: (id) => (id === room ? {locked: true, manual: true} : null), canEdit: () => false,
    current: () => (guest ? {is_guest: true} : null), security: () => (guest ? {guest_text: false} : null)};
}

// The remote page at ?room=room. options: access (a client for the locks),
// music (the mock music's settings), wide (a window 1200 px wide), short
// (and 500 px tall, a phone on its side), stored, html (another page: Home's
// home.html, room null).
function page(room, options = {}) {
  const time = clock();
  const html = options.html || 'index.html';
  const document = new Document(fs.readFileSync(path.join(WEB, html), 'utf8'));
  const stored = new Map(Object.entries(options.stored || {}));
  const windowListeners = {};
  const toasts = [];
  const context = {
    document, console: {log() {}, info() {}, warn() {}, error: (...args) => { throw new Error(args.map((arg) => (arg && arg.stack) || arg).join(' ')); }},
    location: {search: '?backend=mock&latency=4' + (room ? '&room=' + room : ''), href: 'file:///remote/' + html, protocol: 'file:', origin: 'null'},
    URLSearchParams, URL, Promise, JSON, Math, Map, Set, WeakMap, Array, Object, String, Number, Boolean, Error, RegExp, Symbol,
    setTimeout: time.setTimeout, clearTimeout: time.clearTimeout,
    setInterval: (callback, ms) => { const again = () => { callback(); time.setTimeout(again, ms); }; return time.setTimeout(again, ms); },
    clearInterval() {},
    fetch: () => Promise.reject(new Error('offline')),
    localStorage: {getItem: (key) => (stored.has(key) ? stored.get(key) : null), setItem: (key, value) => stored.set(key, String(value)),
      removeItem: (key) => stored.delete(key)},
    CSS: {escape: (text) => String(text).replace(/["\\]/g, '\\$&')},
    navigator: {},
    matchMedia: (query) => ({matches: /min-width: 1200px/.test(query) ? Boolean(options.wide) && !(options.short && /min-height/.test(query))
      : false, addEventListener() {}, addListener() {}}),
    // remote.css and listen.css as far as the scripts ask: the Signal panel shows from 1200 px, tall, in Watch.
    getComputedStyle: (element) => ({getPropertyValue: () => '', display: element.classList.contains('panel--signal') && options.wide
      && !options.short && document.body.dataset.mode !== 'listen' ? 'flex' : 'none'}),
    addEventListener: (type, listener) => { (windowListeners[type] = windowListeners[type] || []).push(listener); },
    REMOTE_HOUSE: house,
    // The shell's links, as shell.js makes them.
    ThermiteShell: {href: (id, search, target) => id + (target ? '?room=' + target : '')},
  };
  document.later = time.setTimeout;
  context.window = context;
  context.globalThis = context;
  context.process = process; vm.createContext(context);
  const Clocked = vm.runInContext('Date', context);
  context.Date = class extends Clocked { static now() { return time.now(); } };
  const made = {};
  for (const name of scripts(html)) {
    vm.runInContext(fs.readFileSync(path.join(WEB, name), 'utf8'), context, {filename: name});
    if (name === 'access.js' && options.access) context.ThermiteAccess.client = options.access;
    // The page's backends kept, so a scenario can see what they sent and make them answer as it wants.
    if (name === 'backend.js') {
      const create = context.RemoteBackend.create;
      context.RemoteBackend.create = (...args) => {
        const backend = made.remote = create(...args);
        // Home's own: whether its stream runs, as each call left it.
        if ((args[1] || {}).view === 'home') {
          made.home = backend;
          backend.streaming = [Boolean(args[1].active)];
          backend.setActive = (active) => backend.streaming.push(Boolean(active));
        }
        const command = backend.command.bind(backend);
        backend.sent = [];
        // Each room the Signal panel read, as it polls.
        backend.diagnosed = [];
        backend.getDiagnostics = (id) => { backend.diagnosed.push(id); return Promise.resolve(null); };
        backend.command = (device, action, value) => { backend.sent.push([device, action, String(value)]); return command(device, action, value); };
        const setInput = backend.setInput.bind(backend);
        backend.setInput = (room, device, value) => { backend.sent.push([device, 'input', String(value)]); return setInput(room, device, value); };
        return backend;
      };
    }
    if (name === 'music-backend.js') {
      const create = context.MusicBackend.create;
      context.MusicBackend.create = (settings) => (made.music = create(Object.assign({}, settings, options.music)));
    }
  }
  const $ = (selector) => document.querySelector(selector);
  const all = (selector) => document.querySelectorAll(selector);
  const focus = () => {
    const at = document.activeElement;
    if (at === document.body) return 'body';
    return at.dataset.focus || (at.hasAttribute('data-page-title') || at.hasAttribute('data-head-name') ? 'title'
      : at.localName + ':' + (at.getAttribute('aria-label') || at.textContent.trim()));
  };
  // A key pressed where the focus is.
  const key = async (name, at) => {
    const event = (at || document.activeElement).dispatch('keydown', {key: name, ctrlKey: false, metaKey: false, altKey: false});
    await time.tick(50);
    return event.defaultPrevented;
  };
  const toast = () => [$('#toast .toast__title strong') ? $('#toast .toast__title strong').textContent : null,
    $('#toast .toast__body') ? $('#toast .toast__body').textContent : null];
  const shown = (element) => Boolean(element) && !element.closest('[hidden]');
  const words = (element) => element.textContent.replace(/\s+/g, ' ').trim();
  return {time, document, $, all, focus, key, toast, shown, words, made, stored, windowListeners, toasts};
}

// The room sheet's rows: in Watch the rooms with a screen, in Listen those
// with speakers, what each plays; the padlock in its own column.
async function rooms(out) {
  const p = page(WATCH);
  const {time, $, all, words, key} = p;
  await time.tick(1500);
  const rows = () => all('#room-sheet [data-room-list] li').map((row) => [words(row.querySelector('.room__label')),
    words(row.querySelector('.room__source-text')), words(row.querySelector('.room__playing'))]);
  out.sheet = rows();
  out.sidebar = all('.panel--rooms [data-room-list] .room').length;
  out.columns = Array.from($('#room-sheet [data-room-list] .room').children).map((child) => child.getAttribute('class'));
  out.lists = all('[data-room-list], [data-input-list]').map((list) => [list.hasAttribute('data-room-list'), list.classList.contains('rooms--house')]);
  await key('y', p.document.body);
  await time.tick(400);
  out.listen = [rows(), words($('[data-room-name]')), all('.panel--rooms [data-room-list] .room').length];
  // Y on a room the switch rebuilds keeps the focus on the room on show.
  $('.panel--rooms [data-room-list] .room[aria-current="true"]').focus();
  await key('y');
  await time.tick(400);
  out.focus = [p.focus(), words($('[data-room-name]'))];
}

// The switch in every room of a house with both, the same place in each; Y and the arrows.
async function modes(out) {
  const p = page(WATCH);
  const {time, $, all, key, shown} = p;
  await time.tick(1500);
  const state = () => ({switch: shown($('[data-modes]')), checked: all('[data-modes] [aria-checked="true"]').map((option) => option.dataset.mode),
    mode: p.document.body.dataset.mode, listen: shown($('#listen')), controls: shown($('#controls')), bubbles: shown($('[data-targets]')),
    speakers: shown($('[data-speakers]')), shortcuts: all('[data-shortcuts] li span').map((span) => span.textContent).slice(0, 2)});
  out.watchOnly = state();
  // A room without speakers gives way to the first with them, and Watch keeps it.
  out.watchOnlyY = [await key('y'), state().mode, p.words($('[data-room-name]'))];
  out.watchAgain = [await key('y'), state().mode, p.words($('[data-room-name]'))];
  $('#room-sheet [data-room="' + MIXED + '"]').click();
  await time.tick(400);
  out.mixed = state();
  out.y = [await key('Y'), state()];
  out.yAgain = [await key('Y'), state()];
  $('[data-modes] [data-mode="listen"]').click();
  await time.tick(50);
  out.clicked = state().mode;
  const radio = $('[data-modes] [data-mode="listen"]');
  radio.focus();
  out.arrow = [await key('ArrowLeft', radio), state().mode, p.focus()];
  out.tabStops = all('[data-modes] [data-mode]').map((option) => option.getAttribute('tabindex'));
  await key('Y');
  $('#room-sheet [data-room="' + DEN + '"]').click();
  await time.tick(400);
  out.den = state();
  // Kept for every room: back in the mixed room it is still in Listen.
  await key(']', p.document.body);
  await key('[', p.document.body);
  $('#room-sheet [data-room="' + MIXED + '"]').click();
  await time.tick(400);
  out.kept = state().mode;
}

// Listen in the mixed room: bubbles, what plays, keys, chips, volume, Info.
async function face(out) {
  const p = page(MIXED, {stored: {'remote.mode': 'listen'}});
  const {time, $, all, key, words, shown, made} = p;
  await time.tick(1500);
  out.speakers = all('[data-speakers] .target').map((bubble) => [bubble.dataset.speaker, words(bubble), bubble.getAttribute('aria-current'),
    bubble.dataset.state, bubble.getAttribute('aria-keyshortcuts')]);
  out.now = [words($('[data-listen-title]')), words($('[data-listen-line]')), $('[data-listen-now]').dataset.playing,
    shown($('[data-progress]')), shown($('[data-art-image]'))];
  out.transport = all('[data-listen-transport] .key').map((button) => [button.dataset.button, button.getAttribute('aria-label'),
    button.disabled, button.getAttribute('aria-pressed'), button.querySelector('use').getAttribute('href')]);
  out.chips = all('[data-listen-chips] .chip').map((chip) => [chip.dataset.action, shown(chip), words(chip)]);
  out.caption = words($('[data-listen-caption]'));
  out.mute = shown($('#listen [data-button="mute"]'));
  out.level = [$('[data-listen-level]').min, $('[data-listen-level]').max, $('[data-listen-level]').value];
  out.alert = shown($('#alert'));
  // Repeat cycles off, all, one: one has its own icon.
  for (let press = 0; press < 2; press += 1) {
    $('#listen [data-button="repeat"]').click();
    await time.tick(400);
  }
  out.repeatOne = [$('#listen [data-button="repeat"]').getAttribute('aria-label'), $('#listen [data-button="repeat"] use').getAttribute('href')];
  // + held: one press, then one each REPEAT_EVERY after REPEAT_DELAY; the click that ends it presses nothing more.
  const plus = $('#listen [data-button="volume_up"]'), from = made.remote.sent.length;
  plus.dispatch('pointerdown', {pointerType: 'touch', button: 0, isPrimary: true, clientX: 0, clientY: 0});
  await time.tick(420 + 2 * 150 + 10);
  p.document.dispatch = null;
  plus.dispatch('pointerup', {pointerType: 'touch'});
  plus.click(1);
  await time.tick(700);
  out.held = made.remote.sent.slice(from).filter(([, action]) => action === 'volume').length;
  // Keys: M mutes, a digit chooses a speaker, S shuffles, Space off a control plays or pauses, < goes back a track.
  const before = made.remote.sent.length;
  await key('m', p.document.body);
  await time.tick(400);
  out.muted = [made.remote.sent.slice(before).map(([, action, value]) => action + ':' + value), $('#listen [data-button="mute"]').getAttribute('aria-pressed')];
  await key('2', p.document.body);
  await time.tick(400);
  out.digit = all('[data-speakers] .target').map((bubble) => bubble.getAttribute('aria-current'));
  await key('1', p.document.body);
  await time.tick(400);
  const music = made.music.sent.length;
  out.keys = [await key('s', p.document.body), await key(' ', p.document.body), await key('<', p.document.body)];
  await time.tick(400);
  out.musicSent = made.music.sent.slice(music).map((sent) => sent.slice(0, 3).join(':'));
  out.enter = await key('Enter', p.document.body);
  out.shortcuts = all('[data-shortcuts] li').map((item) => words(item));
  // Info: the speaker's power, settings by section and facts, as the Audio page drew them.
  $('#info-open').click();
  await time.tick(50);
  out.info = {sheet: $('#info-sheet').open, watch: shown($('#info-sheet [data-info]')), label: $('#info-open').getAttribute('aria-label'),
    titles: all('[data-listen-info] .info__title').map((title) => words(title)), speaker: words($('[data-listen-info] .listen-info__speaker')),
    switches: all('[data-listen-info] [role=switch]').map((button) => words(button)),
    selects: all('[data-listen-info] select').map((select) => select.getAttribute('data-setting'))};
  const sentBefore = made.remote.sent.length;
  $('[data-listen-info] [data-setting="' + MINI + '_eq"]').click();
  const select = $('[data-listen-info] select[data-setting="' + MINI + '_eq_preset"]');
  select.value = 'Jazz';
  select.dispatch('change');
  await time.tick(400);
  out.settings = made.remote.sent.slice(sentBefore).map(([, action, value]) => action + ':' + value);
  $('#info-sheet').close();
  // Input: Remotes' input sheet lists the speaker's inputs.
  $('[data-action="listen-input"]').click();
  await time.tick(50);
  out.inputs = [$('#input-sheet').open, all('[data-input-list] .room').map((row) => [row.dataset.listenInput, row.getAttribute('aria-current')])];
  const chosen = made.remote.sent.length;
  $('[data-input-list] [data-listen-input="line-in"]').click();
  await time.tick(400);
  out.input = [$('#input-sheet').open, made.remote.sent.slice(chosen).map(([, action, value]) => action + ':' + value),
    words($('[data-action="listen-input"]'))];
  // The Amp Pro: its power and what it reports of itself.
  await key('2', p.document.body);
  await time.tick(400);
  out.amp = [all('[data-listen-info] .info__title').map((title) => words(title)), all('[data-listen-info] [data-power]').length,
    all('[data-listen-info] dl.map dt').map((term) => words(term))];
  const powered = made.remote.sent.length;
  $('[data-listen-info] [data-power]').click();
  await time.tick(400);
  out.amp.push(made.remote.sent.slice(powered).map(([device, action, value]) => [device, action, value]));
}

// One speaker in a room: no bubbles; its name and padlock by its volume.
async function alone(out) {
  const {time, $, words, shown} = page(DEN);
  await time.tick(1500);
  out.den = [shown($('[data-modes]')), shown($('[data-speakers]')), words($('[data-listen-caption]')), shown($('#controls'))];
}

// Watch is today's remote; its volume caption listens to the room's speaker.
async function watch(out) {
  const p = page(MIXED);
  const {time, $, all, shown, words} = p;
  await time.tick(1500);
  out.mixed = [p.document.body.dataset.mode, shown($('#listen')), all('[data-targets] .target').length > 1, shown($('.targets-wrap')),
    all('#controls [data-button]').length > 0];
  const link = $('[data-caption=audio] [data-action="listen-to"]') || $('[data-caption=fader] [data-action="listen-to"]');
  out.link = [link ? link.getAttribute('aria-label') : null];
  link.click();
  await time.tick(400);
  out.listened = [p.document.body.dataset.mode, words($('[data-listen-caption]')),
    p.all('[data-speakers] .target').map((bubble) => bubble.getAttribute('aria-current'))];
  const west = page(WATCH);
  await west.time.tick(1500);
  out.west = [west.document.body.dataset.mode, west.shown(west.$('[data-targets]')), west.shown(west.$('#listen')),
    west.$('[data-caption=audio]').querySelector('button') === null];
}

// The room's lock, as Watch shows one: padlock, controls disabled, the alert's words.
async function locks(out) {
  const p = page(MIXED, {access: locking(MIXED), stored: {'remote.mode': 'listen'}});
  const {time, $, all, words, made, toast} = p;
  await time.tick(1500);
  out.bubbles = all('[data-speakers] .target').map((bubble) => [bubble.hasAttribute('data-locked'), bubble.getAttribute('aria-current')]);
  out.alert = [p.shown($('#alert')), words($('#alert-text'))];
  out.disabled = all('#listen button, #listen input').filter((control) => p.shown(control)).map((control) => [control.dataset.button
    || control.dataset.action || control.getAttribute('aria-label'), control.disabled]);
  const sent = made.remote.sent.length;
  await p.key('+', p.document.body);
  await time.tick(400);
  out.refused = [made.remote.sent.length - sent, toast()];
  const den = page(DEN, {access: locking(MIXED)});
  await den.time.tick(1500);
  out.free = [den.shown(den.$('#alert')), den.$('#listen [data-button="volume_up"]').disabled];
}

// The library: in its sheet on a phone, beside the remote on a desktop.
async function library(out) {
  const p = page(MIXED, {stored: {'remote.mode': 'listen'}});
  const {time, $, all, words, made, toast, focus} = p;
  await time.tick(1500);
  $('[data-action="listen-library"]').click();
  await time.tick(50);
  out.sheet = [$('#library-sheet').open, $('[data-library]').parentNode === $('[data-library-narrow]'),
    all('[data-tab]').map((tab) => [tab.dataset.tab, tab.getAttribute('aria-selected')])];
  $('[data-query]').value = 'harbour';
  $('[data-query]').dispatch('input');
  await time.tick(2000);
  out.kinds = all('[data-kind] option').map((option) => option.textContent);
  out.sources = all('[data-source] option').map((option) => option.textContent);
  out.sections = all('[data-results] .library-section h3').map((title) => words(title));
  const album = all('[data-results] [data-item]').find((button) => /^Harbour Lights, The Lanterns/.test(button.getAttribute('aria-label')));
  album.focus();
  album.click();
  out.item = [$('#item-sheet').open, all('[data-item-actions] button').map((button) => words(button)), words($('[data-item-target]'))];
  $('[data-item-actions] [data-item-action="next"]').focus();
  $('[data-item-actions] [data-item-action="next"]').click();
  await time.tick(400);
  out.played = [$('#item-sheet').open, toast(), made.music.sent.slice(-1)[0].slice(0, 2).concat(made.music.sent.slice(-1)[0].slice(3))];
  $('[data-tab="queue"]').click();
  await time.tick(600);
  out.queue = [p.shown($('#library-panel-queue')), all('[data-queue-items] [data-item]').length > 0];
  $('#library-sheet').close();
  out.slash = [await p.key('/', p.document.body), $('#library-sheet').open, focus(), all('[data-tab]').find((tab) => tab.getAttribute('aria-selected') === 'true').dataset.tab];
  const wide = page(MIXED, {wide: true, stored: {'remote.mode': 'listen'}});
  await wide.time.tick(1500);
  wide.$('[data-action="listen-library"]').click();
  await wide.time.tick(50);
  out.wide = [wide.$('[data-library]').parentNode === wide.$('[data-library-wide]'), wide.$('#library-sheet').open, wide.shown(wide.$('[data-tabs]'))];
  // Typing in the library's search is the field's, not the remote's keys.
  wide.$('[data-query]').focus();
  out.typing = [await wide.key('m'), await wide.key('Y'), wide.document.body.dataset.mode];
}

// Browse: a folder opened, Back, and a track's sheet; the focus follows each.
async function browse(out) {
  const p = page(MIXED, {stored: {'remote.mode': 'listen'}});
  const {time, $, focus} = p;
  await time.tick(1500);
  $('[data-action="listen-library"]').click();
  $('[data-tab="browse"]').click();
  await time.tick(400);
  // A key pressed on a button: it has the focus, then the click.
  const pressed = async (button) => { button.focus(); button.click(); await time.tick(0); };
  const bar = (q) => [q.shown(q.$('[data-page-bar]')), q.$('[data-page-back-label]').textContent, q.$('[data-page-title]').textContent];
  out.root = bar(p);
  await pressed($('[data-page-items] [data-item]'));
  out.opening = [...bar(p), focus()];
  await time.tick(400);
  out.opened = [...bar(p), focus()];
  await pressed($('[data-page-back]'));
  out.back = [...bar(p), focus()];
  for (let depth = 0; depth < 3; depth += 1) {
    await pressed($('[data-page-items] [data-item]'));
    await time.tick(400);
  }
  out.album = [...bar(p), focus()];
  await pressed($('[data-page-items] [data-item]'));
  out.sheet = [$('#item-sheet').open, focus()];
  await pressed($('[data-item-actions] [data-item-action="next"]'));
  out.played = [$('#item-sheet').open, focus()];
  // Taken elsewhere while a folder reads, to a control in sight: the focus stays there.
  const other = page(MIXED, {wide: true, stored: {'remote.mode': 'listen'}});
  await other.time.tick(1500);
  other.$('[data-tab="browse"]').click();
  await other.time.tick(400);
  const folder = other.$('[data-page-items] [data-item]');
  folder.focus();
  folder.click();
  await other.time.tick(0);
  out.elsewhere = [...bar(other), other.focus()];
  other.$('#listen [data-button="volume_up"]').focus();
  await other.time.tick(400);
  out.elsewhere.push(other.$('[data-page-items]').querySelectorAll('[data-item]').length > 0, other.focus());
  // A folder read that failed leaves the focus on the title once, then lets it be; nothing scrolls the page.
  const failing = page(MIXED, {wide: true, stored: {'remote.mode': 'listen'}});
  await failing.time.tick(1500);
  const read = failing.made.music.browse.bind(failing.made.music);
  failing.made.music.browse = (ref, ...rest) => (ref ? Promise.reject(Object.assign(new Error('music_failed'), {code: 'music_failed', status: 502}))
    : read(ref, ...rest));
  failing.$('[data-tab="browse"]').click();
  await failing.time.tick(400);
  failing.$('[data-page-items] [data-item]').click();
  await failing.time.tick(400);
  out.failed = [failing.$('[data-page-text]').textContent !== '', failing.focus()];
  failing.$('[data-page-title]').blur();
  failing.$('#listen [data-button="volume_up"]').click();
  await failing.time.tick(400);
  out.failed.push(failing.focus(), failing.document.scrolls);
}

// An artist found and opened over the search, a single of it, its header's
// Play, Shuffle and artist, a track played from where it is, and Back to the search.
async function pages(out) {
  const p = page(MIXED, {music: {tidal: true}, stored: {'remote.mode': 'listen'}});
  const {time, $, all, words, made, focus} = p;
  await time.tick(1500);
  $('[data-action="listen-library"]').click();
  await time.tick(50);
  $('[data-query]').value = 'juniper';
  $('[data-query]').dispatch('input');
  await time.tick(2000);
  const pressed = async (button) => { button.focus(); button.click(); await time.tick(400); };
  const head = () => [p.shown($('[data-page-head]')), words($('[data-head-name]')), words($('[data-head-by]')), words($('[data-head-line]')),
    $('[data-head-art]').dataset.kind];
  const artist = all('[data-results] [data-item]').find((button) => /^Juniper Vale, Artist/.test(button.getAttribute('aria-label')));
  out.row = [artist.getAttribute('aria-haspopup'), words(artist)];
  await pressed(artist);
  out.artist = {shown: [p.shown($('#library-panel-search')), p.shown($('[data-page]')), $('[data-page]').getAttribute('aria-labelledby')],
    bar: [p.shown($('[data-page-bar]')), words($('[data-page-back-label]')), p.shown($('[data-page-title]'))], head: head(), focus: focus(),
    sections: all('[data-page-sections] .library-section h3').map((title) => words(title)),
    play: all('[data-head-action]').map((button) => [button.getAttribute('aria-label'), button.disabled])};
  const single = all('[data-page-sections] [data-item]').find((button) => /^Wide Awake, Juniper Vale · Single/.test(button.getAttribute('aria-label')));
  await pressed(single);
  out.album = {back: words($('[data-page-back-label]')), head: head(), focus: focus(),
    rows: all('[data-page-items] [data-item]').map((button) => [words(button.querySelector('.library-item__number')), words(button.querySelector('.library-item__name')),
      words(button.querySelector('.library-item__line'))])};
  let from = made.music.sent.length;
  $('[data-head-action="replace"]').click();
  await time.tick(400);
  $('[data-head-action="shuffle"]').click();
  await time.tick(400);
  out.headPlayed = made.music.sent.slice(from).map((sent) => sent.slice(2));
  await pressed($('[data-head-more]'));
  out.more = [$('#item-sheet').open, all('[data-item-actions] button').map((button) => words(button))];
  $('#item-sheet').close();
  await pressed($('[data-page-items] [data-item]'));
  out.track = [$('#item-sheet').open, all('[data-item-actions] button').map((button) => words(button)), focus()];
  from = made.music.sent.length;
  await pressed($('[data-item-actions] [data-item-action="from"]'));
  out.from = [$('#item-sheet').open, made.music.sent.slice(from).map((sent) => sent.slice(2))];
  // The header's artist opens its page again, over the single.
  await pressed($('[data-head-artist]'));
  out.again = [words($('[data-page-back-label]')), head()[1]];
  // Home on the tab shown keeps its pages.
  out.homeKey = [await p.key('Home', $('[data-tab="search"]')), p.shown($('[data-page]')), head()[1]];
  // Back, page by page, returns the focus to what opened the page left.
  await pressed($('[data-page-back]'));
  out.backToSingle = [words($('[data-page-back-label]')), head()[1], focus()];
  await pressed($('[data-page-back]'));
  out.backToArtist = [words($('[data-page-back-label]')), head()[1], focus()];
  await pressed($('[data-page-back]'));
  out.backToSearch = [p.shown($('#library-panel-search')), p.shown($('[data-page]')), $('[data-query]').value,
    all('[data-results] [data-item]').length > 0, focus()];
  // The tab shown, chosen again, leaves a page for its own list.
  await pressed(all('[data-page-sections] [data-item]')[0] || artist);
  $('[data-tab="search"]').click();
  await time.tick(100);
  out.home = [p.shown($('#library-panel-search')), p.shown($('[data-page]'))];
}

// The group sheet: who joined, who could not and why, who was asked; leave and end.
async function group(out) {
  const p = page(MIXED, {stored: {'remote.mode': 'listen'}});
  const {time, $, all, words, made, key} = p;
  await time.tick(1500);
  out.chip = [words($('[data-action="listen-group"]')), $('[data-action="listen-group"]').getAttribute('aria-keyshortcuts')];
  await key('g', p.document.body);
  out.open = [$('#group-sheet').open, words($('[data-group-title]')), all('[data-candidates] input').map((box) => [box.value, box.disabled])];
  for (const id of [AMP, PATIO_MINI, OWNED]) {
    const box = $('[data-candidates] input[value="' + id + '"]');
    box.checked = true;
    box.dispatch('change');
  }
  out.join = words($('[data-join]'));
  made.music.simulateUnconfirmed([OWNED]);
  $('[data-join]').click();
  await time.tick(600);
  out.result = all('[data-group-result] p').map((line) => [words(line), line.getAttribute('class')]);
  out.members = all('[data-members] .listen-member').map((row) => words(row));
  out.grouped = [words($('[data-action="listen-group"]')), p.shown($('[data-group-volume-wrap]')), words($('[data-group-caption]'))];
  const before = made.music.sent.length;
  $('#listen [data-button="group_volume_up"]').click();
  await time.tick(400);
  $('[data-members] [data-leave="' + PATIO_MINI + '"]').click();
  await time.tick(400);
  out.sent = made.music.sent.slice(before).map((sent) => sent.slice(0, 2).join(':'));
  $('[data-group-end]').click();
  await time.tick(400);
  out.ended = [made.music.sent.slice(-1)[0].slice(0, 2).join(':'), p.shown($('[data-group-volume-wrap]'))];
}

// Covers the gateway was too busy to fetch (503): each asked for once more
// after ART_RETRY_MS and up to ART_JITTER_MS (a thumbnail, lazy, eagerly),
// then left to its placeholder.
async function covers(out) {
  const p = page(MIXED, {stored: {'remote.mode': 'listen'}});
  const {time, $, document, made} = p;
  const backend = made.music;
  const art = (n) => n.toString(16).padStart(64, '0'), playing = art(4095);
  backend.art = (id, size) => (id ? '/api/v1/music/art/' + id + '?size=' + size : null);
  Object.values(backend.items).forEach((item, index) => { item.art = art(index + 1); });
  const speaker = backend.speaker.bind(backend);
  backend.speaker = (id) => {
    const view = speaker(id);
    if (view.now_playing && view.now_playing.title) view.now_playing.art = playing;
    return view;
  };
  const asked = (src) => document.images.filter((image) => image === src).length;
  const answered = new Map();
  document.answerImage = (src) => {
    answered.set(src, (answered.get(src) || []).concat(time.now()));
    return src.includes(playing) && asked(src) > 1;
  };
  backend.simulateNext(MINI);
  const image = $('[data-art-image]');
  await time.tick(1500);
  const failed = (img) => [img.hidden, img.hasAttribute('data-failed'), asked(img.getAttribute('src'))];
  out.cover = [failed(image)];
  await time.tick(3000);
  out.cover.push(failed(image));
  $('[data-action="listen-library"]').click();
  $('[data-tab="search"]').click();
  $('[data-query]').value = 'harbour';
  $('[data-query]').dispatch('input');
  await time.tick(2000);
  await time.tick(5000);
  const thumbs = $('[data-results]').querySelectorAll('img');
  out.thumbs = [thumbs.length > 0, [...new Set(thumbs.map((thumb) => failed(thumb).join()))]];
  const after = thumbs.map((thumb) => answered.get(thumb.getAttribute('src'))).map(([first, again]) => again - first);
  out.jitter = [after.length, after.every((ms) => ms >= 2000 && ms <= 3000), new Set(after).size > 1];
  out.cover.push(asked(image.src));
  // A position dragged and let go unchanged: the next drawing shows the speaker's again.
  const seek = $('[data-seek]');
  seek.value = '200';
  seek.dispatch('input');
  out.dragged = $('[data-elapsed]').textContent;
  seek.dispatch('pointerup');
  $('#listen [data-button="volume_up"]').click();
  await time.tick(400);
  out.released = [seek.value !== '200', $('[data-elapsed]').textContent !== '3:20'];
}

// Without music on the host, or Music Assistant away, or a Guest kept from titles.
async function music(out) {
  const absent = page(MIXED, {music: {configured: false}, stored: {'remote.mode': 'listen'}});
  await absent.time.tick(1500);
  out.absent = [absent.all('[data-listen-chips] .chip').filter((chip) => absent.shown(chip)).map((chip) => chip.dataset.action),
    absent.all('[data-listen-info] .info__note').map((note) => absent.words(note)), absent.words(absent.$('[data-listen-title]'))];
  const away = page(MIXED, {stored: {'remote.mode': 'listen'}});
  await away.time.tick(1500);
  away.made.music.simulateUnavailable('music_unavailable');
  await away.time.tick(1500);
  out.away = [away.shown(away.$('#alert')), away.words(away.$('#alert-text')), away.shown(away.$('[data-action="listen-library"]'))];
  // The gateway says so once the library is read; access says so before.
  for (const [name, options] of [['guest', {music: {guest: true}}], ['told', {access: locking(null, true)}]]) {
    const guest = page(MIXED, Object.assign({stored: {'remote.mode': 'listen'}}, options));
    await guest.time.tick(1500);
    guest.$('[data-action="listen-library"]').click();
    guest.$('[data-tab="browse"]').click();
    await guest.time.tick(400);
    out[name] = [guest.shown(guest.$('[data-action="listen-library"]')), guest.words(guest.$('[data-blind]')), guest.shown(guest.$('[data-blind]')),
      guest.all('[data-panel]').filter((panel) => guest.shown(panel)).length, guest.words(guest.$('[data-listen-title]'))];
  }
  // An Admin signs Music Assistant into TIDAL from the library.
  const admin = page(MIXED, {wide: true, music: {tidal: false}, stored: {'remote.mode': 'listen'}});
  await admin.time.tick(1500);
  out.tidal = [admin.shown(admin.$('[data-tidal]')), admin.words(admin.$('[data-tidal-text]'))];
  admin.$('[data-tidal-start]').click();
  await admin.time.tick(600);
  out.tidal.push(admin.shown(admin.$('[data-tidal-code]')), admin.words(admin.$('[data-tidal-text]')));
}

// Watch and Listen share Remotes' input sheet: each lists its own device's inputs,
// and a report from Watch's device leaves Listen's list as it is.
async function inputs(out) {
  const p = page(MIXED);
  const {time, $, all, key, made} = p;
  await time.tick(1500);
  const listed = () => [$('#input-sheet').open, all('[data-input-list] button').map((button) => button.dataset.listenInput
    ? 'listen:' + button.dataset.listenInput : 'watch:' + button.dataset.input)];
  const watching = async () => {
    $('[data-targets] [data-device="example_display"]').click();
    await time.tick(400);
    $('#controls [data-button="input"]').click();
    await time.tick(50);
    return listed();
  };
  const listening = async () => {
    $('[data-action="listen-input"]').click();
    await time.tick(50);
    return listed();
  };
  out.watch = await watching();
  $('#input-sheet').close();
  await key('y', p.document.body);
  out.listen = await listening();
  $('#input-sheet').close();
  await key('y', p.document.body);
  out.watchAgain = await watching();
  $('#input-sheet').close();
  await key('y', p.document.body);
  out.listenAgain = await listening();
  // The TV reports while Listen's list is open.
  made.remote.updateDevice('example_display', 'input', 'hdmi2');
  await time.tick(1500);
  out.reported = listed();
  const sent = made.remote.sent.length;
  $('[data-input-list] button').click();
  await time.tick(400);
  out.tapped = made.remote.sent.slice(sent).map(([device, action]) => device + ':' + action);
}

// Diagnostics poll while the Signal panel shows: from 1200 px only in Watch;
// and Listen's Info, opened after D in Watch, is the speaker's, not the signal's.
async function signal(out) {
  const p = page(MIXED, {wide: true});
  const {time, $, key, made} = p;
  await time.tick(1500);
  const reads = async (ms) => {
    const from = made.remote.diagnosed.length;
    await time.tick(ms);
    return made.remote.diagnosed.length - from;
  };
  out.watch = await reads(25000);
  await key('y', p.document.body);
  out.listen = await reads(25000);
  await key('y', p.document.body);
  out.back = await reads(25000);
  // A room of speakers alone chosen while it shows: it has no signal, so none is read.
  await key('y', p.document.body);
  const before = made.remote.diagnosed.length;
  $('#room-sheet [data-room="' + DEN + '"]').click();
  await time.tick(25000);
  out.speakers = [p.document.body.dataset.mode, made.remote.diagnosed.slice(before)];
  const narrow = page(MIXED);
  await narrow.time.tick(1500);
  const from = narrow.made.remote.diagnosed.length;
  await narrow.key('d', narrow.document.body);
  await narrow.time.tick(100);
  out.d = [narrow.$('#info-sheet').open, narrow.made.remote.diagnosed.length - from];
  narrow.$('#info-sheet').close();
  await narrow.time.tick(100);
  await narrow.key('y', narrow.document.body);
  narrow.$('#info-open').click();
  await narrow.time.tick(100);
  const opened = narrow.made.remote.diagnosed.length;
  await narrow.time.tick(25000);
  out.listenInfo = [narrow.$('#info-sheet').open, narrow.made.remote.diagnosed.length - opened];
}

// Home: Watch's rooms with their sources, then Listen's with what plays, the
// padlock in its own column; the switch is the remote's own choice, and Y too.
async function home(out) {
  const p = page(null, {html: 'home.html'});
  const {time, $, all, words, key, stored} = p;
  await time.tick(1500);
  const rows = () => all('[data-home-rooms] tr').map((row) => row.children.map((cell) => words(cell)));
  out.watch = [rows(), $('[data-home-modes]').hidden, words($('[data-home-what]')), all('[data-home-rooms] a').map((link) => link.getAttribute('href'))];
  $('[data-home-modes] [data-mode="listen"]').click();
  await time.tick(1500);
  out.listen = [rows(), words($('[data-home-what]')), stored.get('remote.mode'), words($('[data-home-status]')),
    all('[data-home-modes] [data-mode]').map((option) => [option.getAttribute('aria-checked'), option.getAttribute('tabindex')])];
  await key('y', p.document.body);
  out.y = [stored.get('remote.mode'), rows().length];
  // Its stream runs in Watch alone; Listen's sessions read the speakers.
  const streaming = made => made.home.streaming[made.home.streaming.length - 1];
  out.streams = [streaming(p.made)];
  $('[data-home-modes] [data-mode="listen"]').click();
  await time.tick(50);
  out.streams.push(streaming(p.made));
  // Y on a row the switch rebuilds moves the focus to the switch.
  $('[data-home-rooms] a').focus();
  await key('y');
  out.focus = [p.focus(), stored.get('remote.mode'), streaming(p.made)];
}

// 1200 px wide but 500 tall (a phone on its side): no right column, so the library opens its sheet.
async function sideways(out) {
  const p = page(MIXED, {wide: true, short: true, stored: {'remote.mode': 'listen'}});
  await p.time.tick(1500);
  p.$('[data-action="listen-library"]').click();
  await p.time.tick(50);
  out.library = [p.$('[data-library]').parentNode === p.$('[data-library-narrow]'), p.$('#library-sheet').open];
}

(async () => {
  const report = {};
  for (const [name, run] of Object.entries({rooms, modes, face, alone, watch, locks, library, browse, pages, group, covers, music, inputs, signal,
    sideways, home})) {
    report[name] = {};
    // A scenario that throws says where, and the rest still run.
    await run(report[name]).catch((error) => { report[name].error = error.stack.split('\n').slice(0, 3).join(' '); });
  }
  process.stdout.write(JSON.stringify(report));
  process.exit(0);
})().catch((error) => { process.stderr.write(error.stack + '\n'); process.exit(1); });
