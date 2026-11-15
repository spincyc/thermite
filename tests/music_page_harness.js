'use strict';
// The Music page's session (web/remote/music-session.js) on the mock's
// invented speakers, on the gateway backend (music-backend.js) over a fake
// access client and stream, and access.js's music calls on a fake fetch.
// Every timer runs on a clock the harness moves, so nothing waits on the
// machine's speed. Prints what each showed and sent; nothing leaves the
// process.
const fs = require('fs'), path = require('path'), vm = require('vm');
const WEB = path.join(__dirname, '..', 'web', 'remote');
console.info = console.warn = () => {};
for (const name of ['navigation.js', 'access.js', 'music-backend.js', 'music-session.js']) {
  vm.runInThisContext(fs.readFileSync(path.join(WEB, name), 'utf8'), {filename: name});
}
const {MusicBackend, RemoteMusic, ThermiteAccess} = globalThis;
const START = Date.UTC(2026, 10, 15, 12, 0, 0);

// A clock the harness moves: every promise settles before the next timer runs.
function clock() {
  let time = START, made = 0;
  const timers = new Map();
  const drain = () => new Promise((resolve) => setImmediate(resolve));
  return {
    now: () => time,
    setTimeout: (callback, ms) => { made += 1; timers.set(made, {at: time + Math.max(0, Number(ms) || 0), callback, made}); return made; },
    clearTimeout: (id) => { timers.delete(id); },
    async tick(ms) {
      const end = time + ms;
      await drain();
      for (;;) {
        const due = [...timers.entries()].filter(([, timer]) => timer.at <= end)
          .sort((a, b) => a[1].at - b[1].at || a[1].made - b[1].made)[0];
        if (!due) break;
        timers.delete(due[0]);
        time = Math.max(time, due[1].at);
        due[1].callback();
        await drain();
      }
      time = end;
      await drain();
    },
  };
}

// The mock on the clock, its reads recorded beside the commands it records itself.
function mock(time, settings = {}) {
  const backend = new MusicBackend.MockMusic({latency: 50, now: time.now, setTimeout: time.setTimeout, ...settings});
  backend.reads = [];
  for (const name of ['overview', 'search', 'browse', 'queue', 'tidal']) {
    const read = backend[name].bind(backend);
    backend[name] = (...args) => { backend.reads.push([name, ...args.map((value) => value === undefined ? null : value)]); return read(...args); };
  }
  return backend;
}

function session(time, backend, extra = {}) {
  const music = RemoteMusic.create({backend, now: time.now, setTimeout: time.setTimeout, clearTimeout: time.clearTimeout, watchLocks: false, ...extra});
  music.log = {toasts: [], announced: [], changes: 0};
  music.subscribe((event) => {
    if (event.type === 'toast') music.log.toasts.push([event.title, event.body, event.tone]);
    else if (event.type === 'announce') music.log.announced.push(event.message);
    else music.log.changes += 1;
  });
  return music;
}

// An access client as music-session.js reads one: locks by room, a Guest or
// not; tell(type) notifies its listeners as access.js does.
function fakeAccess({locked = [], guest = false, guestText = true, admin = false} = {}) {
  const listeners = new Set();
  const access = {native: false, mock: false, locked, guest, guestText, canEdit: () => admin,
    current: () => ({is_guest: access.guest}), security: () => ({guest_text: access.guestText}),
    lockSummary: (room) => (locked.includes(room) ? {locked: true} : null), ready: () => Promise.resolve(), watchLocks() {},
    subscribe: (listener) => { listeners.add(listener); return () => listeners.delete(listener); },
    tell: (type) => listeners.forEach((listener) => listener({type}))};
  return access;
}

const blocks = (music) => music.speakers().map((block) => ({group: block.group, leader: block.leader, line: block.line,
  speakers: block.speakers.map((row) => [row.id, row.name, row.line, row.playing, row.locked])}));
const summary = (view) => view && {speaker: view.speaker, title: view.title, line: view.line, now: view.now, playing: view.playing,
  playLabel: view.playLabel, progress: view.progress && view.progress.text, shuffle: view.shuffle.label, repeat: view.repeat.label,
  volume: [view.volume.value, view.volume.pending], group: view.group && {text: view.group.text, leader: view.group.leader,
  volume: view.group.volume.value}, queueOf: view.queueOf, locked: view.locked && view.locked.text, canSkip: view.canSkip};
const names = (items) => items.map((item) => item.name);
const sentSince = (backend, from) => backend.sent.slice(from);

async function overview(out) {
  const time = clock(), backend = mock(time), music = session(time, backend);
  out.reading = music.status();
  music.start();
  await time.tick(100);
  out.status = music.status();
  out.blocks = blocks(music);
  out.selected = music.selected();
  out.view = summary(music.view());
  out.progress = [music.view().progress.elapsed, music.view().progress.duration, music.view().progress.left];
  await time.tick(5000);
  out.advanced = music.view().progress.elapsed;
  music.select('study_amp');
  const paused = music.view().progress.elapsed;
  await time.tick(5000);
  out.paused = {view: summary(music.view()), before: paused, after: music.view().progress.elapsed};
  // Pressed, not yet read back: Play waits for the speaker, Pause stops where it was pressed.
  const transport = backend.transport;
  backend.transport = () => new Promise(() => {});
  music.playPause();
  await time.tick(3000);
  out.pressed = {play: [music.view().playing, music.view().progress.elapsed]};
  music.select('kitchen_amp');
  const at = music.view().progress.elapsed;
  music.playPause();
  await time.tick(3000);
  out.pressed.pause = [music.view().playing, music.view().progress.elapsed - at];
  await time.tick(RemoteMusic.HOLD_MS);
  out.pressed.ignored = [music.view().playing, music.view().progress.elapsed - at];
  backend.transport = transport;
  music.select('patio_roam');
  out.away = summary(music.view());
  // [ and ]: the previous or next speaker in the list, round from either end.
  music.select('kitchen_amp');
  out.stepped = [1, 1, -1, -1, -1].map((direction) => { music.selectNext(direction); return music.selected(); });
  out.reads = backend.reads.map((read) => read[0]);
  music.stop();
}

async function transport(out) {
  const time = clock(), backend = mock(time), music = session(time, backend);
  music.start();
  await time.tick(100);
  let from = backend.sent.length;
  music.playPause();
  out.held = {playing: music.view().playing, label: music.view().playLabel, sent: sentSince(backend, from)};
  await time.tick(100);
  out.read = {playing: music.view().playing, state: music.view().state, announced: music.log.announced.slice()};
  music.playPause();
  await time.tick(100);
  from = backend.sent.length;
  music.toggleShuffle();
  out.shuffle = {held: [music.view().shuffle.label, music.view().shuffle.pending]};
  await time.tick(100);
  out.shuffle.read = [music.view().shuffle.label, music.view().shuffle.pending];
  music.cycleRepeat();
  await time.tick(100);
  music.cycleRepeat();
  await time.tick(100);
  out.repeat = music.view().repeat.label;
  music.next();
  await time.tick(100);
  out.next = music.view().title;
  music.seek(30);
  out.seek = {held: [music.view().progress.elapsed, music.view().progress.pending]};
  await time.tick(100);
  out.seek.read = [music.view().progress.elapsed, music.view().progress.pending];
  music.seekBy(1);
  await time.tick(100);
  out.seek.by = music.view().progress.elapsed;
  out.sent = sentSince(backend, from);
  // A slide: one level at once, then the last, and no more.
  from = backend.sent.length;
  for (const level of [33, 34, 35, 36]) music.setVolume(level);
  out.slide = {shown: [music.view().volume.value, music.view().volume.pending], first: sentSince(backend, from)};
  await time.tick(RemoteMusic.SLIDE_EVERY);
  out.slide.sent = sentSince(backend, from);
  await time.tick(RemoteMusic.SLIDE_EVERY * 2);
  out.slide.after = sentSince(backend, from);
  out.slide.read = [music.view().volume.value, music.view().volume.pending];
  music.stepVolume(1);
  await time.tick(RemoteMusic.SLIDE_EVERY);
  out.step = {volume: music.view().volume.value, said: music.log.announced[music.log.announced.length - 1]};
  // Someone else moves it: the page follows.
  backend.simulateVolume('kitchen_amp', 12);
  await time.tick(10);
  out.elsewhere = music.view().volume.value;
  // A slide's first level goes whatever went before: here the speaker's
  // own app moved it while the page slept, and the page woke to a snapshot.
  music.setVolume(40);
  await time.tick(RemoteMusic.SLIDE_EVERY + 100);
  backend.players.kitchen_amp.volume = 38;
  backend.simulateDrop();
  await time.tick(100);
  from = backend.sent.length;
  out.wake = {woke: music.view().volume.value};
  music.stepVolume(1);
  out.wake.sent = sentSince(backend, from);
  await time.tick(RemoteMusic.SLIDE_EVERY + 100);
  out.wake.read = [music.view().volume.value, music.view().volume.pending, sentSince(backend, from).length];
  // A command that reaches no reader: what it held ends after HOLD_MS.
  backend.answer = () => new Promise(() => {});
  music.toggleShuffle();
  out.deaf = [music.view().shuffle.label, music.view().shuffle.pending];
  await time.tick(RemoteMusic.HOLD_MS + 100);
  out.deaf.push(music.view().shuffle.label, music.view().shuffle.pending);
  out.toasts = music.log.toasts;
  music.stop();
}

async function library(out) {
  const time = clock(), backend = mock(time), music = session(time, backend);
  music.start();
  await time.tick(100);
  out.tab = music.tab();
  // Typing: one search once the typing stops.
  for (const text of ['h', 'ha', 'harbour']) { music.setQuery(text); await time.tick(100); }
  out.typing = music.library().search.state;
  await time.tick(RemoteMusic.SEARCH_MS + 100);
  const search = music.library().search;
  out.searches = backend.reads.filter((read) => read[0] === 'search');
  out.sections = search.sections.map((section) => [section.title, section.total, section.more, names(section.items)]);
  out.sources = search.sources.map((source) => source.label);
  out.kinds = search.kinds.map((kind) => [kind.label, kind.current]);
  out.track = search.sections[0].items[0];
  music.setKind('album');
  await time.tick(100);
  out.albums = music.library().search.sections.map((section) => [section.title, names(section.items)]);
  music.setKind('');
  music.setQuery('zzz');
  music.searchNow();
  await time.tick(100);
  out.nothing = music.library().search.text;
  music.setQuery('');
  out.cleared = [music.library().search.state, music.library().search.sections.length];
  // Browse: the roots, a folder at a time, and back.
  music.setTab('browse');
  await time.tick(100);
  const browse = () => music.library().browse;
  out.roots = [browse().path, names(browse().items), browse().items.map((item) => [item.source, item.line])];
  music.open(browse().items[0]); await time.tick(100);
  out.artists = names(browse().items);
  music.open(browse().items.find((item) => item.name === 'The Lanterns')); await time.tick(100);
  music.open(browse().items[0]); await time.tick(100);
  out.album = {path: browse().path, depth: browse().depth, tracks: names(browse().items), first: browse().items[0]};
  const reads = backend.reads.length;
  music.back();
  out.back = {path: browse().path, items: names(browse().items), read: backend.reads.length - reads};
  out.actions = {
    folder: music.actions(browse().items[0]).map((action) => action.label),
    track: music.actions(out.album.first).map((action) => action.label),
  };
  // Play a track next, an album to the end, a playlist now.
  let from = backend.sent.length;
  music.act(out.album.first, 'next');
  music.play(out.track, 'add');
  music.setTab('search');
  music.setQuery('kitchen');
  music.searchNow();
  await time.tick(100);
  const playlist = music.library().search.sections.find((section) => section.kind === 'playlist').items[0];
  out.playlist = [playlist.name, playlist.source, music.actions(playlist).map((action) => action.label)];
  music.play(playlist, 'replace');
  await time.tick(100);
  out.played = {sent: sentSince(backend, from), toasts: music.log.toasts.slice(), said: music.log.announced[music.log.announced.length - 1]};
  // The queue: what it holds, and read again after a track begins.
  music.setTab('queue');
  await time.tick(100);
  const queue = () => music.library().queue;
  out.queue = {items: queue().items.map((item) => [item.name, item.current]), total: queue().total, text: queue().text};
  from = backend.reads.length;
  backend.simulateNext('kitchen_amp');
  await time.tick(600);
  out.queue.reread = backend.reads.slice(from).map((read) => read.slice(0, 2));
  out.queue.current = queue().items.filter((item) => item.current).map((item) => item.name);
  music.select('patio_roam');
  out.queue.away = queue().text;
  music.stop();
}

async function grouping(out) {
  const time = clock(), backend = mock(time), music = session(time, backend);
  music.start();
  await time.tick(100);
  const before = music.grouping();
  out.candidates = before.candidates.map((candidate) => [candidate.id, candidate.name, candidate.note]);
  const from = backend.sent.length;
  music.join(['den_one', 'study_amp', 'patio_roam']);
  out.pending = music.grouping().pending;
  await time.tick(100);
  out.result = music.grouping().result;
  out.toast = music.log.toasts[music.log.toasts.length - 1];
  out.blocks = blocks(music);
  music.select('den_one');
  out.member = summary(music.view());
  out.memberGrouping = {leader: music.grouping().leaderName, members: music.grouping().members.map((member) => [member.name, member.leader])};
  music.play({ref: 'library://track/101', kind: 'track', name: 'Low Tide'}, 'add');
  await time.tick(100);
  out.memberPlay = [backend.sent[backend.sent.length - 1], music.log.toasts[music.log.toasts.length - 1]];
  music.setGroupVolume(50);
  await time.tick(RemoteMusic.SLIDE_EVERY);
  out.groupVolume = [music.view().group.volume.value, music.view().volume.value];
  music.stepGroupVolume(-1);
  await time.tick(RemoteMusic.SLIDE_EVERY);
  out.groupStep = music.view().group.volume.value;
  // A member out; then the group ended by its leader.
  music.leave('den_one');
  await time.tick(100);
  out.left = {den: summary(music.view()).group, blocks: blocks(music).map((block) => block.group)};
  music.select('kitchen_amp');
  music.join(['kitchen_mini']);
  await time.tick(100);
  out.again = music.log.toasts[music.log.toasts.length - 1];
  music.leave('kitchen_amp');
  await time.tick(100);
  out.ended = [summary(music.view()).group, music.log.announced[music.log.announced.length - 1]];
  out.sent = sentSince(backend, from);
  music.stop();
}

async function locks(out) {
  // Music Assistant's gateway refuses: the page drops what it held and says why.
  let time = clock(), backend = mock(time), music = session(time, backend);
  music.start();
  await time.tick(100);
  backend.simulateLock(['kitchen_amp']);
  music.playPause();
  out.held = music.view().playing;
  await time.tick(100);
  out.refused = {playing: music.view().playing, toast: music.log.toasts[music.log.toasts.length - 1]};
  music.setVolume(50);
  await time.tick(RemoteMusic.SLIDE_EVERY + 100);
  out.volume = [music.view().volume.value, music.view().volume.pending, music.log.toasts[music.log.toasts.length - 1]];
  music.stop();
  // A room's lock the page knows of: nothing is sent.
  time = clock();
  backend = mock(time);
  music = session(time, backend, {access: fakeAccess({locked: ['kitchen']})});
  music.start();
  await time.tick(100);
  out.view = summary(music.view());
  out.rows = blocks(music);
  const from = backend.sent.length;
  music.playPause();
  music.setVolume(40);
  music.join(['den_one']);
  music.select('den_one');
  music.join(['kitchen_mini']);
  await time.tick(400);
  out.blocked = {sent: sentSince(backend, from), toasts: music.log.toasts};
  music.stop();
  // A group member's room locked: the speaker's own volume still goes, nothing that moves the group.
  time = clock();
  backend = mock(time);
  music = session(time, backend);
  music.start();
  await time.tick(100);
  music.join(['den_one']);
  await time.tick(400);
  music.stop();
  music = session(time, backend, {access: fakeAccess({locked: ['den']})});
  music.start();
  await time.tick(100);
  const view = music.view();
  out.member = {locked: view.locked, group: view.groupLocked};
  const before = backend.sent.length;
  music.stepVolume(1);
  music.playPause();
  music.stepGroupVolume(1);
  await time.tick(400);
  out.member.sent = sentSince(backend, before);
  out.member.toasts = music.log.toasts;
  music.select('den_one');
  out.member.den = [music.view().locked.text, music.view().groupLocked.text];
  music.stop();
}

// Grouping involves each speaker whose group changes: a joining speaker's
// own group too. A lock on any of them stops it, named once the page knows.
async function involved(out) {
  const time = clock(), backend = mock(time), access = fakeAccess(), music = session(time, backend, {access});
  music.start();
  await time.tick(100);
  music.select('kitchen_mini');
  music.join(['den_one']);
  await time.tick(100);
  out.formed = blocks(music).find((block) => block.group).speakers.map((row) => row[0]);
  music.select('kitchen_amp');
  access.locked.push('den');
  let from = backend.sent.length;
  music.join(['kitchen_mini']);
  await time.tick(100);
  out.blocked = {sent: sentSince(backend, from), toast: music.log.toasts[music.log.toasts.length - 1]};
  // The gateway refuses first; the lock read again names the room.
  access.locked.splice(0);
  backend.simulateLock(['den_one']);
  from = backend.sent.length;
  music.join(['kitchen_mini']);
  access.locked.push('den');
  await time.tick(100);
  out.refused = {sent: sentSince(backend, from), toast: music.log.toasts[music.log.toasts.length - 1], result: music.grouping().result};
  // A member leaving changes its leader's group: a lock there stops it.
  backend.simulateLock([]);
  access.locked.splice(0, 1, 'kitchen');
  music.select('den_one');
  from = backend.sent.length;
  music.leave('den_one');
  await time.tick(100);
  out.leave = {sent: sentSince(backend, from), toast: music.log.toasts[music.log.toasts.length - 1]};
  music.stop();
}

async function guests(out) {
  // A Guest without free text: no titles from the gateway, no library.
  let time = clock(), backend = mock(time, {guest: true}), music = session(time, backend);
  music.start();
  await time.tick(100);
  out.view = summary(music.view());
  out.rows = blocks(music);
  out.library = music.library();
  music.setTab('search');
  music.setQuery('harbour');
  await time.tick(RemoteMusic.SEARCH_MS + 100);
  out.library = music.library();
  out.searched = backend.reads.filter((read) => read[0] === 'search').length;
  music.stop();
  // Access says so before the gateway does.
  time = clock();
  backend = mock(time);
  music = session(time, backend, {access: fakeAccess({guest: true, guestText: false})});
  music.start();
  await time.tick(100);
  music.setTab('browse');
  await time.tick(100);
  out.told = [music.library().blind, backend.reads.filter((read) => read[0] === 'browse').length];
  music.stop();
}

// Left and opened again while the library read, or a Guest who signs in:
// what it had not finished is read again.
async function resuming(out) {
  let time = clock(), backend = mock(time), music = session(time, backend);
  const searches = () => backend.reads.filter((read) => read[0] === 'search').map((read) => read[1]);
  const shown = () => music.library().search.sections.map((section) => [section.title, names(section.items)]);
  music.start();
  await time.tick(100);
  music.setTab('search');
  // Typing still to settle, and a search on its way, when the page sleeps.
  music.setQuery('harbour');
  await time.tick(100);
  music.stop();
  music.start();
  await time.tick(200);
  out.debounced = [searches(), music.library().search.state, shown().length > 0];
  music.setQuery('lantern');
  music.searchNow();
  await time.tick(10);
  music.stop();
  music.start();
  await time.tick(200);
  out.searching = [searches(), music.library().search.state, shown()];
  // An answer to an earlier query never shows under the next.
  music.setQuery('');
  music.setQuery('harbour');
  music.searchNow();
  await time.tick(10);
  music.setQuery('signal');
  await time.tick(100);
  out.late = [music.library().search.state, shown()];
  await time.tick(RemoteMusic.SEARCH_MS + 100);
  out.late.push(searches().slice(-2), music.library().search.state, shown().length > 0);
  // A folder on its way.
  music.setTab('browse');
  await time.tick(100);
  music.open(music.library().browse.items[0]);
  await time.tick(10);
  out.folder = [music.library().browse.state];
  music.stop();
  music.start();
  await time.tick(200);
  const browse = music.library().browse;
  out.folder.push(browse.path, browse.state, browse.items.length > 0);
  music.stop();
  // A Guest refused, who signs in: the next visit, and access saying so, read again.
  time = clock();
  backend = mock(time, {guest: true});
  let access = fakeAccess({guest: true});
  music = session(time, backend, {access});
  music.start();
  await time.tick(100);
  music.setTab('search');
  music.setQuery('harbour');
  await time.tick(RemoteMusic.SEARCH_MS + 100);
  out.guest = [music.library().blind];
  backend.guest = false;
  access.guest = false;
  music.stop();
  music.start();
  await time.tick(200);
  out.guest.push(music.library().blind, music.library().search.state);
  music.stop();
  time = clock();
  backend = mock(time, {guest: true});
  access = fakeAccess({guest: true, guestText: false});
  music = session(time, backend, {access});
  music.start();
  await time.tick(100);
  music.setTab('browse');
  await time.tick(100);
  out.told = [music.library().blind, backend.reads.filter((read) => read[0] === 'browse').length];
  backend.guest = false;
  access.guest = false;
  access.tell('change');
  await time.tick(100);
  out.told.push(music.library().blind, backend.reads.filter((read) => read[0] === 'browse').length, music.library().browse.state);
  music.stop();
}

async function absent(out) {
  let time = clock(), backend = mock(time, {configured: false}), music = session(time, backend);
  music.start();
  await time.tick(100);
  out.unconfigured = {status: music.status(), view: music.view(), library: music.library(), speakers: music.speakers()};
  music.stop();
  time = clock();
  const native = new MusicBackend.GatewayMusic({access: {native: true}, setTimeout: time.setTimeout, clearTimeout: time.clearTimeout});
  music = session(time, native);
  music.start();
  await time.tick(10);
  out.native = music.status();
  music.stop();
}

async function unavailable(out) {
  const time = clock(), backend = mock(time), music = session(time, backend);
  music.start();
  await time.tick(100);
  backend.simulateUnavailable('music_unclaimed');
  await time.tick(100);
  out.gone = {status: music.status(), view: music.view(), lines: music.speakers().map((block) => block.line), library: music.library()};
  backend.recover();
  await time.tick(10);
  out.back = {status: music.status(), title: music.view().title};
  backend.simulateDrop();
  await time.tick(10);
  out.dropped = music.status();
  await time.tick(100);
  out.resynced = {status: music.status(), connected: music.view().connected};
  // The stop leaves the stream: nothing more is heard.
  music.stop();
  const changes = music.log.changes;
  backend.simulateNext('kitchen_amp');
  await time.tick(100);
  out.stopped = music.log.changes - changes;
  // Started again, it reads afresh.
  music.start();
  await time.tick(100);
  out.restarted = [music.status().state, music.view().title];
  music.stop();
}

async function tidal(out) {
  let time = clock(), backend = mock(time), music = session(time, backend);
  music.start();
  await time.tick(100);
  out.before = music.tidal();
  out.checked = backend.reads.filter((read) => read[0] === 'tidal').length;
  music.tidalStart();
  out.starting = music.tidal().pending;
  await time.tick(100);
  out.waiting = music.tidal();
  out.said = music.log.announced[music.log.announced.length - 1];
  await time.tick(RemoteMusic.TIDAL_POLL_MS + 100);
  out.still = music.tidal() && music.tidal().state;
  await time.tick(RemoteMusic.TIDAL_POLL_MS + 300);
  out.done = {tidal: music.tidal(), toast: music.log.toasts[music.log.toasts.length - 1],
              sent: backend.sent.filter((entry) => entry[0] === 'tidal')};
  music.setQuery('juniper');
  music.searchNow();
  await time.tick(100);
  out.search = {sources: music.library().search.sources.map((source) => source.label),
                found: music.library().search.sections.map((section) => [section.title, names(section.items), section.items[0].source])};
  music.setTab('browse');
  await time.tick(100);
  out.roots = names(music.library().browse.items);
  music.stop();
  // Cancelled half way.
  time = clock();
  backend = mock(time);
  music = session(time, backend);
  music.start();
  await time.tick(100);
  music.tidalStart();
  await time.tick(100);
  music.tidalCancel();
  await time.tick(100);
  out.cancelled = {tidal: music.tidal(), sent: backend.sent.filter((entry) => entry[0] === 'tidal')};
  music.stop();
  // Not an Admin: no sign-in, and nothing asked.
  time = clock();
  backend = mock(time, {admin: false});
  music = session(time, backend);
  music.start();
  await time.tick(100);
  out.member = [music.tidal(), backend.reads.filter((read) => read[0] === 'tidal').length];
  music.stop();
}

// A fake EventSource the harness feeds frames; readyState 2 is closed for good.
class Source {
  constructor(url) { this.url = url; this.readyState = 0; Source.made.push(this); }
  close() { this.closed = true; this.readyState = 2; }
  send(frame) { this.onmessage({data: typeof frame === 'string' ? frame : JSON.stringify(frame)}); }
  fail(closed) { this.readyState = closed ? 2 : 0; this.onerror(); }
}
Source.made = [];

async function gateway(out) {
  const time = clock();
  const kitchen = {id: 'kitchen_amp', label: 'Sonos Amp', room: 'kitchen', room_label: 'Kitchen', provider: 'sonos', available: true,
    state: 'playing', volume: 20, muted: false, group: null, shuffle: false, repeat: 'off',
    now_playing: {duration: 200, elapsed: 10, at: START / 1000, title: 'Low Tide', artist: 'The Lanterns', album: 'Harbour Lights', art: 'a'.repeat(64)}};
  const overview = {version: 1, available: true, problem: null, sources: {tidal: false, libraries: {NAS: true}}, speakers: [kitchen]};
  const folder = (index) => ({ref: 'filesystem_local--nas://folder/' + index, kind: 'folder', name: 'Folder ' + index, source: 'library', library: 'NAS'});
  const asked = [];
  // The first read times out; the next, after the stream says Music Assistant is away, says why.
  const away = {...overview, available: false, problem: 'music_failed', sources: null,
    speakers: [{...kitchen, available: false, state: 'unavailable', volume: null, muted: null, now_playing: null, shuffle: null, repeat: null}]};
  const overviews = [ThermiteAccess.problem('client_timeout'), away];
  const access = {...fakeAccess(),
    musicRead: (target, ms) => {
      asked.push(ms ? ['GET', target, ms] : ['GET', target]);
      if (target === '' && overviews.length) {
        const answer = overviews.shift();
        return answer instanceof Error ? Promise.reject(answer) : Promise.resolve(answer);
      }
      if (target === '') return Promise.resolve(overview);
      if (target.startsWith('/browse')) {
        const offset = Number(new URLSearchParams(target.split('?')[1]).get('offset'));
        return Promise.resolve({ref: null, offset, limit: 100, total: 150,
          items: Array.from({length: Math.min(100, 150 - offset)}, (_, index) => folder(offset + index))});
      }
      if (target.startsWith('/search')) return Promise.reject(ThermiteAccess.problem('login_required', 401));
      return Promise.resolve({});
    },
    musicCommand: (target, value) => {
      asked.push(['POST', target, value]);
      if (target.endsWith('/group')) return Promise.resolve({accepted: true, speaker: 'kitchen_amp', joined: [], refused: value.members});
      return Promise.resolve({accepted: true});
    }};
  Source.made = [];
  const backend = MusicBackend.create({backend: 'gateway', access, base: 'https://site.example', EventSource: Source,
    setTimeout: time.setTimeout, clearTimeout: time.clearTimeout});
  const music = session(time, backend, {access});
  music.start();
  await time.tick(10);
  // The read failed: the stream's snapshot is the overview it did not bring.
  out.failed = music.status();
  out.stream = Source.made.map((source) => source.url);
  Source.made[0].send({type: 'unavailable'});
  await time.tick(10);
  out.away = [music.status(), music.speakers().map((block) => block.line)];
  Source.made[0].send({type: 'snapshot', view: 'music', music: overview});
  await time.tick(10);
  out.ready = [music.status().state, music.view().title, music.view().connected];
  out.art = [music.art(music.view().art, 80), music.art('../etc', 80), music.art(null, 80)];
  Source.made[0].send({type: 'speaker', speaker: {...kitchen, volume: 44}});
  Source.made[0].send('{broken');
  Source.made[0].send({type: 'speaker', speaker: {id: 7}});
  await time.tick(10);
  out.speakerFrame = music.view().volume.value;
  music.playPause();
  music.seek(42);
  music.setVolume(30);
  music.toggleShuffle();
  music.cycleRepeat();
  await time.tick(RemoteMusic.SLIDE_EVERY);
  music.join(['den_one']);
  music.play({ref: 'library://track/1', kind: 'track', name: 'Low Tide'}, 'next');
  await time.tick(10);
  out.played = music.log.toasts.slice();
  // Paging, a folder at a time.
  music.setTab('browse');
  await time.tick(10);
  out.page = [music.library().browse.items.length, music.library().browse.more];
  music.more();
  await time.tick(10);
  out.paged = [music.library().browse.items.length, music.library().browse.more];
  // A Guest refused free text: the library is closed.
  music.setTab('search');
  music.setQuery('low tide');
  music.searchNow();
  await time.tick(10);
  out.guest = music.library().blind;
  out.asked = asked.slice();
  // The stream: closed for good, it opens again after REOPEN_MS[0].
  Source.made[0].fail(false);
  out.blip = [music.status().text, Source.made.length];
  Source.made[0].fail(true);
  await time.tick(MusicBackend.REOPEN_MS[0] - 1);
  out.waited = Source.made.length;
  await time.tick(2);
  out.reopened = Source.made.length;
  Source.made[1].send({type: 'unavailable'});
  await time.tick(10);
  out.unavailable = music.status();
  backend.setActive(false);
  out.hidden = [Source.made[1].closed, Source.made.length];
  backend.setActive(true);
  out.shown = Source.made.length;
  music.stop();
  out.stopped = Source.made[2].closed;
}

// access.js's music calls on a fake fetch: the paths, the CSRF of a command, the problems.
async function client(out) {
  const origin = 'https://site.example:13601', csrf = 'C'.repeat(32), csrfName = 'thermite_0123456789abcdef_csrf';
  const guest = {id: 0, username: 'Guest', admin: false, can_edit: false, is_guest: true, auth_enabled: true, permissions: ['site.read']};
  const response = (value, status = 200, cookies = []) => ({ok: status >= 200 && status < 300, status, json: async () => value,
    clone() { return this; }, headers: {getSetCookie: () => cookies}});
  const calls = [];
  const access = ThermiteAccess.create({base: origin, getToken: () => null, fetch: async (url, init) => {
    const address = new URL(url);
    calls.push([init && init.method || 'GET', address.pathname + address.search, init && init.body ? JSON.parse(init.body) : null,
                init && init.headers && typeof init.headers.get === 'function' ? init.headers.get('X-CSRFToken') : null]);
    if (address.pathname === '/api/v1/security') {
      return response({enabled: true, revision: 1, bootstrap_required: false, setup_available: false, guest_text: false, csrf},
                      200, [csrfName + '=' + csrf + '; Path=/; Secure']);
    }
    if (address.pathname === '/api/v1/session') return response({authenticated: false, csrf});
    if (address.pathname === '/api/v1/me') return response(guest);
    if (address.pathname === '/api/v1/music/search') return response({error: 'login_required'}, 401);
    if (address.pathname === '/api/v1/music/kitchen_amp/transport') return response({error: 'device_locked'}, 423);
    if (address.pathname === '/api/v1/music/den_one/volume') return response({error: 'music_unclaimed'}, 503);
    if (address.pathname === '/api/v1/music') return response({version: 1, available: false, problem: 'music_unavailable', sources: null, speakers: []});
    throw new Error('Unexpected route: ' + address.pathname);
  }});
  out.overview = await access.musicRead('');
  const problem = (error) => [error.code, error.status, error.message];
  out.search = await access.musicRead('/search?q=low+tide').then(() => null, problem);
  out.locked = await access.musicCommand('kitchen_amp/transport', {action: 'pause'}).then(() => null, problem);
  out.unclaimed = await access.musicCommand('den_one/volume', {level: 3}).then(() => null, problem);
  out.calls = calls.filter((call) => call[1].startsWith('/api/v1/music'));
  out.messages = ['music_not_configured', 'music_unavailable', 'music_unclaimed', 'music_failed', 'device_locked', 'tidal_not_started',
                  'invalid_ref', 'unknown_speaker'].map((code) => [code, ThermiteAccess.problem(code).code]);
  access.stop();
}

(async () => {
  const report = {};
  for (const [name, run] of Object.entries({overview, transport, library, resuming, grouping, locks, involved, guests, absent, unavailable, tidal, gateway, client})) {
    report[name] = {};
    await run(report[name]);
  }
  process.stdout.write(JSON.stringify(report));
  process.exit(0);
})().catch((error) => { process.stderr.write(error.stack + '\n'); process.exit(1); });
