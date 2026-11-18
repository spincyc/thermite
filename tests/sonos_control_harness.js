'use strict';
const fs = require('fs');
const vm = require('vm');
const { records } = require('./command_record.js');
const source = fs.readFileSync(0, 'utf8');
function load(quiet = false) {
  const sent = [], rules = [], logs = [], posted = [];
  const states = { example_arc_state: 'PLAYING', example_arc_night: 'OFF', example_arc_speech: 'ON' };
  const openhab = {
    items: { getItem: name => ({ get state() { return states[name] ?? 'UNDEF'; }, sendCommand: value => sent.push([name, value]),
      postUpdate: value => { posted.push([name, String(value)]); states[name] = String(value); } }) },
    rules: { JSRule: rule => rules.push(rule) }, triggers: { ItemCommandTrigger: item => item }
  };
  vm.runInNewContext(quiet ? source.replace('const SEND = true;', 'const SEND = false;') : source,
    { require: name => { if (name !== 'openhab') throw Error('unexpected import'); return openhab; },
      console: { info: message => logs.push(['info', message]), warn: message => logs.push(['warn', message]) } });
  return { sent, states, ...records(logs, posted), press: key => rules[0].execute({ itemName: 'example_arc_key', receivedCommand: key }) };
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
process.stdout.write(JSON.stringify({ sent: expected, unknownSent: live.sent, readOnlySent: quiet.sent,
  recorded: live.recorded(), problems: live.problems('example_arc_problem'), tally: live.tally('example_arc_sonos_commands'),
  readOnlyRecorded: quiet.recorded(), readOnlyProblems: quiet.problems('example_arc_problem') }));
