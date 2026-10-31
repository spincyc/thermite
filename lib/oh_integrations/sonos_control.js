// Sonos remote keys use the binding's native channels. No group/join API,
// power, arbitrary URI or arbitrary native command is reachable here.
'use strict';
const openhab = require('openhab');
const DEVICES = null;
const SEND = true;

function send(device, suffix, command) {
  if (SEND) openhab.items.getItem(`${device.id}_${suffix}`).sendCommand(command);
}

function press(device, button) {
  if (!SEND) return;
  if (button === 'previous' || button === 'next') {
    send(device, 'player', button === 'previous' ? 'PREVIOUS' : 'NEXT');
  } else if (button === 'play_pause') {
    const state = String(openhab.items.getItem(`${device.id}_state`).state);
    if (state === 'PLAYING') send(device, 'player', 'PAUSE');
    else if (state === 'STOPPED' || state === 'PAUSED_PLAYBACK') send(device, 'player', 'PLAY');
    else console.warn(`${device.id}: playback state is not known; no toggle sent`);
  } else if (button === 'tv' && device.model === 'Arc') {
    send(device, 'tv', device.udn);
  } else if (button === 'night' || button === 'speech') {
    const state = String(openhab.items.getItem(`${device.id}_${button}`).state);
    if (state === 'ON' || state === 'OFF') send(device, button, state === 'ON' ? 'OFF' : 'ON');
    else console.warn(`${device.id}: ${button} state is not known; no toggle sent`);
  }
}

for (const device of DEVICES) {
  openhab.rules.JSRule({
    id: `sonos_${device.id}_key`,
    name: `${device.id}: Sonos transport, TV audio and sound modes`,
    triggers: [openhab.triggers.ItemCommandTrigger(`${device.id}_key`)],
    execute: event => press(device, String(event.receivedCommand)),
  });
}
