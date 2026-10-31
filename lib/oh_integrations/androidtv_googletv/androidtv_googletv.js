// Android and Google TVs and NVIDIA SHIELDs over the androidtv binding
// (lib/oh_integrations/androidtv_googletv).
//
// DEVICES lists each device: its id (its Items are <id>_key,
// <id>_android_volume, <id>_pairing and <id>_pairing_status), the UID of its
// googletv Thing, the remote it is drawn with and the volume it is held to
// (null for a SHIELD, whose sound is the room's). The binding reports power,
// mute and volume and switches power and mute itself, but it cannot set a
// volume, it takes key codes rather than remote button ids, and it pairs only
// from its console, so this rule does all three:
//
// - a button id sent to <id>_key is pressed as PRESSES[remote] gives it: a key
//   code on the Thing's keycode channel, or an app link on its debug channel;
// - a volume sent to <id>_android_volume is held to the TV's max and reached
//   with one VOLUME_UP or VOLUME_DOWN press per step, counted from the volume
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
//   middle) is moved back the same way at the next poll.
//
// Anything else is dropped. The rule hands each press to the Thing's
// handler, as the binding's own console command (openhab:androidtv <Thing>
// <channel> <command>) does; no Item links those channels, so a client can
// press only what this rule allows. With SEND false (oh_render.read_only)
// it logs what it would press, and never the code, and pairs nothing again.
const DEVICES = null;
const PRESSES = null;
const SEND = true;

const openhab = require('openhab');

const ChannelUID = Java.type('org.openhab.core.thing.ChannelUID');
const StringType = Java.type('org.openhab.core.library.types.StringType');
const InetSocketAddress = Java.type('java.net.InetSocketAddress');
const Socket = Java.type('java.net.Socket');
const Files = Java.type('java.nio.file.Files');
const Paths = Java.type('java.nio.file.Paths');
const StandardCopyOption = Java.type('java.nio.file.StandardCopyOption');
const JavaSystem = Java.type('java.lang.System');

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

function has(object, key) {
  return Object.prototype.hasOwnProperty.call(object, key);
}

function handlerOf(device) {
  const thing = openhab.things.getThing(device.thing);
  return thing ? thing.rawThing.getHandler() : null;
}

// Hands each command to the Thing's handler; `what` names them in the log.
function deliver(device, channel, commands, what) {
  if (commands.length === 0) return false;
  const shown = what || (commands.length > 1 ? `${commands.length} x ${commands[0]}` : commands[0]);
  if (!SEND) {
    console.info(`${device.id}: SEND is false, not pressing ${channel} ${shown}`);
    return false;
  }
  const handler = handlerOf(device);
  if (!handler) {
    console.warn(`${device.id}: ${device.thing} has no handler; ${channel} ${shown} not pressed`);
    return false;
  }
  const uid = new ChannelUID(`${device.thing}:${channel}`);
  for (const command of commands) handler.handleCommand(uid, new StringType(command));
  return true;
}

function key(device, button) {
  const presses = PRESSES[device.remote];
  if (!has(presses, button)) {
    console.warn(`${device.id}_key: no key for '${button}', dropped`);
    return;
  }
  const [channel, command] = presses[button];
  deliver(device, channel, [command]);
}

function volume(device, command) {
  if (!/^\d+(\.\d+)?$/.test(command)) {
    console.warn(`${device.id}_android_volume: '${command}' is not a volume, dropped`);
    return;
  }
  const target = Math.min(Math.round(Number(command)), device.max);
  const aim = aims[device.id];
  const reported = parseFloat(String(openhab.items.getItem(`${device.id}_android_volume`).state));
  const from = aim && Date.now() - aim.at < AIM_MS ? aim.value : Math.round(reported);
  if (!Number.isFinite(from)) {
    console.warn(`${device.id}: its volume is not known yet; ${target} not stepped to`);
    return;
  }
  aims[device.id] = { value: target, at: Date.now() };
  const steps = target - from;
  deliver(device, 'keycode', Array(Math.abs(steps)).fill(steps > 0 ? VOLUME_UP : VOLUME_DOWN));
}

function pairingState(device) {
  const thing = openhab.things.getThing(device.thing);
  const now = Date.now();
  const pairing = pairings[device.id] || {};
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
  return /\bPIN\b/i.test(description) ? 'unpaired' : 'unreachable';
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
  const host = String(config.get('ipAddress'));
  const port = Number(String(config.get('googletvPort'))) + 1;
  if (!Number.isInteger(port)) return null;
  const socket = new Socket();
  try {
    socket.connect(new InetSocketAddress(host, port), CONNECT_MS);
    return null;
  } catch (e) {
    return `${host}:${port}`;
  } finally {
    socket.close();
  }
}

function paths(device) {
  const keystore = Paths.get(`${JavaSystem.getProperty('openhab.userdata') || 'userdata'}/${device.keystore}`);
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

function pairAgain(device) {
  const thing = openhab.things.getThing(device.thing);
  if (!SEND) {
    console.info(`${device.id}: SEND is false, not pairing it again`);
    return;
  }
  const closed = thing ? closedPort(thing) : null;
  if (closed) {
    console.warn(`${device.id}: its pairing port ${closed} does not connect, so it is not paired again `
      + '(the binding would drop the request for a code); restart the device, then ask again');
    pairings[device.id] = { closed: Date.now() + FAILED_MS };
    return;
  }
  const { keystore, previous } = paths(device);
  if (!Files.exists(keystore) || Files.exists(previous)) {
    console.warn(`${device.id}: not paired again: ${Files.exists(previous) ? `${previous} is there already` : `no ${keystore}`}`);
    return;
  }
  try {
    restart(thing, () => Files.move(keystore, previous));
  } catch (e) {
    console.warn(`${device.id}: could not move ${keystore} aside (${e}), so it is not paired again`);
    pairings[device.id] = { restoring: Date.now() + RESTORE_MS, failed: false };
    return;
  }
  console.info(`${device.id}: pairing again: ${keystore} moved to ${previous}, the Thing restarted on new keys`);
  delete pairings[device.id];
  repairs[device.id] = { started: Date.now(), refused: null, asked: null };
}

// Moves the previous keystore back over the new one, so the old pairing works again.
function goBack(device, why, failed) {
  delete repairs[device.id];
  const { keystore, previous } = paths(device);
  const say = failed ? console.warn : console.info;
  try {
    restart(openhab.things.getThing(device.thing),
      () => Files.move(previous, keystore, StandardCopyOption.REPLACE_EXISTING));
    say(`${device.id}: ${why}; ${previous} moved back, so it is on the pairing it had`);
  } catch (e) {
    console.warn(`${device.id}: ${why}, but ${previous} could not be moved back (${e}); pair it again`);
  }
  pairings[device.id] = { restoring: Date.now() + RESTORE_MS, failed: Boolean(failed) };
}

// Takes a pairing again a step further, or back, as the Thing and the clock say.
function advance(device) {
  if (!SEND) return;
  const thing = openhab.things.getThing(device.thing);
  if (!thing) return;
  const repair = repairs[device.id];
  const { previous } = paths(device);
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
      if (deliver(device, 'pincode', [REQUEST], 'a request for a pairing code')) {
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

// REQUEST and codes while pairing again: a new code once one was asked for, or the code.
function repairing(device, command) {
  const repair = repairs[device.id];
  if (command === REQUEST) {
    if (!repair.asked) {
      console.info(`${device.id}: a code is asked for once it has refused the new keys`);
    } else if (deliver(device, 'pincode', [REQUEST], 'a request for a pairing code')) {
      repair.asked = Date.now();
      pairings[device.id] = { asked: repair.asked };
    }
  } else if (!repair.asked) {
    console.warn(`${device.id}: no code was asked for yet; the code is dropped`);
  } else if (deliver(device, 'pincode', [command.toUpperCase()], 'the pairing code')) {
    pairings[device.id] = { sent: Date.now() };
  }
}

function pair(device, command) {
  if (command === CANCEL) {
    if (repairs[device.id]) goBack(device, 'cancelled', false);
    else console.info(`${device.id}: not pairing again; nothing to cancel`);
  } else if (repairs[device.id] && (command === REQUEST || CODE.test(command))) {
    repairing(device, command);
  } else if ((pairings[device.id] || {}).restoring && (command === REQUEST || command === AGAIN)) {
    console.info(`${device.id}: going back to the pairing it had; ask again once it is paired`);
  } else if (command === AGAIN && repairs[device.id]) {
    console.info(`${device.id}: pairing again already`);
  } else if (command === AGAIN && String((openhab.things.getThing(device.thing) || {}).status) === 'ONLINE') {
    pairAgain(device);
  } else if (command === REQUEST || command === AGAIN) {
    // Not paired, so asking for a code is all pairing again takes.
    const thing = openhab.things.getThing(device.thing);
    const closed = SEND && thing && String(thing.status) !== 'ONLINE' ? closedPort(thing) : null;
    if (thing && String(thing.status) === 'ONLINE') {
      console.info(`${device.id}: paired already; no code asked for`);
    } else if (closed) {
      console.warn(`${device.id}: its pairing port ${closed} does not connect, so no code was asked for `
        + '(the binding would drop the request); restart the device, then ask again');
      pairings[device.id] = { closed: Date.now() + FAILED_MS };
    } else if (deliver(device, 'pincode', [REQUEST], 'a request for a pairing code')) {
      pairings[device.id] = { asked: Date.now() };
    }
  } else if (CODE.test(command)) {
    if (deliver(device, 'pincode', [command.toUpperCase()], 'the pairing code')) {
      pairings[device.id] = { sent: Date.now() };
    }
  } else {
    console.warn(`${device.id}_pairing: a command that is not ${REQUEST}, ${AGAIN}, ${CANCEL} or six letters and digits, dropped`);
  }
  showPairing(device);
}

openhab.rules.JSRule({
  id: 'androidtv_googletv_pairing_poll',
  name: 'Android TVs: keep each pairing status',
  triggers: [openhab.triggers.GenericCronTrigger(POLL)],
  execute: () => {
    for (const device of DEVICES) showPairing(device);
  },
});

for (const device of DEVICES) {
  const points = device.max === null ? ['key', 'pairing'] : ['key', 'android_volume', 'pairing'];
  openhab.rules.JSRule({
    id: `androidtv_googletv_${device.id}_command`,
    name: `${device.id}: press its remote keys, step its volume and pair it`,
    triggers: points.map((point) => openhab.triggers.ItemCommandTrigger(`${device.id}_${point}`))
      .concat([openhab.triggers.ThingStatusChangeTrigger(device.thing)]),
    execute: (event) => {
      if (!event.itemName) {
        showPairing(device);
        return;
      }
      const command = String(event.receivedCommand);
      if (event.itemName === `${device.id}_key`) key(device, command);
      else if (event.itemName === `${device.id}_pairing`) pair(device, command);
      else volume(device, command);
    },
  });
}
