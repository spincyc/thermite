// Only profile-verified keys enter the native MAP lookup. No raw IR payload,
// relay/serial command, arbitrary channel or inferred device state is exposed.
'use strict';
const DEVICES = null;
const SEND = true;
// THERMITE_DEVICE
// Why a key was not sent is said once per device and reason a minute (a held
// button repeats), without the caller's key text; the command record
// (thermiteDevice.commandRule) has a line for each, with its code.
const drop = thermiteDevice.rateDrop();
// How a key ended. An IR code has no acknowledgement: one the transport took
// is sent, and the device's state is not reported.
function press(device, allowed, key) {
  if (!allowed.has(key)) return drop(device.id, 'not a verified key of its IR profile', 'dropped', 'invalid');
  return thermiteDevice.deliverToChannel(
    device.thing, device.channel, key,
    (reason, outcome, code) => drop(device.id, reason, outcome, code),
    'the IR transport did not accept the key',
  );
}
for (const device of DEVICES) {
  const allowed = new Set(device.keys);
  thermiteDevice.commandRule({
    id: `ir_${device.id}_key`, name: `${device.id}: verified IR keys`, device: device.id,
    items: [`${device.id}_key`],
    direct: event => press(device, allowed, String(event.receivedCommand)),
  });
}
