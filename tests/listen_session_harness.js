'use strict';
// stdin: a generated house model (remote_model.build) with a room of two
// speakers alone (argv[2], argv[3]) and a routed room (argv[5]) that owns a
// third (argv[4]). Runs RemoteSession, RemoteAudio, RemoteMusic and
// RemoteListen together on the mock backends, as the remote draws them;
// prints what each showed and sent. Nothing leaves the process.
const fs = require('fs'), path = require('path'), vm = require('vm');
const WEB = path.join(__dirname, '..', 'web', 'remote');
console.info = console.warn = () => {};
const house = JSON.parse(fs.readFileSync(0, 'utf8'));
globalThis.REMOTE_HOUSE = house;
for (const name of ['navigation.js', 'dom.js', 'sse.js', 'hold.js', 'remotes.js', 'backend.js', 'backend-mock.js', 'audio-session.js', 'music-backend.js', 'music-backend-mock.js', 'music-session.js',
  'listen-session.js', 'session.js']) {
  vm.runInThisContext(fs.readFileSync(path.join(WEB, name), 'utf8'), {filename: name});
}
const [mini, amp, owned, mixed] = process.argv.slice(2);
const {RemoteBackend, RemoteAudio, RemoteMusic, RemoteListen, RemoteSession, MusicBackend} = globalThis;
const settle = (ms = 30) => new Promise((resolve) => setTimeout(resolve, ms));

function recording(backend) {
  const command = backend.command.bind(backend);
  backend.sent = [];
  backend.command = (device, action, value) => {
    backend.sent.push([device, action, String(value)]);
    return command(device, action, value);
  };
  return backend;
}

// An access client whose only lock is the routed room's.
function locking(room) {
  return {native: false, mock: false, subscribe: () => () => {}, ready: () => Promise.resolve(), watchLocks: () => {},
    lockSummary: (id) => (id === room ? {locked: true, manual: true} : null), canEdit: () => false, current: () => null,
    security: () => null};
}

async function remote(options = {}) {
  const backend = recording(RemoteBackend.create(house, {backend: 'mock', latency: 4}));
  const audio = RemoteAudio.create({backend, house, watchLocks: false, access: options.access});
  // The mock plays the first of its speakers: options.playing puts that one first.
  const speakers = MusicBackend.MockMusic.speakersOf(house).filter((spec) => !(options.unknown || []).includes(spec.id))
    .map((spec) => ((options.away || []).includes(spec.id) ? {...spec, away: true} : spec))
    .sort((a, b) => (b.id === options.playing) - (a.id === options.playing));
  const musicBackend = new MusicBackend.MockMusic({speakers, latency: 4});
  if (options.problem) musicBackend.problem = options.problem;
  const music = options.music === false ? null : RemoteMusic.create({backend: musicBackend, house, watchLocks: false,
    access: options.access});
  const listen = RemoteListen.create({audio, music, house});
  const session = RemoteSession.create({backend, house, listen, store: options.store});
  const toasts = [];
  listen.subscribe((event) => { if (event.type === 'toast') toasts.push([event.title, event.body, event.tone]); });
  // Early: the remote may draw a room before either session has read its speakers.
  const starting = Promise.all([audio.start(), music ? music.start() : null]);
  if (!options.early) await starting;
  await session.start(options.room);
  await starting;
  await settle();
  return {backend, musicBackend, audio, music, listen, session, toasts};
}

async function main() {
  const out = {};
  const {backend, musicBackend, listen, session, audio, music} = await remote({room: mixed});
  out.rooms = session.rooms().map((room) => [room.id, room.label, session.modes(room), room.speakers, Boolean(room.listenOnly)]);
  out.mixedMode = session.mode();
  out.listed = session.rooms().map((room) => [room.id, session.view(room, session.state(room.id))]);
  session.setMode('listen');
  out.mixedListen = {mode: session.mode(), speakers: listen.speakers(), selected: listen.selected()};
  session.selectRoom(session.rooms().find((room) => room.listenOnly).id);
  await settle();
  out.den = {mode: session.mode(), modes: session.modes(), speakers: listen.speakers(), status: listen.status(session.current().id)};
  const face = listen.view();
  out.face = {source: face.source, now: face.now, line: face.line, transport: face.transport.map((key) => key.id),
    volume: [face.volume.min, face.volume.max, face.volume.step, face.volume.value], speaker: face.speaker.name,
    library: face.library, canGroup: face.canGroup, progress: Boolean(face.progress)};
  await listen.press('play_pause');
  await settle();
  out.paused = {music: musicBackend.sent.slice(-1), playing: listen.view().playing};
  const before = backend.sent.length;
  out.volumeKey = session.pressKey(['volume_up']);
  await settle();
  out.volumeSent = backend.sent.slice(before);
  out.digit = session.chooseDigit('2');
  await settle();
  out.chosen = {listen: listen.selected(), audio: audio.selected(), music: music.selected(), speaker: listen.view().speaker.name};
  out.badDigit = session.chooseDigit('7');
  session.selectRoom(mixed);
  out.back = {mode: session.mode(), selected: listen.selected()};
  // A room of Watch alone brings Watch; Listen from it goes back to the room last in Listen.
  session.selectRoom(session.rooms()[0].id);
  out.watchOnly = {mode: session.mode(), modes: session.modes(), listed: session.listed().map((room) => room.id)};
  out.toggled = {toggled: session.toggleMode(), room: session.current().id, mode: session.mode(),
    listed: session.listed().map((room) => room.id), view: session.view(session.room(mixed), session.state(mixed)).short};

  // Another page choosing Listen changes the next page opened, not this one under its Watch room.
  const shared = RemoteSession.memoryStore();
  const watching = await remote({room: session.rooms()[0].id, store: shared});
  shared.set('remote.mode', 'listen');
  out.otherPage = {mode: watching.session.mode(), room: watching.session.current().id,
    listed: watching.session.listed().map((room) => room.id)};
  const next = await remote({store: shared});
  out.nextPage = {mode: next.session.mode(), room: next.session.current().id};
  // A kept Listen room that has no speakers now gives way to the first that has; Listen stays.
  const stale = RemoteSession.memoryStore();
  stale.set('remote.mode', 'listen'); stale.set('remote.room.listen', session.rooms()[0].id);
  const reopened = await remote({store: stale});
  out.staleRoom = {mode: reopened.session.mode(), room: reopened.session.current().id, kept: stale.get('remote.mode')};

  // No music on the host: the speakers show and play what they report themselves.
  const plain = await remote({room: session.rooms().find((room) => room.listenOnly).id, music: false});
  const own = plain.listen.view();
  out.plain = {source: own.source, transport: own.transport.map((key) => key.id), library: own.library, canGroup: own.canGroup};
  const sent = plain.backend.sent.length;
  await plain.listen.press('play_pause');
  out.plainSent = plain.backend.sent.slice(sent);

  // A speaker the locked routed room owns is locked in Listen; one no room owns is not.
  const locked = await remote({room: mixed, access: locking(mixed)});
  locked.session.setMode('listen');
  await settle();
  out.lockedOwned = locked.listen.view().locked;
  locked.session.selectRoom(locked.session.rooms().find((room) => room.listenOnly).id);
  await settle();
  out.lockedFree = locked.listen.view().locked;
  // Locked, the device's own commands are held back too, with Remotes' muted word.
  const sent0 = locked.backend.sent.length;
  locked.session.selectRoom(mixed);
  locked.session.pressKey(['volume_up']);
  await settle();
  out.lockedVolume = {sent: locked.backend.sent.slice(sent0), toast: locked.toasts.slice(-1)[0]};

  // A speaker Music Assistant does not have, and Music Assistant away: the device's own face.
  const den = session.rooms().find((room) => room.listenOnly).id;
  const unknown = await remote({room: den, unknown: [mini]});
  const face1 = unknown.listen.view();
  out.unknownSpeaker = {speaker: face1.speaker.id, source: face1.source, library: face1.library, canGroup: face1.canGroup};
  const away = await remote({room: den, problem: 'music_unavailable'});
  const face2 = away.listen.view();
  out.away = {source: face2.source, music: face2.music, library: face2.library};
  // Speakers Music Assistant cannot reach, that report nothing themselves, are not answering.
  // Ones it does not have are still being read.
  const silent = async (options) => {
    const made = await remote({room: den, ...options});
    [mini, amp].forEach((id) => made.backend.updateAudio(id, house.devices[id].audio.state.item, 'UNDEF'));
    await settle();
    return made.session.view(made.session.room(den), made.session.state(den));
  };
  out.notAnswering = await silent({away: [mini, amp]});
  out.reading = (await silent({unknown: [mini, amp]})).short;

  // Drawn before the speakers were read, a room still opens on the one that plays.
  const early = await remote({room: den, early: true, playing: amp});
  await settle(60);
  out.early = {listen: early.listen.selected(), music: early.music.selected()};
  early.listen.choose(mini);
  await settle();
  out.earlyChosen = early.listen.selected();

  // A speaker Music Assistant plays whose own report is a silent TV input
  // (audio.silent, as an Arc's) plays nothing, in its room's line and on its
  // face; Music Assistant keeps its library.
  const tv = await remote({room: den, playing: mini});
  tv.listen.choose(mini);
  await settle();
  const heardPlaying = [tv.listen.status(den).short, tv.listen.view().playing];
  tv.audio.silent = (id) => id === mini;
  const face3 = tv.listen.view();
  out.silentTv = {before: heardPlaying, status: tv.listen.status(den), playing: face3.playing, now: face3.now === tv.audio.view().now,
    play: face3.transport.find((key) => key.id === 'play_pause').label, source: face3.source, library: Boolean(face3.library),
    progress: face3.progress};
  // Play resumes Music Assistant's queue there, where its toggle would pause.
  await tv.listen.press('play_pause');
  await settle();
  out.silentTv.pressed = tv.musicBackend.sent.slice(-1);

  out.wired = [];
  for (const music of [true, false]) {
    const wired = await remote({room: mixed, playing: owned, music});
    wired.session.setMode('listen');
    await wired.backend.choose(mixed, 'example_computer');
    wired.backend.updateAudio(owned, owned + '_status', 'play');
    wired.backend.updateAudio(owned, owned + '_title', 'Previous song');
    wired.backend.updateDevice(owned, 'input', 'optical');
    const view = wired.listen.view();
    const result = {short: wired.listen.status(mixed).short, now: view.now, playing: view.playing, title: view.title};
    const switching = wired.backend.choose(mixed, 'off');
    result.pending = wired.listen.status(mixed).short;
    await switching;
    wired.backend.updateDevice(owned, 'input', 'wifi');
    result.stream = wired.listen.status(mixed).short;
    out.wired.push(result);
  }

  // A front end without Listen lists no rooms of speakers alone.
  const plainSession = RemoteSession.create({backend: RemoteBackend.create(house, {backend: 'mock', latency: 4}), house});
  await plainSession.start();
  out.withoutListen = plainSession.rooms().map((room) => [room.id, plainSession.modes(room)]);
  process.stdout.write(JSON.stringify(out));
  process.exit(0);
}

main().catch((error) => { console.error(error && error.stack || error); process.exit(1); });
