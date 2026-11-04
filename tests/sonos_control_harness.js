'use strict';
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync(0, 'utf8');
function load(quiet = false) {
  const sent = [], rules = [], states = { example_arc_state: 'PLAYING', example_arc_night: 'OFF', example_arc_speech: 'ON' };
  const openhab = {
    items: { getItem: name => ({ get state() { return states[name] ?? 'UNDEF'; }, sendCommand: value => sent.push([name, value]) }) },
    rules: { JSRule: rule => rules.push(rule) }, triggers: { ItemCommandTrigger: item => item }
  };
  vm.runInNewContext(quiet ? source.replace('const SEND = true;', 'const SEND = false;') : source,
    { require: name => { if (name !== 'openhab') throw Error('unexpected import'); return openhab; }, console: { warn() {} } });
  return { sent, states, press: key => rules[0].execute({ receivedCommand: key }) };
}
const live = load();
for (const k of ['previous', 'next', 'play_pause']) live.press(k);
live.states.example_arc_state = 'PAUSED_PLAYBACK';
for (const k of ['play_pause', 'night', 'speech', 'tv']) live.press(k);
const expected = live.sent.slice();live.sent.length = 0;
for (const k of ['warp', '<script>', 'http://example.invalid/', 'power']) live.press(k);
live.states.example_arc_state = 'UNDEF';live.states.example_arc_night = 'NULL';
for (const k of ['play_pause', 'night']) live.press(k);
const quiet = load(true);
for (const k of ['previous', 'play_pause', 'tv', 'night', 'speech']) quiet.press(k);
process.stdout.write(JSON.stringify({ sent: expected, unknownSent: live.sent, readOnlySent: quiet.sent }));
