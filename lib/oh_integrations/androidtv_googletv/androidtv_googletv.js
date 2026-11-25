// Android and Google TVs and NVIDIA SHIELDs over the androidtv binding
// (lib/oh_integrations/androidtv_googletv).
//
// DEVICES lists each device: its id (its Items are <id>_key,
// <id>_android_volume, <id>_pairing and <id>_pairing_status), the UID of its
// googletv Thing, the remote it is drawn with and its volume channel's scale
// ({min, max, step}; null for a SHIELD, whose sound is the room's). The binding reports power,
// mute and volume and switches power and mute itself, but it cannot set a
// volume, it takes key codes rather than remote button ids, and it pairs only
// from its console, so this rule does all three:
//
// - a button id sent to <id>_key is pressed as PRESSES[remote] gives it: a key
//   code on the Thing's keycode channel, or an app link on its debug channel;
// - a volume on that scale sent to <id>_android_volume is reached (any other
//   is dropped) with one VOLUME_UP or VOLUME_DOWN press per step, counted from the volume
//   last aimed at if that was under AIM_MS ago (a second command before the
//   TV reports the first steps from the first), else from the volume the TV
//   last reported;
// - REQUEST sent to <id>_pairing goes to the Thing's pincode channel, which
//   makes the device show six letters and digits, and those sent back pair
//   it. <id>_pairing_status follows the Thing: ONLINE is paired; for CODE_MS
//   after a request the code is on screen; for CHECK_MS after a code the
//   device is checking it, and if the Thing is not ONLINE by then the code
//   was refused, shown as failed for FAILED_MS; otherwise OFFLINE for want of
//   a PIN is unpaired and any other OFFLINE unreachable. The binding asks for
//   a code on the device's pairing port (the Thing's port + 1) and drops the
//   request without a word when that port does not connect, so the rule
//   tries the port first: closed, it asks nothing and shows closed for
//   FAILED_MS.
// - AGAIN sent to <id>_pairing of a paired device pairs it again. The binding
//   asks a paired device for no code and makes new keys only when its Thing
//   starts without a keystore, so the rule tries the pairing port as for
//   REQUEST, then disables the Thing, renames its keystore (the device's
//   keystore under openHAB's userdata) to the same name with PREVIOUS after
//   it, and enables the Thing. The device refuses the new keys, the binding
//   opens its pairing connection (OFFLINE for want of a PIN), and SETTLE_MS
//   later the rule asks for a code; the status is repairing until a code is
//   sent. Once the Thing is ONLINE the new keystore stands and the previous
//   one is deleted. CANCEL, no refusal within REFUSE_MS, a code refused
//   (CHECK_MS) or no code within REPAIR_MS of the last request disables the
//   Thing, moves the previous keystore back over the new one and enables it,
//   so the old pairing works again; the status is checking (failed for a
//   refusal) until it is ONLINE, for at most RESTORE_MS. A previous keystore
//   with no pairing again under way (openHAB or the rule restarted in the
//   middle) is moved back the same way at the next poll. A move back that
//   fails is logged once with both paths and shows failed; it is not retried
//   on its own, since each try restarts the Thing. CANCEL tries once more,
//   and a previous keystore moved away by hand ends it.
// - Right after its Thing starts, the binding can report a missing PIN before
//   its stored keys connect: that shows checking until it has lasted
//   PIN_GRACE_MS, and only then unpaired.
//
// Anything else is dropped. The rule hands each press to the Thing's
// handler, as the binding's own console command (openhab:androidtv <Thing>
// <channel> <command>) does; no Item links those channels, so a client can
// press only what this rule allows. With SEND false (oh_render.read_only)
// it logs what it would press, and never the code, and pairs nothing again.
//
// Each command ends as the command record keeps it
// (thermiteDevice.commandRule): the handler acknowledges nothing, so a
// press it took is `sent`, and one it took while its Thing was not ONLINE
// has an unknown outcome.
// Each rendered device gets its own context, including its ordered command
// and poll rules. A slow peer cannot hold another device's context.
const DEVICES = null;
const PRESSES = null;
const SEND = true;
// THERMITE_BOUNDED_IO
// THERMITE_DEVICE

const openhab = require('openhab');

const ChannelUID = Java.type('org.openhab.core.thing.ChannelUID');
const StringType = Java.type('org.openhab.core.library.types.StringType');
const Files = Java.type('java.nio.file.Files');
const Paths = Java.type('java.nio.file.Paths');
const StandardCopyOption = Java.type('java.nio.file.StandardCopyOption');
const OpenHAB = Java.type('org.openhab.core.OpenHAB');

const VOLUME_UP = '24';
const VOLUME_DOWN = '25';
const AIM_MS = 3000;
const REQUEST = 'REQUEST';
const AGAIN = 'AGAIN';
const CANCEL = 'CANCEL';
const CODE = /^[0-9A-Fa-f]{6}$/;
const POLL = '0/5 * * * * ?';
// How long a device shows its code, and takes to check one.
const CODE_MS = 180000;
const CHECK_MS = 20000;
const FAILED_MS = 60000;
// How long the pairing port has to connect.
const CONNECT_MS = 3000;
// Pairing again: how long the device has to refuse the new keys; how long
// after that the binding's pairing connection has to open before the code
// is asked for (asked sooner, the request is dropped); how long a code may
// take to come, from the last request; how long the old pairing has to come back.
const REFUSE_MS = 60000;
const SETTLE_MS = 5000;
const REPAIR_MS = 300000;
const RESTORE_MS = 60000;
// How long a missing-PIN report must last before it means unpaired.
const PIN_GRACE_MS = 120000;
const PREVIOUS = '.previous';
// device id -> {value, at}: the volume last aimed at.
const aims = {};
// device id -> {asked, sent, refused, closed, restoring, failed}: when a
// code was last asked for and sent, until when it shows as refused, its
// port as closed or the old pairing as coming back, and whether that is
// for a refused code.
const pairings = {};
// device id -> {started, refused, asked}: a pairing again under way: when
// the Thing was restarted with new keys, when it was first seen refusing
// them, and when a code was last asked for.
const repairs = {};
// device id -> the previous keystore a failed move back left in place.
const restoreFailed = {};
// device id -> when the Thing was first seen OFFLINE for want of a PIN, in
// its current run of that status.
const pinSince = {};

function has(object, key) {
  return Object.prototype.hasOwnProperty.call(object, key);
}

function handlerOf(device) {
  const thing = openhab.things.getThing(device.thing);
  return thing ? thing.rawThing.getHandler() : null;
}

// Hands each command to the Thing's handler; `what` names them in the log.
// How that ended: sent, or why nothing was handed over.
function deliver(device, channel, commands, what) {
  const shown = what || (commands.length > 1 ? `${commands.length} x ${commands[0]}` : commands[0]);
  if (!SEND) {
    console.info(`${device.id}: SEND is false, not pressing ${channel} ${shown}`);
    return { outcome: 'dropped', code: 'read_only' };
  }
  const handler = handlerOf(device);
  if (!handler) {
    console.warn(`${device.id}: ${device.thing} has no handler; ${channel} ${shown} not pressed`);
    return { outcome: 'unsent', code: 'unreachable' };
  }
  const uid = new ChannelUID(`${device.thing}:${channel}`);
  for (const command of commands) handler.handleCommand(uid, new StringType(command));
  return { outcome: 'sent' };
}

function delivered(done) {
  return done.outcome === 'sent';
}

// A key or volume press: the binding takes it whatever its Thing's status
// and drops it without a word unless it is connected, so a press handed
// over while the Thing was not ONLINE is not known to have arrived.
function press(device, channel, commands) {
  const thing = openhab.things.getThing(device.thing);
  const why = thing && String(thing.status) === 'ONLINE' ? null : missingPin(thing) ? 'auth' : 'unreachable';
  const done = deliver(device, channel, commands);
  return delivered(done) && why ? { outcome: 'unknown', code: why } : done;
}

function key(device, button) {
  const presses = PRESSES[device.remote];
  if (!has(presses, button)) {
    console.warn(`${device.id}_key: no key for '${button}', dropped`);
    return { outcome: 'dropped', code: 'invalid' };
  }
  const [channel, command] = presses[button];
  return press(device, channel, [command]);
}

function volume(device, command) {
  const scale = device.volume;
  const along = (Number(command) - scale.min) / scale.step;
  if (!/^\d+(\.\d+)?$/.test(command) || Number(command) < scale.min || Number(command) > scale.max
      || Math.abs(along - Math.round(along)) > 1e-7) {
    console.warn(`${device.id}_android_volume: '${command}' is not a volume on its scale, dropped`);
    return { outcome: 'dropped', code: 'invalid' };
  }
  const target = Number(command);
  const aim = aims[device.id];
  const volumeItem = openhab.items.getItem(`${device.id}_android_volume`, true);
  const reported = volumeItem === null ? NaN : parseFloat(String(volumeItem.state));
  const from = aim && Date.now() - aim.at < AIM_MS ? aim.value : Math.round(reported);
  if (!Number.isFinite(from)) {
    console.warn(`${device.id}: its volume is not known yet; ${target} not stepped to`);
    return { outcome: 'unsent', code: 'not_ready' };
  }
  aims[device.id] = { value: target, at: Date.now() };
  const steps = Math.round((target - from) / scale.step);
  // Already there: nothing to press.
  if (steps === 0) return { outcome: 'applied' };
  return press(device, 'keycode', Array(Math.abs(steps)).fill(steps > 0 ? VOLUME_UP : VOLUME_DOWN));
}

function missingPin(thing) {
  return !!thing && String(thing.status) === 'OFFLINE'
    && /\bPIN\b/i.test(String(thing.rawThing.getStatusInfo().getDescription() || ''));
}

function pairingState(device) {
  const thing = openhab.things.getThing(device.thing);
  const now = Date.now();
  const pairing = pairings[device.id] || {};
  if (!missingPin(thing)) delete pinSince[device.id];
  else if (pinSince[device.id] === undefined) pinSince[device.id] = now;
  if (restoreFailed[device.id]) return 'failed';
  if (repairs[device.id]) return pairing.sent && now - pairing.sent < CHECK_MS ? 'checking' : 'repairing';
  if (pairing.restoring) {
    const online = thing && thing.rawThing.getHandler() && String(thing.status) === 'ONLINE';
    if (!online && now < pairing.restoring) return pairing.failed ? 'failed' : 'checking';
    delete pairings[device.id];
  }
  if (!thing || !thing.rawThing.getHandler()) return 'unavailable';
  const status = String(thing.status);
  // Also a paired device's: asked to pair again, it was asked nothing.
  if (pairing.closed && now < pairing.closed) return 'closed';
  if (status === 'ONLINE') {
    delete pairings[device.id];
    return 'paired';
  }
  if (pairing.sent && now - pairing.sent < CHECK_MS) return 'checking';
  if (pairing.sent) {
    pairings[device.id] = { refused: pairing.sent + CHECK_MS + FAILED_MS };
    return pairingState(device);
  }
  if (pairing.refused && now < pairing.refused) return 'failed';
  if (pairing.asked && now - pairing.asked < CODE_MS) return 'code_shown';
  if (status !== 'OFFLINE') return 'checking';
  const description = String(thing.rawThing.getStatusInfo().getDescription() || '');
  if (/initiali[sz]/i.test(description)) return 'checking';
  if (!/\bPIN\b/i.test(description)) return 'unreachable';
  return now - pinSince[device.id] >= PIN_GRACE_MS ? 'unpaired' : 'checking';
}

function showPairing(device) {
  advance(device);
  // Null while a reload has the rules in before the Items: the next status change posts it.
  const item = openhab.items.getItem(`${device.id}_pairing_status`, true);
  const state = pairingState(device);
  if (item && String(item.state) !== state) item.postUpdate(state);
}

// "host:port" of the Thing's pairing port when it does not connect, else
// null (also when the port is not known: the binding is left to try).
function closedPort(thing) {
  const config = thing.rawThing.getConfiguration();
  const rawHost = config.get('ipAddress');
  if (rawHost === null || rawHost === undefined) return null;
  const host = String(rawHost);
  const port = Number(String(config.get('googletvPort'))) + 1;
  if (!Number.isInteger(port)) return null;
  let link = null;
  try {
    link = thermiteIO.open(host, port, CONNECT_MS, thermiteIO.deadline(CONNECT_MS));
    return null;
  } catch (e) {
    return `${host}:${port}`;
  } finally {
    if (link) thermiteDevice.close(link);
  }
}

function paths(device) {
  const keystore = Paths.get(`${String(OpenHAB.getUserDataFolder())}/${device.keystore}`);
  return { keystore, previous: Paths.get(`${keystore}${PREVIOUS}`) };
}

// Disables the Thing, runs move and enables the Thing again, so that the
// binding starts on the keystore move leaves.
function restart(thing, move) {
  thing.setEnabled(false);
  try {
    move();
  } finally {
    thing.setEnabled(true);
  }
}

// How the request ended (see the header): begun is applied.
function pairAgain(device) {
  const thing = openhab.things.getThing(device.thing);
  if (!SEND) {
    console.info(`${device.id}: SEND is false, not pairing it again`);
    return { outcome: 'dropped', code: 'read_only' };
  }
  const closed = thing ? closedPort(thing) : null;
  if (closed) {
    console.warn(`${device.id}: its pairing port ${closed} does not connect, so it is not paired again `
      + '(the binding would drop the request for a code); restart the device, then ask again');
    pairings[device.id] = { closed: Date.now() + FAILED_MS };
    return { outcome: 'unsent', code: 'unreachable' };
  }
  const { keystore, previous } = paths(device);
  if (!Files.exists(keystore) || Files.exists(previous)) {
    console.warn(`${device.id}: not paired again: ${Files.exists(previous) ? `${previous} is there already` : `no ${keystore}`}`);
    return { outcome: 'dropped', code: 'held' };
  }
  try {
    restart(thing, () => Files.move(keystore, previous));
  } catch (e) {
    console.warn(`${device.id}: could not move ${keystore} aside (${e}), so it is not paired again`);
    pairings[device.id] = { restoring: Date.now() + RESTORE_MS, failed: false };
    return { outcome: 'unknown', code: 'other' };
  }
  console.info(`${device.id}: pairing again: ${keystore} moved to ${previous}, the Thing restarted on new keys`);
  delete pairings[device.id];
  repairs[device.id] = { started: Date.now(), refused: null, asked: null };
  return { outcome: 'applied' };
}

// Moves the previous keystore back over the new one, so the old pairing works
// again. How that ended, for a command that asked for it.
function goBack(device, why, failed) {
  delete repairs[device.id];
  const { keystore, previous } = paths(device);
  const say = failed ? console.warn : console.info;
  try {
    restart(openhab.things.getThing(device.thing),
      () => Files.move(previous, keystore, StandardCopyOption.REPLACE_EXISTING));
    say(`${device.id}: ${why}; ${previous} moved back, so it is on the pairing it had`);
  } catch (e) {
    // Retrying at each poll would restart the Thing every few seconds.
    restoreFailed[device.id] = String(previous);
    delete pairings[device.id];
    console.error(`${device.id}: ${why}, but ${previous} could not be moved back over ${keystore} (${e}); `
      + 'not retried: move it back by hand (or away to keep the new keys), or send CANCEL to try once more');
    return { outcome: 'unknown', code: 'other' };
  }
  pairings[device.id] = { restoring: Date.now() + RESTORE_MS, failed: Boolean(failed) };
  return { outcome: 'applied' };
}

// Takes a pairing again a step further, or back, as the Thing and the clock say.
function advance(device) {
  if (!SEND) return;
  const thing = openhab.things.getThing(device.thing);
  if (!thing) return;
  const repair = repairs[device.id];
  const { previous } = paths(device);
  if (restoreFailed[device.id]) {
    if (Files.exists(previous)) return;
    delete restoreFailed[device.id];
    console.info(`${device.id}: ${previous} is gone, so pairing works as before`);
  }
  if (!repair) {
    if (Files.exists(previous)) goBack(device, 'a pairing again did not finish', false);
    return;
  }
  const now = Date.now();
  const status = String(thing.status);
  const pairing = pairings[device.id] || {};
  if (status === 'ONLINE') {
    delete repairs[device.id];
    try {
      Files.deleteIfExists(previous);
      console.info(`${device.id}: paired again; ${previous} deleted`);
    } catch (e) {
      console.warn(`${device.id}: paired again, but ${previous} could not be deleted (${e})`);
    }
    return;
  }
  if (!repair.asked) {
    const description = String(thing.rawThing.getStatusInfo().getDescription() || '');
    if (!repair.refused && status === 'OFFLINE' && /\bPIN\b/i.test(description)) repair.refused = now;
    if (repair.refused && now - repair.refused >= SETTLE_MS) {
      if (delivered(deliver(device, 'pincode', [REQUEST], 'a request for a pairing code'))) {
        repair.asked = now;
        pairings[device.id] = { asked: now };
      } else {
        goBack(device, 'no code could be asked for', true);
      }
    } else if (!repair.refused && now - repair.started >= REFUSE_MS) {
      goBack(device, `it did not refuse the new keys within ${REFUSE_MS / 1000} s`, true);
    }
    return;
  }
  if (pairing.sent && now - pairing.sent >= CHECK_MS) goBack(device, 'the code was not accepted', true);
  else if (!pairing.sent && now - repair.asked >= REPAIR_MS) {
    goBack(device, `no code paired it within ${REPAIR_MS / 60000} minutes`, false);
  }
}

// A pairing command this is not the moment for: another step is under way.
const HELD = Object.freeze({ outcome: 'dropped', code: 'held' });

// REQUEST and codes while pairing again: a new code once one was asked for,
// or the code. How the command ended.
function repairing(device, command) {
  const repair = repairs[device.id];
  if (command === REQUEST) {
    if (!repair.asked) {
      console.info(`${device.id}: a code is asked for once it has refused the new keys`);
      return HELD;
    }
    const done = deliver(device, 'pincode', [REQUEST], 'a request for a pairing code');
    if (delivered(done)) {
      repair.asked = Date.now();
      pairings[device.id] = { asked: repair.asked };
    }
    return done;
  }
  if (!repair.asked) {
    console.warn(`${device.id}: no code was asked for yet; the code is dropped`);
    return HELD;
  }
  const done = deliver(device, 'pincode', [command.toUpperCase()], 'the pairing code');
  if (delivered(done)) pairings[device.id] = { sent: Date.now() };
  return done;
}

// How a pairing command ended: what the binding's handler was handed is
// sent, a step of pairing again begun or undone is applied, and so is a
// request that what already holds makes needless.
function paired(device, command) {
  if (command === CANCEL && restoreFailed[device.id]) {
    delete restoreFailed[device.id];
    return SEND ? goBack(device, 'asked to restore the previous pairing again', true) : { outcome: 'dropped', code: 'read_only' };
  }
  if (restoreFailed[device.id]) {
    console.warn(`${device.id}: ${restoreFailed[device.id]} could not be moved back; send CANCEL to try again or move it by hand`);
    return HELD;
  }
  if (command === CANCEL) {
    if (repairs[device.id]) return goBack(device, 'cancelled', false);
    console.info(`${device.id}: not pairing again; nothing to cancel`);
    return { outcome: 'applied' };
  }
  if (repairs[device.id] && (command === REQUEST || CODE.test(command))) return repairing(device, command);
  if ((pairings[device.id] || {}).restoring && (command === REQUEST || command === AGAIN)) {
    console.info(`${device.id}: going back to the pairing it had; ask again once it is paired`);
    return HELD;
  }
  if (command === AGAIN && repairs[device.id]) {
    console.info(`${device.id}: pairing again already`);
    return HELD;
  }
  if (command === AGAIN && String((openhab.things.getThing(device.thing) || {}).status) === 'ONLINE') return pairAgain(device);
  if (command === REQUEST || command === AGAIN) {
    // Not paired, so asking for a code is all pairing again takes.
    const thing = openhab.things.getThing(device.thing);
    const closed = SEND && thing && String(thing.status) !== 'ONLINE' ? closedPort(thing) : null;
    if (thing && String(thing.status) === 'ONLINE') {
      console.info(`${device.id}: paired already; no code asked for`);
      return { outcome: 'applied' };
    }
    if (closed) {
      console.warn(`${device.id}: its pairing port ${closed} does not connect, so no code was asked for `
        + '(the binding would drop the request); restart the device, then ask again');
      pairings[device.id] = { closed: Date.now() + FAILED_MS };
      return { outcome: 'unsent', code: 'unreachable' };
    }
    const done = deliver(device, 'pincode', [REQUEST], 'a request for a pairing code');
    if (delivered(done)) pairings[device.id] = { asked: Date.now() };
    return done;
  }
  if (CODE.test(command)) {
    const done = deliver(device, 'pincode', [command.toUpperCase()], 'the pairing code');
    if (delivered(done)) pairings[device.id] = { sent: Date.now() };
    return done;
  }
  console.warn(`${device.id}_pairing: a command that is not ${REQUEST}, ${AGAIN}, ${CANCEL} or six letters and digits, dropped`);
  return { outcome: 'dropped', code: 'invalid' };
}

function pair(device, command) {
  const done = paired(device, command);
  showPairing(device);
  return done;
}

openhab.rules.JSRule({
  id: `androidtv_googletv_${DEVICES[0].id}_poll`,
  name: 'Android TVs: keep each pairing status',
  triggers: [openhab.triggers.GenericCronTrigger(POLL)],
  execute: () => {
    for (const device of DEVICES) showPairing(device);
  },
});

for (const device of DEVICES) {
  const points = device.volume === null ? ['key', 'pairing'] : ['key', 'android_volume', 'pairing'];
  thermiteDevice.commandRule({
    id: `androidtv_googletv_${device.id}_command`,
    name: `${device.id}: press its remote keys, step its volume and pair it`,
    device: device.id,
    items: points.map((point) => `${device.id}_${point}`),
    direct: (event) => {
      const command = String(event.receivedCommand);
      if (event.itemName === `${device.id}_key`) return key(device, command);
      if (event.itemName === `${device.id}_pairing`) return pair(device, command);
      return volume(device, command);
    },
  });
  openhab.rules.JSRule({
    id: `androidtv_googletv_${device.id}_status`,
    name: `${device.id}: keep its pairing status as its Thing's status changes`,
    triggers: [openhab.triggers.ThingStatusChangeTrigger(device.thing)],
    execute: () => showPairing(device),
  });
}
