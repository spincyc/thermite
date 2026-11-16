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
for (const name of ['navigation.js', 'remotes.js', 'backend.js', 'audio-session.js', 'music-backend.js', 'music-session.js',
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
  const audio = RemoteAudio.create({backend, house, watchLocks: false});
  const musicBackend = new MusicBackend.MockMusic({speakers: MusicBackend.speakersOf(house), latency: 4});
  const music = options.music === false ? null : RemoteMusic.create({backend: musicBackend, house, watchLocks: false,
    access: options.access});
  const listen = RemoteListen.create({audio, music, house});
  const session = RemoteSession.create({backend, house, listen});
  await Promise.all([audio.start(), music ? music.start() : null]);
  await session.start(options.room);
  await settle();
  return {backend, musicBackend, audio, music, listen, session};
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
  session.selectRoom(session.rooms()[0].id);
  out.watchOnly = {mode: session.mode(), modes: session.modes(), toggled: session.toggleMode(), face: listen.view()};

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
  process.stdout.write(JSON.stringify(out));
  process.exit(0);
}

main().catch((error) => { console.error(error && error.stack || error); process.exit(1); });
