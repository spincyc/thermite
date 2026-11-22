'use strict';
// Rendered tables from test_audio_scaler.py exercise the production Source
// command rule. The executor answers owned reads from the same device model
// that receives plain Item commands and reports changes to openHAB.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(process.argv[2], 'utf8');
const planner = require(process.argv[3]);
const tables = JSON.parse(fs.readFileSync(0, 'utf8'));
const ROOM = 'example_lounge', SOURCE = 'loc_example_lounge_source';
const COMPUTER = 'example_computer', DISC = 'example_disc', PLAYER = 'example_player';
const PICTURE = 'example_matrix_output1_input', AUDIO = 'example_matrix_output2_input';
const SCALER = 'example_matrix_output2_scaler';

function load(table, initialScaler = '3') {
  const items = new Map(), rules = [], pending = [], scalerWrites = [], writes = [], reads = [];
  const initial = {
    [SOURCE]: 'NULL', [SOURCE + '_problem']: 'NULL', [SOURCE + '_lifecycle']: 'NULL',
    [PICTURE]: '8', [AUDIO]: '8', [SCALER]: initialScaler, example_usb_device1_host: '4',
    example_tv_input: 'hdmi1', example_tv_power: 'OFF', example_avr_main_input: 'AUX2', example_avr_main_power: 'OFF',
  };
  function apply(name, value, via) {
    writes.push({name, value, via});
    const item = items.get(name), oldState = item.state;
    if (oldState === value) return;
    item.state = value;
    for (const rule of rules) {
      if (rule.triggers.some(t => t.change === name)) rule.execute({itemName: name, newState: value, oldState});
    }
  }
  for (const [name, state] of Object.entries(initial)) {
    items.set(name, {state,
      sendCommand(value) { pending.push(() => apply(name, String(value), 'item')); },
      postUpdate(value) { this.state = String(value); },
    });
  }
  function submit(op, report) {
    // Let every ready operation run ahead of the scaler write. A matrix
    // audio route must wait even when that property's actor finishes last.
    const queue = op.kind === 'write' && op.resource === SCALER ? scalerWrites : pending;
    queue.push(() => {
      const token = op.token;
      if (!op.isCurrent()) return report({type: 'terminal', token, admitted: false});
      report({type: 'admitted', token});
      if (op.kind === 'probe') {
        reads.push(op.resource);
        report({type: 'sample', token, value: items.get(op.resource).state, fresh: true, retired: true});
      } else {
        apply(op.resource, op.value, 'executor');
        report({type: 'terminal', token, sample: {value: op.value, fresh: true, retired: true}});
      }
    });
  }
  const modules = {
    openhab: {
      items: {getItem: (name, nullable) => items.get(name) || (nullable ? null : assert.fail('no Item ' + name))},
      rules: {JSRule: rule => rules.push(rule)},
      triggers: {ItemCommandTrigger: command => ({command}), ItemStateChangeTrigger: change => ({change}),
        ItemStateUpdateTrigger: update => ({update})},
    },
    '@runtime': {lifecycleTracker: {addDisposeHook() {}}},
    'thermite-routing-io': {
      create: () => ({transaction: fn => fn(), probe: submit, dispatch: submit, invalidateStale() {}, onLegacy() {}, events() {}}),
      nativeJournal: () => ({load: () => null, save() {}}),
    },
    'thermite-routing-lifecycle': planner,
  };
  vm.runInNewContext(source.replace('const TABLE = null;', 'const TABLE = ' + JSON.stringify(table) + ';'), {
    require: name => modules[name] || assert.fail('unexpected module ' + name),
    console: {info() {}, warn: console.warn, error: console.error},
    Date: {now: () => 1000}, setTimeout: () => 1, clearTimeout() {},
  });
  function choose(choice) {
    writes.length = 0;
    reads.length = 0;
    const rule = rules.find(r => r.triggers.some(t => t.command === SOURCE));
    rule.execute({receivedCommand: choice});
    for (let steps = 0; pending.length || scalerWrites.length; steps++) {
      assert(steps < 500, 'route did not settle');
      const next = pending.shift() || scalerWrites.shift();
      next();
    }
    assert.equal(items.get(SOURCE).state, choice);
    if (table.lifecycle) {
      const status = JSON.parse(items.get(SOURCE + '_lifecycle').state);
      assert.equal(status.choice, choice);
      assert.equal(choice === table.off ? status.endpointInactive : status.pathReady, true);
    }
  }
  return {choose, writes, reads, state: name => items.get(name).state};
}

const tests = [];
function test(name, run) { tests.push([name, run]); }

for (const [choice, input, scaler] of [[COMPUTER, '1', '4'], [DISC, '2', '3'], ['off', '8', '3'], [PLAYER, '8', '3']]) {
  test(choice, () => {
    for (const [mode, table] of Object.entries(tables)) {
      const room = load(table);
      if (choice !== COMPUTER) room.choose(COMPUTER);
      room.choose(choice);
      const via = mode === 'lifecycle' ? 'executor' : 'item';
      assert.deepEqual(room.writes.filter(w => w.name.endsWith('_scaler')), [{name: SCALER, value: scaler, via}], mode);
      assert.equal(room.state(PICTURE), input, mode);
      assert.equal(room.state(AUDIO), input, mode);
      assert.equal(room.state(SCALER), scaler, mode);
      if (mode === 'plain' || choice === COMPUTER || choice === DISC) {
        const audioSteps = room.writes.filter(w => [SCALER, AUDIO].includes(w.name));
        assert.deepEqual(audioSteps.map(w => w.name), [SCALER, AUDIO], mode);
      }
    }
  });
}
test('the lifecycle reads an already-correct scaler and leaves it alone', () => {
  const room = load(tables.lifecycle, '4');
  room.choose(COMPUTER);
  assert(room.reads.includes(SCALER));
  assert.deepEqual(room.writes.filter(w => w.name.endsWith('_scaler')), []);
  assert.equal(room.state(AUDIO), '1');
});

let failed = 0;
for (const [name, run] of tests) {
  try { run(); } catch (error) { failed++; console.error(name, error); }
}
process.stdout.write(JSON.stringify({passed: tests.length - failed, failed, tests: tests.map(([name]) => name)}) + '\n');
if (failed) process.exitCode = 1;
