'use strict';
// stdin: a generated house model (remote_model.build) with argv's two audio
// devices: the first owned by a room argv[3] locks, the second by none;
// argv[4], a file of a house model with a Sonos Arc; argv[5], one whose
// routed room shows a source of picture alone. Runs
// the Audio page's session (audio-session.js) on the mock backend, and the
// openHAB backend's command() and audio Items on a fake fetch and stream;
// prints what each showed and sent. Nothing leaves the process.
const fs = require('fs'), path = require('path'), vm = require('vm');
const WEB = path.join(__dirname, '..', 'web', 'remote');
console.info = console.warn = () => {};
for (const name of ['navigation.js', 'dom.js', 'sse.js', 'hold.js', 'remotes.js', 'backend.js', 'backend-mock.js', 'audio-session.js']) {
  vm.runInThisContext(fs.readFileSync(path.join(WEB, name), 'utf8'), {filename: name});
}
const house = JSON.parse(fs.readFileSync(0, 'utf8'));
const [owned, free, lockedRoom, barFile, routeFile] = process.argv.slice(2);
const {RemoteBackend, RemoteAudio} = globalThis;
const settle = (ms = 20) => new Promise((resolve) => setTimeout(resolve, ms));
const sectionsOf = (view) => view.sections.map((section) => [section.id, section.settings.map((s) => [s.item, s.kind, s.text, s.pending])]);
const settingOf = (view, item) => view.sections.flatMap((section) => section.settings).find((s) => s.item === item) || null;
const page = house.devices[free].audio;
const item = (suffix) => free + '_' + suffix;

// A backend whose commands are recorded and, when `fail`, refused; with
// `deaf` the device never reads one back.
function recording(backend, options = {}) {
  const command = backend.command.bind(backend);
  backend.sent = [];
  backend.command = (device, action, value) => {
    backend.sent.push([device, action, String(value)]);
    if (options.fail) return Promise.reject(new Error(options.fail));
    if (options.deaf) return Promise.resolve({device, action, sent: true, tag: ''});
    return command(device, action, value);
  };
  return backend;
}

async function mock() {
  const backend = recording(RemoteBackend.create(house, {backend: 'mock', latency: 8}));
  const session = RemoteAudio.create({backend, house});
  const events = [];
  session.subscribe((event) => events.push(event.type));
  await session.start(free);
  const out = {devices: session.devices(), first: session.view()};
  await session.set(item('eq_preset'), page.settings.find((s) => s.item === item('eq_preset')).options[3][0]);
  out.presetHeld = settingOf(session.view(), item('eq_preset'));
  await settle();
  out.presetRead = settingOf(session.view(), item('eq_preset'));
  await session.toggle(item('eq'));
  await session.step(item('sub_level'), -1);
  await settle();
  out.stepped = [settingOf(session.view(), item('eq')).text, settingOf(session.view(), item('sub_level')).text];
  await session.set(item('sub_level'), -15);
  await settle();
  const before = backend.sent.length;
  await session.step(item('sub_level'), -1);
  out.atMin = {setting: settingOf(session.view(), item('sub_level')), sentMore: backend.sent.length - before};
  await session.press('play_pause');
  out.paused = {playing: session.view().playing, transport: session.view().transport.map((button) => button.label)};
  await settle();
  out.pausedRead = session.view().playing;
  await session.chooseInput('bluetooth');
  await settle();
  out.bluetooth = {input: session.view().input.label, sections: session.view().sections.map((section) => section.id)};
  const volume = session.view().volume.value;
  const sliding = backend.sent.length;
  for (const value of [volume + 1, volume + 2, volume + 3, volume + 4]) session.setVolume(value);
  out.sliding = {shown: session.view().volume.value, pending: session.view().volume.pending,
                 sent: backend.sent.slice(sliding).map((entry) => entry[2])};
  await settle(RemoteAudio.SLIDE_EVERY + 40);
  out.slid = backend.sent.slice(sliding).map((entry) => entry[2]);
  session.select(owned);
  out.ownedView = {name: session.view().device.name, locked: session.view().locked};
  // Left mid-slide, with a draw due, and opened again.
  session.stepVolume(1);
  session.stop();
  events.length = 0;
  // What was in flight lands while it is stopped, and draws nothing; were
  // it still in flight, it would race the reopened session's first draw.
  await settle();
  const stopped = events.length;
  await session.start();
  backend.updateAudio(owned, owned + '_title', 'Another Track');
  await settle();
  const resent = backend.sent.length;
  session.stepVolume(1);
  out.restarted = {stopped, selected: session.selected(), changes: events.filter((type) => type === 'change').length,
                   title: session.view().track.title, sent: backend.sent.length - resent};
  session.stop();
  out.sent = backend.sent;
  return out;
}

// A room that owns the device and is locked refuses it here before the gateway does.
async function locked() {
  const backend = recording(RemoteBackend.create(house, {backend: 'mock', latency: 8}));
  let watching = null;
  // Access knows whether accounts are on a moment after the page starts.
  const access = {native: false, mock: false, subscribe: () => () => {}, watchLocks: (on) => { watching = on; },
                  ready: () => new Promise((resolve) => setTimeout(resolve, 5)),
                  lockSummary: (room) => ({locked: room === lockedRoom})};
  const early = RemoteAudio.create({backend, house, access});
  early.start(owned);
  early.stop();
  await settle();
  const watchedAfterEarlyStop = watching;
  const session = RemoteAudio.create({backend, house, access});
  const toasts = [];
  session.subscribe((event) => { if (event.type === 'toast') toasts.push(event); });
  const starting = session.start(owned);
  const watchingAtStart = watching;
  await starting;
  await settle();
  const out = {watchedAfterEarlyStop, watchingAtStart, watching, view: session.view().locked,
               list: session.devices().flatMap((place) => place.devices.map((d) => [d.id, d.locked]))};
  await session.press('play_pause');
  await session.toggleMute();
  session.stepVolume(1);
  out.refused = {toasts: toasts.map((toast) => [toast.body, toast.tone]), sent: backend.sent.length};
  session.select(free);
  await session.toggle(item('light'));
  out.free = {locked: session.view().locked, sent: backend.sent.map((entry) => entry[1])};
  session.stop();
  out.unwatched = watching;
  return out;
}

// A refusal drops what the page held; a value never read back shows what
// was read once its hold ends.
async function unheard() {
  let clock = 1000;
  const failing = recording(RemoteBackend.create(house, {backend: 'mock', latency: 8}), {fail: 'Refused by the gateway'});
  const session = RemoteAudio.create({backend: failing, house, now: () => clock});
  const toasts = [];
  session.subscribe((event) => { if (event.type === 'toast') toasts.push([event.title, event.body, event.tone]); });
  await session.start(free);
  await session.toggle(item('light'));
  const out = {refused: {toasts, light: settingOf(session.view(), item('light'))}};
  session.stop();
  const deaf = recording(RemoteBackend.create(house, {backend: 'mock', latency: 8}), {deaf: true});
  const quiet = RemoteAudio.create({backend: deaf, house, now: () => clock});
  await quiet.start(free);
  await quiet.toggleMute();
  out.held = quiet.view().muted;
  clock += RemoteBackend.heldMs(RemoteBackend.controlOf(house, free), RemoteAudio.HOLD_MS);
  out.expired = quiet.view().muted;
  quiet.stop();
  return out;
}

// What the review of the page found, each as it now behaves.
async function review() {
  const out = {};
  // A volume the device's own knob moved off can be sent again.
  const backend = recording(RemoteBackend.create(house, {backend: 'mock', latency: 8}));
  const announced = [];
  const session = RemoteAudio.create({backend, house});
  session.subscribe((event) => { if (event.type === 'announce') announced.push(event.message); });
  await session.start(free);
  const volume = session.view().volume.value;
  session.stepVolume(1);
  await settle(RemoteAudio.SLIDE_EVERY + 40);
  backend.updateDevice(free, 'volume', String(volume + 2));
  await settle();
  session.stepVolume(-1);
  await settle(RemoteAudio.SLIDE_EVERY + 40);
  out.resent = backend.sent.filter((entry) => entry[1] === 'volume').map((entry) => entry[2]);
  out.announced = announced.filter((message) => message.includes('Volume'));
  // Two quick presses: the second undoes the first.
  await session.press('play_pause');
  await session.press('play_pause');
  out.twice = session.view().playing;
  // Another device chosen mid-slide: the first still gets its last volume.
  const sliding = backend.sent.length;
  session.setVolume(30);
  session.setVolume(35);
  session.select(owned);
  session.setVolume(20);
  await settle(RemoteAudio.SLIDE_EVERY + 40);
  out.switched = backend.sent.slice(sliding).filter((entry) => entry[1] === 'volume');
  // A title the device does not know is none; the page says it plays.
  session.select(free);
  out.headline = session.view().now;
  for (const field of ['title', 'artist', 'album', 'service']) backend.updateAudio(free, item(field), 'Unknown');
  await settle();
  out.unknown = {track: session.view().track, now: session.view().now,
                 title: session.devices().flatMap((place) => place.devices).find((d) => d.id === free).title};
  await session.press('play_pause');
  await settle();
  out.paused = session.view().now;
  session.stop();
  // A refused play/pause shows what was read; a stopped session leaves no timer.
  const timers = new Set();
  const refusing = recording(RemoteBackend.create(house, {backend: 'mock', latency: 8}), {fail: 'Refused by the gateway'});
  const counted = RemoteAudio.create({backend: refusing, house,
    setTimeout: (fn, ms) => { const handle = setTimeout(() => { timers.delete(handle); fn(); }, ms); timers.add(handle); return handle; },
    clearTimeout: (handle) => { timers.delete(handle); clearTimeout(handle); }});
  await counted.start(free);
  await counted.press('play_pause');
  out.refusedPress = {playing: counted.view().playing, label: counted.view().transport.find((button) => button.id === 'play_pause').label};
  await counted.toggleMute();
  out.timersBeforeStop = timers.size;
  counted.stop();
  out.timersAfterStop = timers.size;
  return out;
}

// A session stopped and started again, as the terminal's Audio screen is
// each time it is left and opened.
async function restart() {
  const out = {};
  const backend = recording(RemoteBackend.create(house, {backend: 'mock', latency: 8}));
  const session = RemoteAudio.create({backend, house});
  await session.start(free);
  const volume = session.view().volume.value;
  // Left mid-slide: the last step still reaches the device.
  session.stepVolume(1);
  session.stepVolume(1);
  session.stop();
  out.leftMidSlide = backend.sent.map((entry) => entry[2]);
  await settle();
  // Moved back while the screen was away: the same step is sent again.
  backend.updateDevice(free, 'volume', String(volume + 1));
  await session.start(free);
  const back = backend.sent.length;
  session.stepVolume(1);
  out.back = backend.sent.slice(back).map((entry) => entry[2]);
  await settle();
  // Started again while running: one set of listeners.
  const listening = backend.listeners.length;
  await session.start(free);
  out.listeners = [listening, backend.listeners.length];
  // A volume not known moves nowhere.
  backend.updateDevice(free, 'volume', 'UNDEF');
  await settle();
  const unknown = backend.sent.length;
  session.setVolume(90);
  out.unknownVolume = backend.sent.length - unknown;
  session.stop();
  // Opened while the stream is down, it says so.
  backend.connected = false;
  await session.start(free);
  out.connected = session.view().connected;
  session.stop();
  // A slow device's late echoes do not send the last volume twice.
  const slow = recording(RemoteBackend.create(house, {backend: 'mock', latency: 1200}));
  const sliding = RemoteAudio.create({backend: slow, house});
  await sliding.start(free);
  for (let step = 0; step < 6; step++) sliding.stepVolume(1);
  await settle(900);
  out.slow = slow.sent.map((entry) => entry[2]);
  sliding.stop();
  return out;
}

// The openHAB backend: a page's commands through the gateway, or natively
// to the Item; its audio Items from the snapshot and the stream.
async function openhab() {
  const posted = [], asked = [];
  const fetch = async (url, init) => {
    if (init && init.method === 'POST') posted.push([url, init.body]);
    return {ok: true, status: 200, text: async () => '', json: async () => ({})};
  };
  const gateway = {native: false, subscribe: () => () => {},
                   deviceCommand: async (device, action, value) => { asked.push([device, action, value]); return {}; }};
  const viaGateway = RemoteBackend.create(house, {backend: 'openhab', fetch, access: gateway});
  await viaGateway.command(free, item('eq_preset'), 'Rock');
  await viaGateway.command(free, 'volume', 30);
  const unknown = await viaGateway.command(free, 'reboot', 'ON').then(() => null, (error) => error.message);
  const native = RemoteBackend.create(house, {backend: 'openhab', fetch, access: {native: true}});
  await native.command(free, 'key', 'play_pause');
  await native.command(free, item('light'), 'OFF');
  const heard = [];
  native.subscribe((event) => { if (event.type === 'audio') heard.push([event.device, event.item]); });
  const unread = native.audioOf(free)[item('title')];
  native.applySnapshot({[item('title')]: 'From the snapshot'});
  native.onMessage(JSON.stringify({topic: `openhab/items/${item('artist')}/statechanged`, type: 'ItemStateChangedEvent',
                                   payload: JSON.stringify({type: 'String', value: 'From the stream'})}));
  native.onMessage(JSON.stringify({topic: `openhab/items/${item('artist')}/statechanged`, type: 'ItemStateChangedEvent',
                                   payload: JSON.stringify({type: 'String', value: 'From the stream'})}));
  return {asked, posted, unknown, watched: native.watched().filter((name) => name.startsWith(free + '_')),
          unread, heard, audio: native.audioOf(free)};
}

// An Arc on its TV input reads PLAYING whatever its TV sends: with no
// track named, its TV audio format says whether that is any sound.
async function arc() {
  const bar = JSON.parse(fs.readFileSync(barFile, 'utf8'));
  const id = Object.keys(bar.devices).find((name) => ((bar.devices[name].audio || {}).state || {}).silent);
  const at = (suffix) => id + '_' + suffix;
  const fetch = async () => ({ok: true, status: 200, text: async () => '', json: async () => ({})});
  const backend = recording(RemoteBackend.create(bar, {backend: 'openhab', fetch, access: {native: true}}));
  const session = RemoteAudio.create({backend, house: bar});
  // Read with the page's other Items, a fact or not.
  const bare = JSON.parse(JSON.stringify(bar));
  delete bare.devices[id].audio.facts;
  const watched = RemoteBackend.create(bare, {backend: 'openhab', fetch, access: {native: true}}).watched().includes(at('codec'));
  await session.start(id);
  // A snapshot replaces the last: each names its state, title and format.
  const playing = (state, title, codec) => {
    backend.applySnapshot({[at('state')]: state, [at('title')]: title, [at('codec')]: codec});
    return [session.devices().flatMap((place) => place.devices).find((device) => device.id === id).playing,
            session.view().now, session.silent(id)];
  };
  const out = {silent: bar.devices[id].audio.state.silent, watched};
  out.silence = playing('PLAYING', ' ', 'silence');
  // Nothing to play or pause: the key is off and sends nothing.
  const key = () => session.view().transport.find((button) => button.id === 'play_pause');
  await session.press('play_pause');
  out.silentKey = {key: key(), sent: backend.sent, playing: session.view().playing};
  out.noSignal = playing('PLAYING', ' ', 'noSignal');
  out.sound = playing('PLAYING', ' ', 'dolbyDigital');
  out.soundKey = key();
  out.unread = playing('PLAYING', ' ', 'NULL');
  out.track = playing('PLAYING', 'Sample Track', 'silence');
  out.stopped = playing('STOPPED', ' ', 'dolbyDigital');
  session.stop();
  return out;
}

// A room that shows a source of picture alone feeds the input its route
// drives no sound: while the room reads such a source and the device is on
// that input, what it plays with no track named is nothing.
async function route() {
  const desk = JSON.parse(fs.readFileSync(routeFile, 'utf8'));
  const id = Object.keys(desk.devices).find((name) => (((desk.devices[name].audio || {}).state || {}).route || []).length);
  const state = desk.devices[id].audio.state;
  const room = state.route[0].room;
  const backend = recording(RemoteBackend.create(desk, {backend: 'mock', latency: 4,
    initial: {[room]: 'example_computer'}, audio: {[id + '_title']: ' '}}));
  backend.devices[id].input = 'optical';
  const session = RemoteAudio.create({backend, house: desk});
  await session.start(id);
  const shown = () => [session.view().playing, session.view().now, session.silent(id)];
  const key = () => session.view().transport.find((button) => button.id === 'play_pause');
  const out = {route: state.route};
  // The room shows the computer and the amp is on the input its route feeds.
  out.quiet = shown();
  out.quietKey = key();
  // The room switching off, and back, is heard.
  await backend.choose(room, 'off');
  out.off = shown();
  await backend.choose(room, 'example_computer');
  out.again = shown();
  // On another input its sound is its own.
  await session.chooseInput('bluetooth');
  await settle(40);
  out.otherInput = shown();
  // What tells (the room's Source) not read yet: not known to be either.
  await session.chooseInput('optical');
  await settle(40);
  backend.items[room].value = 'UNDEF';
  backend.emitState(room);
  await settle();
  out.unread = shown();
  // A track is a track whatever the route carries.
  backend.items[room].value = 'example_computer';
  backend.updateAudio(id, id + '_title', 'Sample Track');
  await settle();
  out.track = shown();
  session.stop();
  return out;
}

(async () => {
  const report = {mock: await mock(), locked: await locked(), unheard: await unheard(), review: await review(),
                  restart: await restart(), openhab: await openhab(), arc: await arc(), route: await route()};
  process.stdout.write(JSON.stringify(report));
  process.exit(0);
})().catch((error) => { process.stderr.write(String(error.stack)); process.exit(1); });
