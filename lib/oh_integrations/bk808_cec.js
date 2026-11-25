// Fixed source CEC requests only. Never route, modify matrix settings, persist
// CEC selection, infer player state, or forward a caller-supplied JSON body.
'use strict';
const DEVICES = null;
const SEND = true;
// THERMITE_DEVICE
// Why a key was not sent is said once per device and reason a minute (a held
// button repeats), without the caller's key text; the command record
// (thermiteDevice.commandRule) has a line for each, with its code.
const drop = thermiteDevice.rateDrop();
// How a key ended. The matrix's answer to a CEC request is not the player's:
// one the transport took is sent, and the player's state is not reported.
function press(device, key) {
  if (!thermiteDevice.has(device.requests, key)) {
    return drop(device.id, 'not a fixed CEC request of this source', 'dropped', 'invalid');
  }
  return thermiteDevice.deliverToChannel(
    device.thing, 'cec', device.requests[key],
    (reason, outcome, code) => drop(device.id, reason, outcome, code),
    'the CEC transport did not accept the request',
  );
}
for (const device of DEVICES) thermiteDevice.commandRule({
  id: `cec_${device.id}_key`, name: `${device.id}: fixed source CEC requests`, device: device.id,
  items: [`${device.id}_key`],
  direct: event => press(device, String(event.receivedCommand)),
});
