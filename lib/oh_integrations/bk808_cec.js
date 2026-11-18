// Fixed source CEC requests only. Never route, modify matrix settings, persist
// CEC selection, infer player state, or forward a caller-supplied JSON body.
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
// How a key ended. The matrix's answer to a CEC request is not the player's:
// one the transport took is sent, and the player's state is not reported.
function press(device, key) {
  if (!Object.prototype.hasOwnProperty.call(device.requests, key)) {
    return drop(device, 'not a fixed CEC request of this source', 'dropped', 'invalid');
  }
  if (!SEND) return drop(device, 'SEND is false (read-only)', 'dropped', 'read_only');
  const thing = openhab.things.getThing(device.thing);
  if (!thing || String(thing.status) !== 'ONLINE') {
    return drop(device, `${device.thing} is ${thing ? String(thing.status) : 'missing'}, not ONLINE`, 'unsent', 'unreachable');
  }
  const handler = thing.rawThing.getHandler();
  if (!handler) return drop(device, `${device.thing} has no handler`, 'unsent', 'unreachable');
  try { handler.handleCommand(new ChannelUID(`${device.thing}:command`), new StringType(device.requests[key])); }
  catch (_) { return drop(device, 'the CEC transport did not accept the request', 'unknown', 'other'); }
  return { outcome: 'sent' };
}
for (const device of DEVICES) thermiteDevice.commandRule({
  id: `cec_${device.id}_key`, name: `${device.id}: fixed source CEC requests`, device: device.id,
  items: [`${device.id}_key`],
  direct: event => press(device, String(event.receivedCommand)),
});
