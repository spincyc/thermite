'use strict';
// Loads the production routing.js template with a minimal lifecycle TABLE and
// stub openHAB, executor and planner modules. No device or network access.
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const source = fs.readFileSync(process.argv[2], 'utf8');
const tests = [];
function test(name, fn) { fn(); tests.push(name); }

const room = (id, label, output) => ({
  item: `loc_${id}_source`, label, hdmi: output,
  choices: { off: [[output, '9']], tv: [[output, '1']] },
  power: { off: [], tv: [[`${id}_tv_power`, 'ON']] },
  inputs: { off: [], tv: [[`${id}_tv_input`, 'hdmi1']] },
  after: { off: [], tv: [[`${id}_lights`, 'ON']] },
  readback: { 9: 'off', 1: 'tv' }, switch: null, stages: [],
  diagnosis: { item: `loc_${id}_source_problem`, checks: {} },
});
const TABLE = {
  rooms: { den: room('den', 'Den', 'matrix_output1_input'), office: room('office', 'Office', 'matrix_output2_input') },
  edid: { inputs: { 1: 'matrix_input1_edid' }, outputs: { matrix_output1_input: '5', matrix_output2_input: '6' }, idle: null },
  off: 'off',
  lifecycle: { resources: { matrix_output1_input: { key: 'matrix' }, matrix_output2_input: { key: 'matrix' } },
    io: { wake: 'house_routing_io_events' }, consumers: [] },
};

function load(options = {}) {
  const items = new Map(), sent = [], posted = [], rules = {}, logs = [], calls = [];
  let onLegacy = null, deps = null;
  const slots = options.slots || {};
  const item = (name, state = 'NULL') => {
    if (!items.has(name)) items.set(name, { state, sendCommand(value) { sent.push([name, String(value)]); calls.push('send ' + name); },
      postUpdate(value) { posted.push([name, String(value)]); this.state = String(value); } });
    return items.get(name);
  };
  for (const id of Object.keys(TABLE.rooms)) {
    const r = TABLE.rooms[id];
    for (const name of [r.item, r.diagnosis.item, `${id}_tv_power`, `${id}_tv_input`, `${id}_lights`]) item(name);
    item(r.hdmi, '9');
  }
  item('matrix_input1_edid', '5');
  const lifecycle = {
    request(id, choice) {
      calls.push(`request ${id} ${choice}`);
      if (options.refuse && options.refuse[id]) throw new Error(options.refuse[id]);
      return 1;
    },
    observe() {}, snapshot: () => ({ slots, holds: {}, endpoints: {} }), recover(key, evidence) { calls.push(`recover ${key} ${evidence.terminal}`); },
    releaseHold(id) { calls.push('release ' + id); }, hold(id) { calls.push('hold ' + id); }, takeover: () => [], external() {},
    report(event) { calls.push(`report ${event.type} ${event.token}`); }, close() {},
  };
  const executor = { transaction: fn => fn(), invalidateStale() { calls.push('invalidate'); }, dispatch() {}, probe() {},
    authorizeLegacy(token) { calls.push('authorize ' + token); },
    onLegacy(callback) { onLegacy = callback; }, events() {} };
  const modules = {
    openhab: {
      items: { getItem: (name, nullable) => items.get(name) || (nullable ? null : assert.fail('no Item ' + name)) },
      rules: { JSRule(rule) { rules[rule.id] = rule; } },
      triggers: { ItemCommandTrigger: name => ({ name }), ItemStateChangeTrigger: name => ({ name }) },
    },
    'thermite-routing-io': { create: () => executor, nativeJournal: () => ({ load: () => null, save() {}, path: '/userdata/thermite-routing/executor_coordinator.json' }) },
    'thermite-routing-lifecycle': { createCoordinator(descriptor, dependencies) { deps = dependencies; return lifecycle; } },
    '@runtime': { lifecycleTracker: { addDisposeHook() {} } },
  };
  const context = vm.createContext({
    require: name => modules[name] || assert.fail('unexpected module ' + name),
    console: { info: m => logs.push(['info', m]), warn: m => logs.push(['warn', m]), error: m => logs.push(['error', m]) },
    setTimeout: () => 0, clearTimeout() {}, Date, JSON, Map, Set, Object, String, Number, Error, Math,
  });
  vm.runInContext(source.replace('const TABLE = null;', 'const TABLE = ' + JSON.stringify(TABLE) + ';'), context);
  posted.length = 0; calls.length = 0;
  const command = (id, value) => rules[`routing_${id}_command`].execute({ receivedCommand: value });
  return { sent, posted, logs, calls, command, deps: () => deps, legacy: event => onLegacy(event) };
}

test('a refused route claim sends no EDID, hold or unmanaged step and re-reads the room', () => {
  const r = load({ refuse: { den: 'lifecycle resource conflict: matrix_output1_input (office)' } });
  r.command('den', 'tv');
  assert.deepEqual(r.sent, []);
  assert.deepEqual(r.calls, ['request den tv']);
  assert(r.logs.some(([level, message]) => level === 'warn' && message === 'Den: tv was not routed: matrix_output1_input is held by Office'));
  assert(r.posted.some(([name, value]) => name === 'loc_den_source' && value === 'off'));
});

test('an accepted route claims before its EDID and unmanaged steps', () => {
  const r = load();
  r.command('office', 'tv');
  assert.equal(r.calls[0], 'request office tv');
  assert(r.calls.indexOf('send matrix_input1_edid') > r.calls.indexOf('invalidate'));
  assert(r.sent.some(([name, value]) => name === 'office_tv_power' && value === 'ON'));
  assert(!r.sent.some(([name]) => name === 'matrix_output2_input'));
});

test('other planner errors still surface, before anything is sent', () => {
  const r = load({ refuse: { den: 'unknown lifecycle route' } });
  assert.throws(() => r.command('den', 'tv'), /unknown lifecycle route/);
  assert.deepEqual(r.sent, []);
});

test('actor readiness clears only quarantine the executor caused', () => {
  for (const [reason, cleared] of [['restart-uncertain', true], ['provider_outcome_unknown', true], ['journal_or_admission_failed', true],
    ['executor_quarantined', true], ['executor_event_overflow', true], ['adapter-threw', false],
    ['unordered-external-admission', false], ['incompatible-journal', false]]) {
    const r = load({ slots: { matrix: { quarantine: reason } } });
    r.legacy({ type: 'retired', legacy: true, recovered: true, key: 'matrix', token: null });
    assert.deepEqual(r.calls.filter(call => call.startsWith('recover')), cleared ? ['recover matrix true'] : [], reason);
  }
});

test('recovery retires the manual command whose outcome was unknown, and only that one', () => {
  const r = load(), lost = ['legacy', 'lost'].join('-'), live = ['legacy', 'live'].join('-');
  for (const token of [lost, live]) {
    r.legacy({ type: 'legacy', token, key: 'matrix', resource: 'matrix_output1_input', value: '1', managed: true, effectResources: [] });
  }
  r.legacy({ type: 'uncertain', token: lost, key: 'matrix', reason: 'provider_outcome_unknown' });
  assert(!r.calls.includes('release legacy-lost'));
  r.legacy({ type: 'retired', legacy: true, recovered: true, key: 'matrix', token: lost });
  assert(r.calls.includes('release legacy-lost')); assert(!r.calls.includes('release legacy-live'));
});

test('the planner is told where its journal is', () => {
  assert.equal(load().deps().journalPath, '/userdata/thermite-routing/executor_coordinator.json');
});

process.stdout.write(JSON.stringify({ passed: tests.length, tests }));
