// Sonos remote keys use the binding's native channels. No group/join API,
// power, arbitrary URI or arbitrary native command is reachable here.
'use strict';
const openhab = require('openhab');
const DEVICES = null;
const SEND = true;
// THERMITE_DEVICE

// The binding's channel takes the command and acknowledges nothing: sent.
function send(device, suffix, command) {
  const item = openhab.items.getItem(`${device.id}_${suffix}`, true);
  if (item === null) return unknown(device, suffix);
  item.sendCommand(command);
  return { outcome: 'sent' };
}

// A toggle needs the state it toggles: without it nothing is sent.
function unknown(device, what) {
  console.warn(`${device.id}: ${what} state is not known; no toggle sent`);
  return { outcome: 'unsent', code: 'not_ready' };
}

// How a key ended, for the command record (thermiteDevice.commandRule).
function press(device, button) {
  const known = ['previous', 'next', 'play_pause', 'night', 'speech'].includes(button) || (button === 'tv' && device.model === 'Arc');
  if (!known) return { outcome: 'dropped', code: 'invalid' };
  if (!SEND) return { outcome: 'dropped', code: 'read_only' };
  if (button === 'previous' || button === 'next') return send(device, 'player', button === 'previous' ? 'PREVIOUS' : 'NEXT');
  if (button === 'play_pause') {
    const item = openhab.items.getItem(`${device.id}_state`, true);
    const state = item === null ? 'NULL' : String(item.state);
    if (state === 'PLAYING') return send(device, 'player', 'PAUSE');
    if (state === 'STOPPED' || state === 'PAUSED_PLAYBACK') return send(device, 'player', 'PLAY');
    return unknown(device, 'playback');
  }
  if (button === 'tv') return send(device, 'tv', device.udn);
  const item = openhab.items.getItem(`${device.id}_${button}`, true);
  const state = item === null ? 'NULL' : String(item.state);
  if (state === 'ON' || state === 'OFF') return send(device, button, state === 'ON' ? 'OFF' : 'ON');
  return unknown(device, button);
}

for (const device of DEVICES) {
  thermiteDevice.commandRule({
    id: `sonos_${device.id}_key`,
    name: `${device.id}: Sonos transport, TV audio and sound modes`,
    device: device.id,
    items: [`${device.id}_key`],
    direct: event => press(device, String(event.receivedCommand)),
  });
}
