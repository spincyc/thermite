// Only profile-verified keys enter the native MAP lookup. No raw IR payload,
// relay/serial command, arbitrary channel or inferred device state is exposed.
'use strict';
const openhab = require('openhab');
const ChannelUID = Java.type('org.openhab.core.thing.ChannelUID');
const StringType = Java.type('org.openhab.core.library.types.StringType');
const DEVICES = null;
const SEND = true;
// THERMITE_DEVICE
// Why a key was not sent is said once per device and reason a minute (a held
// button repeats), without the caller's key text; the command record
// (thermiteDevice.commandRule) has a line for each, with its code.
const DROP_MS = 60000;
const dropped = new Map();
function drop(device, reason, outcome, code) {
  const key = `${device.id}:${reason}`, now = Date.now(), last = dropped.get(key);
  if (last === undefined || now - last >= DROP_MS) {
    dropped.set(key, now);
    console.warn(`${device.id}: key dropped: ${reason}`);
  }
  return { outcome, code };
}
// How a key ended. An IR code has no acknowledgement: one the transport took
// is sent, and the device's state is not reported.
function press(device, allowed, key) {
  if (!allowed.has(key)) return drop(device, 'not a verified key of its IR profile', 'dropped', 'invalid');
  if (!SEND) return drop(device, 'SEND is false (read-only)', 'dropped', 'read_only');
  const thing = openhab.things.getThing(device.thing);
  if (!thing || String(thing.status) !== 'ONLINE') {
    return drop(device, `${device.thing} is ${thing ? String(thing.status) : 'missing'}, not ONLINE`, 'unsent', 'unreachable');
  }
  const handler = thing.rawThing.getHandler();
  if (!handler) return drop(device, `${device.thing} has no handler`, 'unsent', 'unreachable');
  try { handler.handleCommand(new ChannelUID(`${device.thing}:${device.channel}`), new StringType(key)); }
  catch (_) { return drop(device, 'the IR transport did not accept the key', 'unknown', 'other'); }
  return { outcome: 'sent' };
}
for (const device of DEVICES) {
  const allowed = new Set(device.keys);
  thermiteDevice.commandRule({
    id: `ir_${device.id}_key`, name: `${device.id}: verified IR keys`, device: device.id,
    items: [`${device.id}_key`],
    direct: event => press(device, allowed, String(event.receivedCommand)),
  });
}
