// Fixed source CEC requests only. Never route, modify matrix settings, persist
// CEC selection, infer player state, or forward a caller-supplied JSON body.
'use strict';
const openhab = require('openhab');
const ChannelUID = Java.type('org.openhab.core.thing.ChannelUID');
const StringType = Java.type('org.openhab.core.library.types.StringType');
const DEVICES = null;
const SEND = true;
// A dropped key is said once per device and reason a minute (a held button
// repeats), without the caller's key text.
const DROP_MS = 60000;
const dropped = new Map();
function drop(device, reason, say = console.warn) {
  const key = `${device.id}:${reason}`, now = Date.now(), last = dropped.get(key);
  if (last !== undefined && now - last < DROP_MS) return;
  dropped.set(key, now);
  say(`${device.id}: key dropped: ${reason}`);
}
for (const device of DEVICES) openhab.rules.JSRule({
  id: `cec_${device.id}_key`, name: `${device.id}: fixed source CEC requests`,
  triggers: [openhab.triggers.ItemCommandTrigger(`${device.id}_key`)],
  execute: event => {
    const key = String(event.receivedCommand);
    if (!SEND) return drop(device, 'SEND is false (read-only)', console.info);
    if (!Object.prototype.hasOwnProperty.call(device.requests, key)) return drop(device, 'not a fixed CEC request of this source');
    const thing = openhab.things.getThing(device.thing);
    if (!thing || String(thing.status) !== 'ONLINE') return drop(device, `${device.thing} is ${thing ? String(thing.status) : 'missing'}, not ONLINE`);
    const handler = thing.rawThing.getHandler();
    if (!handler) return drop(device, `${device.thing} has no handler`);
    try { handler.handleCommand(new ChannelUID(`${device.thing}:command`), new StringType(device.requests[key])); }
    catch (_) { console.warn(`${device.id}: CEC request was not accepted; player state is not reported`); }
  },
});
