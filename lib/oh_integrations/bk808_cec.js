// Fixed source CEC requests only. Never route, modify matrix settings, persist
// CEC selection, infer player state, or forward a caller-supplied JSON body.
'use strict';
const openhab = require('openhab');
const ChannelUID = Java.type('org.openhab.core.thing.ChannelUID');
const StringType = Java.type('org.openhab.core.library.types.StringType');
const DEVICES = null;
const SEND = true;
for (const device of DEVICES) openhab.rules.JSRule({
  id: `cec_${device.id}_key`, name: `${device.id}: fixed source CEC requests`,
  triggers: [openhab.triggers.ItemCommandTrigger(`${device.id}_key`)],
  execute: event => {
    const key = String(event.receivedCommand);
    if (!SEND || !Object.prototype.hasOwnProperty.call(device.requests, key)) return;
    const thing = openhab.things.getThing(device.thing);
    if (!thing || String(thing.status) !== 'ONLINE') return;
    const handler = thing.rawThing.getHandler();
    if (!handler) return;
    try { handler.handleCommand(new ChannelUID(`${device.thing}:command`), new StringType(device.requests[key])); }
    catch (_) { console.warn(`${device.id}: CEC request was not accepted; player state is not reported`); }
  },
});
