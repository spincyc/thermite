'use strict';
// stdin: a generated house model (remote_model.build); argv: the room, its TV
// and its Arc. The shared web/TUI backend talks to openHAB's REST API through a
// fake fetch that records every POST; nothing leaves the process.
const fs = require('fs'), path = require('path');
const Shared = require(path.join(__dirname, '..', 'tui', 'remote', 'shared'));
console.info = console.warn = () => {};
const shared = Shared.load(), house = JSON.parse(fs.readFileSync(0, 'utf8'));
const [room, tv, arc] = process.argv.slice(2);
const states = {}, posted = [];
for (const entry of house.rooms) states[`loc_${entry.id}_source`] = entry.sources[0] || 'off';
for (const entry of Object.values(house.devices)) {
  const c = entry.control || {};
  for (const key of ['key', 'power', 'mute']) if (c[key]) states[c[key]] = key === 'power' ? 'ON' : key === 'mute' ? 'OFF' : 'NULL';
  if (c.volume) states[c.volume.item] = '40';
}
const fetch = async (url, init) => {
  const match = /^\/rest\/items\/([^/?]+)(?:\/state)?$/.exec(url);
  if (!match) return {ok: false, status: 404, text: async () => '', json: async () => ({})};
  if (init && init.method === 'POST') posted.push([match[1], init.body]);
  return {ok: true, status: 200, text: async () => states[match[1]] ?? 'UNDEF'};
};
function Events() { this.close = () => {}; this.addEventListener = () => {}; }
(async () => {
  const backend = shared.Remote.create(house, {backend: 'openhab', fetch, EventSource: Events});
  for (const value of [50, 75, 100, 101]) await backend.setVolume(room, value);
  const volume = await backend.press(room, tv, 'volume_up');
  await backend.press(room, null, 'mute');
  await backend.press(room, arc, 'tv');
  if (backend.stop) backend.stop();
  process.stdout.write(JSON.stringify({posted, volumeTarget: volume.device.id}));
  process.exit(0);
})().catch(error => { process.stderr.write(String(error.stack)); process.exit(1); });
