// Only profile-verified keys enter the native MAP lookup. No raw IR payload,
// relay/serial command, arbitrary channel or inferred device state is exposed.
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
for (const device of DEVICES) {
  const allowed = new Set(device.keys);
  openhab.rules.JSRule({
    id: `ir_${device.id}_key`, name: `${device.id}: verified IR keys`,
    triggers: [openhab.triggers.ItemCommandTrigger(`${device.id}_key`)],
    execute: event => {
      const key = String(event.receivedCommand);
      if (!SEND) return drop(device, 'SEND is false (read-only)', console.info);
      if (!allowed.has(key)) return drop(device, 'not a verified key of its IR profile');
      const thing = openhab.things.getThing(device.thing);
      if (!thing || String(thing.status) !== 'ONLINE') return drop(device, `${device.thing} is ${thing ? String(thing.status) : 'missing'}, not ONLINE`);
      const handler = thing.rawThing.getHandler();
      if (!handler) return drop(device, `${device.thing} has no handler`);
      try { handler.handleCommand(new ChannelUID(`${device.thing}:${device.channel}`), new StringType(key)); }
      catch (_) { console.warn(`${device.id}: IR transport did not accept the key; device state is not reported`); }
    },
  });
}
