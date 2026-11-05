'use strict';
// Loads the production routing.js template with a minimal lifecycle TABLE and
// stub openHAB, executor and planner modules. No device or network access.
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert');
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
  holdMs: 15000,
  lifecycle: { resources: { matrix_output1_input: { key: 'matrix' }, matrix_output2_input: { key: 'matrix' } },
    io: { wake: 'house_routing_io_events' }, consumers: [] },
};

function load(options = {}) {
  const items = new Map(), sent = [], posted = [], rules = {}, logs = [], calls = [], routes = [];
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
    request(id, choice, route) {
      calls.push(`request ${id} ${choice}`);
      routes.push(route);
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
  posted.length = 0; calls.length = 0; logs.length = 0;
  const command = (id, value, eventSource) => rules[`routing_${id}_command`].execute({ receivedCommand: value, eventSource });
  return { sent, posted, logs, calls, routes, command, context, deps: () => deps, legacy: event => onLegacy(event) };
}

// Sources a command can carry: what openHAB's REST API makes of the
// gateway's X-OpenHAB-Source, and others that must never be logged as text.
const GATEWAY = 'thermite.web:1a2b3c4d=>org.openhab.core.io.rest$thermite';
const SOURCES = [
  [GATEWAY, { origin: 'web', route: '1a2b3c4d' }],
  ['thermite.all_off:0123abcd', { origin: 'all_off', route: '0123abcd' }],
  ['thermite.schedule:89abcdef=>org.openhab.core.io.rest$thermite=>org.openhab.automation.jsscripting$rule', { origin: 'schedule', route: '89abcdef' }],
  ['org.openhab.core.io.rest$admin', { origin: 'openhab', route: null }],
  ['org.openhab.automation.jsscripting$scene_rule', { origin: 'openhab', route: null }],
  ['thermite.web:1A2B3C4D', { origin: 'other', route: null }],
  ['thermite.web:1a2b3c4', { origin: 'other', route: null }],
  ['thermite.web:1a2b3c4d9', { origin: 'other', route: null }],
  ['thermite.web:1a2b3c4d extra=>org.openhab.core.io.rest$x', { origin: 'other', route: null }],
  ['thermite.w:1a2b3c4d', { origin: 'other', route: null }],
  ['thermite.Web:1a2b3c4d', { origin: 'other', route: null }],
  ['x=>thermite.web:1a2b3c4d', { origin: 'other', route: null }],
  ['hostile route=deadbeef origin=web\nforged line', { origin: 'other', route: null }],
  ['', { origin: 'none', route: null }],
  [null, { origin: 'none', route: null }],
  [undefined, { origin: 'none', route: null }],
];
const COMMAND_LINE = /^routing command room=[a-z0-9_]+ choice=[A-Za-z0-9_.@-]+ origin=[a-z_]+ route=(?:[0-9a-f]{8}|-)$/;

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

test('a gateway command logs its origin and id and the id rides the planner request as a label', () => {
  const r = load();
  r.command('office', 'tv', GATEWAY);
  assert.deepEqual(r.logs[0], ['info', 'routing command room=office choice=tv origin=web route=1a2b3c4d']);
  assert.match(r.logs[0][1], COMMAND_LINE);
  assert.deepEqual(r.calls.slice(0, 1), ['request office tv']); assert.deepEqual(r.routes, ['1a2b3c4d']);
  // A refused claim was still asked for with its id, and the line before the
  // refusal names it.
  const refused = load({ refuse: { den: 'lifecycle resource conflict: matrix_output1_input (office)' } });
  refused.command('den', 'tv', GATEWAY);
  assert.deepEqual(refused.routes, ['1a2b3c4d']);
  assert.deepEqual(refused.logs.map(([level]) => level), ['info', 'warn']);
  assert.equal(refused.logs[0][1], 'routing command room=den choice=tv origin=web route=1a2b3c4d');
});

test('any other source is named coarsely, never by its text, and carries no id', () => {
  for (const [source, expected] of SOURCES) {
    const r = load();
    r.command('den', 'tv', source);
    const line = r.logs.find(([level]) => level === 'info')[1];
    assert.match(line, COMMAND_LINE, String(source));
    assert.equal(line, `routing command room=den choice=tv origin=${expected.origin} route=${expected.route || '-'}`, String(source));
    assert.deepEqual(r.routes, [expected.route], String(source));
    if (!expected.route) {
      for (const text of ['hostile', 'forged', 'deadbeef', 'admin', 'scene_rule', 'extra', '1A2B3C4D'])
        assert(!r.logs.some(([, message]) => message.includes(text)), `${source} leaked ${text}`);
    }
  }
});

test('an unknown choice is refused with its source fields and without its text', () => {
  const r = load();
  r.command('den', 'not-a-source <script>', GATEWAY);
  assert.deepEqual(r.logs, [['warn', 'routing command room=den choice=- origin=web route=1a2b3c4d outcome=refused reason=unknown_choice']]);
  assert.deepEqual(r.calls, []); assert.deepEqual(r.sent, []);
});

test('routing.js and lifecycle_io.js parse command sources identically', () => {
  const r = load();
  const io = { exports: {} };
  vm.runInNewContext(fs.readFileSync(path.join(path.dirname(process.argv[2]), 'lifecycle_io.js'), 'utf8'),
    { module: io, require: () => assert.fail('no module at load') });
  for (const [source, expected] of SOURCES) {
    assert.deepEqual(JSON.parse(JSON.stringify(r.context.commandSource(source))), expected, String(source));
    assert.deepEqual(JSON.parse(JSON.stringify(io.exports.commandSource(source))), expected, String(source));
  }
});

test('the planner logs through the script console', () => {
  const r = load();
  r.deps().log.warn('routing deadline room=den');
  assert.deepEqual(r.logs, [['warn', 'routing deadline room=den']]);
});

process.stdout.write(JSON.stringify({ passed: tests.length, tests }));
