// Only profile-verified keys enter the native MAP lookup. No raw IR payload,
// relay/serial command, arbitrary channel or inferred device state is exposed.
'use strict';
const openhab = require('openhab');
const ChannelUID = Java.type('org.openhab.core.thing.ChannelUID');
const StringType = Java.type('org.openhab.core.library.types.StringType');
const DEVICES = null;
const SEND = true;
for (const device of DEVICES) {
  const allowed = new Set(device.keys);
  openhab.rules.JSRule({
    id: `ir_${device.id}_key`, name: `${device.id}: verified IR keys`,
    triggers: [openhab.triggers.ItemCommandTrigger(`${device.id}_key`)],
    execute: event => {
      const key = String(event.receivedCommand);
      if (!SEND || !allowed.has(key)) return;
      const thing = openhab.things.getThing(device.thing);
      if (!thing || String(thing.status) !== 'ONLINE') return;
      const handler = thing.rawThing.getHandler();
      if (!handler) return;
      try { handler.handleCommand(new ChannelUID(`${device.thing}:${device.channel}`), new StringType(key)); }
      catch (_) { console.warn(`${device.id}: IR transport did not accept the key; device state is not reported`); }
    },
  });
}
