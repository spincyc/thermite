'use strict';
// Loads the production routing.js template with a minimal lifecycle TABLE and
// stub openHAB, executor and planner modules. No device or network access.
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert');
const source = fs.readFileSync(process.argv[2], 'utf8');
// The planner's own reason table, as routing.js reads it in production.
const planner = require(path.join(path.dirname(path.resolve(process.argv[2])), 'lifecycle.js'));
const {QUARANTINE_REASONS} = planner;
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
  hotplug: { reports: 2, absentMs: 60000 },
  reads: {},
  lifecycle: { resources: { matrix_output1_input: { key: 'matrix' }, matrix_output2_input: { key: 'matrix' } },
    io: { wake: 'house_routing_io_events' }, consumers: [] },
};

// The same rooms over a descriptor the real planner takes (load({real})):
// a matrix whose outputs are independent, and a receiver whose fields are not.
const REAL = { ...TABLE, lifecycle: { version: 1, deadlineMs: 30000, sources: {}, endpoints: {},
  resources: {
    matrix_output1_input: { key: 'matrix', values: ['1', '9'], capable: true, independent: true },
    matrix_output2_input: { key: 'matrix', values: ['1', '9'], capable: true, independent: true },
    receiver_input: { key: 'receiver', values: ['1', '2'], capable: true },
    receiver_power: { key: 'receiver', values: ['ON', 'OFF'], capable: true },
  },
  io: { wake: 'house_routing_io_events' }, consumers: [] } };

function load(options = {}) {
  const items = new Map(), sent = [], posted = [], rules = {}, logs = [], calls = [], routes = [], observed = [];
  // Timers run only when advance() passes them.
  let timers = [];
  const table = options.table || (options.real ? REAL : TABLE);
  let onLegacy = null, deps = null, real = null;
  const slots = options.slots || {};
  const item = (name, state = 'NULL') => {
    if (!items.has(name)) items.set(name, { state, sendCommand(value) { sent.push([name, String(value)]); calls.push('send ' + name); },
      postUpdate(value) { posted.push([name, String(value)]); this.state = String(value); } });
    return items.get(name);
  };
  const init = (name, fallback = 'NULL') =>
    item(name, options.initial && Object.prototype.hasOwnProperty.call(options.initial, name) ? options.initial[name] : fallback);
  for (const id of Object.keys(table.rooms)) {
    const r = table.rooms[id];
    for (const name of [r.item, r.diagnosis.item, `${id}_tv_power`, `${id}_tv_input`, `${id}_lights`]) init(name);
    init(r.hdmi, '9');
  }
  item('matrix_input1_edid', '5');
  if (table.lifecycle) for (const name of Object.keys(table.lifecycle.resources)) item(name);
  const hooks = [];
  const lifecycle = {
    request(id, choice, route) {
      calls.push(`request ${id} ${choice}`);
      routes.push(route);
      if (options.refuse && options.refuse[id]) throw new Error(options.refuse[id]);
      return 1;
    },
    observe(name, value) { observed.push([name, value]); }, snapshot: () => ({ slots, holds: options.holds || {}, endpoints: {} }),
    recover(key, evidence) { calls.push(`recover ${key} ${evidence.terminal} ${evidence.resource || '-'}`); },
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
      triggers: { ItemCommandTrigger: name => ({ name }), ItemStateChangeTrigger: name => ({ name }),
        ItemStateUpdateTrigger: name => ({ name, update: true }) },
    },
    'thermite-routing-io': { create: () => executor, nativeJournal: () => ({ load: () => null, save() {}, path: '/userdata/thermite-routing/executor_coordinator.json' }) },
    'thermite-routing-lifecycle': { QUARANTINE_REASONS,
      createCoordinator(descriptor, dependencies) {
        deps = dependencies;
        if (options.real) real = planner.createCoordinator(descriptor, dependencies);
        return real || lifecycle;
      } },
    '@runtime': { lifecycleTracker: { addDisposeHook(fn) { hooks.push(fn); } } },
  };
  const context = vm.createContext({
    require: name => modules[name] || assert.fail('unexpected module ' + name),
    console: { info: m => logs.push(['info', m]), warn: m => logs.push(['warn', m]), error: m => logs.push(['error', m]) },
    setTimeout: (fn, ms) => { const timer = { at: (options.clock ? options.clock.now : 0) + ms, fn }; timers.push(timer); return timer; },
    clearTimeout: timer => { timers = timers.filter(other => other !== timer); }, Date: options.clock ? { now: () => options.clock.now } : Date, JSON, Map, Set, Object, String, Number, Error, Math,
  });
  vm.runInContext(source.replace('const TABLE = null;', 'const TABLE = ' + JSON.stringify(table) + ';'), context);
  posted.length = 0; calls.length = 0; logs.length = 0;
  const command = (id, value, eventSource) => rules[`routing_${id}_command`].execute({ receivedCommand: value, eventSource });
  const advance = ms => {
    const end = options.clock.now + ms;
    for (;;) {
      const due = timers.filter(timer => timer.at <= end).sort((a, b) => a.at - b.at)[0];
      if (!due) break;
      timers = timers.filter(timer => timer !== due);
      options.clock.now = Math.max(options.clock.now, due.at);
      due.fn();
    }
    options.clock.now = end;
  };
  const dispose = () => { for (const fn of hooks) fn(); };
  const fireNextEarly = beforeMs => {
    const due = timers.sort((a, b) => a.at - b.at)[0];
    assert(due, 'expected a pending timer');
    timers = timers.filter(other => other !== due);
    options.clock.now = due.at - beforeMs;
    due.fn();
  };
  return { sent, posted, logs, calls, routes, observed, command, context, item, rules, advance, dispose, fireNextEarly,
    pending: () => timers.length, deps: () => deps, legacy: event => onLegacy(event), planner: () => real };
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

test('actor readiness hands the planner its proof, a property\'s alone for an independent one', () => {
  // A deploy's incompatible journal, a thrown adapter and an out-of-order
  // admission used to wait for a second reload; the planner ends those in
  // its table (lifecycle_planner_harness.js).
  for (const reason of ['incompatible-journal', 'adapter-threw', 'unordered-external-admission'])
    assert(QUARANTINE_REASONS.includes(reason), reason);
  const [orphan, port] = ['orphan', 'port'].map(name => ['legacy', name].join('-'));
  const holds = { [orphan]: { resources: ['matrix_output1_input', 'matrix_output2_input'], sources: [] },
    [port]: { resources: ['matrix_output1_input'], sources: [] } };
  const device = load({ holds });
  device.legacy({ type: 'retired', legacy: true, recovered: true, key: 'matrix', token: null });
  assert.deepEqual(device.calls.filter(call => /^(recover|release)/.test(call)),
    ['release ' + orphan, 'release ' + port, 'recover matrix true -']);
  // A property's proof ends an orphaned command on it alone, and says
  // nothing of the rest of the device, where another may still be on its way.
  const field = load({ holds });
  field.legacy({ type: 'retired', legacy: true, recovered: true, key: 'matrix', resource: 'matrix_output1_input', token: null });
  assert.deepEqual(field.calls.filter(call => /^(recover|release)/.test(call)),
    ['release ' + port, 'recover matrix true matrix_output1_input']);
  // Nor is a property the planner does not manage its to hear.
  const other = load({ holds });
  other.legacy({ type: 'retired', legacy: true, recovered: true, key: 'matrix', resource: 'matrix_input1_edid', token: null });
  assert.deepEqual(other.calls.filter(call => /^(recover|release)/.test(call)), []);
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

test('with the real planner, a manual command to an independent field holds and quarantines that field alone', () => {
  const r = load({ real: true }), status = () => r.planner().snapshot();
  // An EDID input is independent at its actor but no planner property: its
  // readiness is not the planner's to hear.
  r.legacy({ type: 'retired', legacy: true, recovered: true, key: 'matrix', resource: 'matrix_input1_edid', token: null });
  const [one, edid, shared] = ['one', 'edid', 'shared'].map(name => ['legacy', name].join('-'));
  r.legacy({ type: 'legacy', token: one, key: 'matrix', resource: 'matrix_output1_input', value: '1', managed: true,
    independent: true, effectResources: [] });
  r.legacy({ type: 'legacy', token: edid, key: 'matrix', resource: 'matrix_input1_edid', value: '5', managed: false,
    independent: true, effectResources: [] });
  r.legacy({ type: 'legacy', token: shared, key: 'receiver', resource: 'receiver_input', value: '2', managed: true, effectResources: [] });
  assert.deepEqual(status().holds[one].resources, ['matrix_output1_input']);
  assert(!(edid in status().holds));
  assert.deepEqual(status().holds[shared].resources.sort(), ['receiver_input', 'receiver_power']);
  // Its unknown outcome quarantines its slot alone; the EDID's ends quietly.
  r.legacy({ type: 'admitted', token: one, key: 'matrix', legacy: true, managed: true, resource: 'matrix_output1_input' });
  r.legacy({ type: 'uncertain', token: one, key: 'matrix', legacy: true, managed: true, reason: 'provider_outcome_unknown' });
  r.legacy({ type: 'uncertain', token: edid, key: 'matrix', legacy: true, reason: 'provider_outcome_unknown' });
  assert.equal(status().slots['matrix#matrix_output1_input'].quarantine, 'provider_outcome_unknown');
  assert.equal(status().slots['matrix#matrix_output2_input'].quarantine, null);
  r.legacy({ type: 'retired', legacy: true, recovered: true, key: 'matrix', resource: 'matrix_input1_edid', token: edid });
  assert.equal(status().slots['matrix#matrix_output1_input'].quarantine, 'provider_outcome_unknown');
  // Its own readiness ends both its hold and its quarantine.
  r.legacy({ type: 'retired', legacy: true, recovered: true, key: 'matrix', resource: 'matrix_output1_input', token: one });
  assert(!(one in status().holds));
  assert.equal(status().slots['matrix#matrix_output1_input'].quarantine, null);
  assert.deepEqual(r.logs.filter(([level]) => level === 'error'), []);
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

test('a readback problem forwards only an input the room declares it reads back', () => {
  const r = load();
  const room = { diagnosis: { item: 'loc_den_source_problem', reported: ['1', 'SAT/CBL'],
    checks: { den_avr_input: { device: 'den_avr', label: 'Den receiver', point: 'input' } } } };
  const problem = (value) => { r.context.unavailable(room, 'den_avr_input', value); return room.readbackProblem; };
  assert.deepEqual(problem('SAT/CBL'), { version: 1, device: 'den_avr', label: 'Den receiver', point: 'input',
    item: 'den_avr_input', reason: 'unsupported_input', reported: 'SAT/CBL' });
  // Anything else the device says is left out, whatever it looks like.
  for (const value of ['3', 'hdmi2', 'tv', '<html>']) assert.equal(problem(value).reported, null, value);
  assert.deepEqual([problem(null).reason, problem(null).reported], ['unavailable', null]);
});

test('an HDMI switch the master matrix does not feed names itself when it does not answer', () => {
  const r = load();
  r.item('den_tv_power').state = 'ON'; r.item('den_tv_input').state = 'hdmi4';
  const output = r.item('den_matrix_output4_input', 'UNDEF');
  const check = { device: 'den_matrix', label: 'den.matrix', model: 'AC-MX-44X', action: 'read selected input' };
  const room = { hdmi: null, stages: [{ power: 'den_tv_power', input: 'den_tv_input', feed: 'hdmi4', readback: {} }],
    switch: { item: 'den_matrix_output4_input', matrix: null, readback: { 1: 'shield', 2: 'apple_tv' } },
    inputs: { shield: [['den_tv_input', 'hdmi4']], apple_tv: [['den_tv_input', 'hdmi4']] },
    diagnosis: { item: 'loc_den_source_problem', reported: ['1', '2', '3', '4'],
      checks: { den_matrix_output4_input: check } } };
  assert.equal(r.context.shown(room), 'UNDEF');
  assert.deepEqual(room.readbackProblem, { version: 1, ...check, item: 'den_matrix_output4_input',
    reason: 'unavailable', reported: null });
  room.readbackProblem = null; output.state = '3';
  assert.equal(r.context.shown(room), 'UNDEF');
  assert.deepEqual([room.readbackProblem.reason, room.readbackProblem.reported], ['unsupported_input', '3']);
  room.readbackProblem = null; output.state = '2';
  assert.equal(r.context.shown(room), 'apple_tv'); assert.equal(room.readbackProblem, null);
});

// A fictional room behind its own matrix's output 4, which confirms a switch
// of that output whether or not a display is cabled to it.
function unplugRoom(r) {
  r.item('den_matrix_output4_input', '2');
  const plug = r.item('den_matrix_output4_hotplug', 'OFF'), power = r.item('den_tv_power');
  power.state = 'ON'; r.item('den_tv_input').state = 'hdmi4';
  const detect = { device: 'den_matrix', label: 'den.matrix', model: 'AC-MX-44X', action: 'detect the display', output: 4 };
  const room = { item: 'loc_den_source', hdmi: null, stages: [{ power: 'den_tv_power', input: 'den_tv_input', feed: 'hdmi4', readback: {} }],
    switch: { item: 'den_matrix_output4_input', matrix: null, readback: { 1: 'shield', 2: 'apple_tv' }, hotplug: 'den_matrix_output4_hotplug' },
    inputs: { shield: [['den_tv_input', 'hdmi4']], apple_tv: [['den_tv_input', 'hdmi4']] },
    diagnosis: { item: 'loc_den_source_problem', reported: ['1', '2', '3', '4'], checks: { den_matrix_output4_hotplug: detect } } };
  return { room, plug, power, detect };
}

test('a room whose output keeps finding no display while its devices are on has no Source, and names that output', () => {
  const clock = { now: 1000000 }, r = load({ clock });
  const { room, plug, power, detect } = unplugRoom(r);
  const read = (state, after) => { clock.now += after; plug.state = state; return r.context.plugRead(room, 'den_matrix_output4_hotplug'); };
  const show = () => { room.readbackProblem = null; return r.context.shown(room); };
  // Two reads in a row find none, a minute apart.
  assert.equal(read('OFF', 0), false); assert.equal(show(), 'apple_tv');
  assert.equal(read('OFF', 59999), false); assert.equal(show(), 'apple_tv');
  assert.equal(read('OFF', 1), true); assert.equal(show(), 'UNDEF');
  assert.deepEqual(room.readbackProblem, { version: 1, ...detect, item: 'den_matrix_output4_hotplug', reason: 'no_display', reported: null });
  // Off is off; on again, the display is missing until a read finds it.
  power.state = 'OFF';
  assert.equal(read('OFF', 30000), false); assert.equal(show(), 'off');
  power.state = 'ON';
  assert.equal(show(), 'UNDEF'); assert.equal(room.readbackProblem.reason, 'no_display');
  assert.equal(read('ON', 30000), true); assert.equal(show(), 'apple_tv'); assert.equal(room.readbackProblem, null);
  // A read that finds one, or none at all (UNDEF), starts the count again,
  // as a read while the devices are off or not known does.
  for (const [state, after] of [['OFF', 0], ['ON', 10000], ['OFF', 60000], ['UNDEF', 30000], ['OFF', 60000]]) {
    assert.equal(read(state, after), false, `${state} after ${after} ms`);
  }
  assert.equal(show(), 'apple_tv');
  assert.equal(read('OFF', 60000), true); assert.equal(show(), 'UNDEF');
  assert.equal(read('ON', 1000), true);
  for (const state of ['OFF', 'NULL']) {
    power.state = state; assert.equal(read('OFF', 0), false);
    power.state = 'ON'; assert.equal(read('OFF', 60000), false, `after a read while ${state}`);
    assert.equal(show(), 'apple_tv'); assert.equal(read('ON', 0), false);
  }
  // A room that powers nothing has no devices on to say a display must be there.
  room.stages = [];
  for (let n = 0; n < 3; n++) assert.equal(read('OFF', 60000), false);
  assert.equal(show(), 'apple_tv');
});

test('a room\'s hot-plug Item is read back at every update, changed or not', () => {
  const clock = { now: 1000000 };
  const check = { device: 'matrix', label: 'Matrix', model: 'BK-808', action: 'detect the display', output: 1 };
  const den = { ...room('den', 'Den', 'matrix_output1_input'), hdmiHotplug: 'matrix_output1_hotplug',
    stages: [{ power: 'den_tv_power', input: 'den_tv_input', feed: 'hdmi1', readback: {} }],
    diagnosis: { item: 'loc_den_source_problem', reported: ['1', '9'], checks: { matrix_output1_hotplug: check } } };
  const r = load({ clock, table: { ...TABLE, rooms: { den } } });
  const rule = r.rules.routing_den_hotplug_readback;
  assert.deepEqual(JSON.parse(JSON.stringify(rule.triggers)), [{ name: 'matrix_output1_hotplug', update: true }]);
  r.item('den_tv_power').state = 'ON'; r.item('den_tv_input').state = 'hdmi1'; r.item('matrix_output1_input').state = '1';
  const plug = r.item('matrix_output1_hotplug', 'OFF'), posts = () => r.posted.filter(([name]) => name.startsWith('loc_den_'));
  rule.execute({ itemName: 'matrix_output1_hotplug' });
  clock.now += 60000;
  assert.deepEqual(posts(), []);
  rule.execute({ itemName: 'matrix_output1_hotplug' });
  assert.equal(r.item('loc_den_source').state, 'UNDEF');
  assert.deepEqual(JSON.parse(r.item('loc_den_source_problem').state), { version: 1, ...check, item: 'matrix_output1_hotplug',
    reason: 'no_display', reported: null });
  // Only a read that changes what the room shows reads it back.
  const count = posts().length;
  rule.execute({ itemName: 'matrix_output1_hotplug' });
  assert.equal(posts().length, count);
  plug.state = 'ON'; rule.execute({ itemName: 'matrix_output1_hotplug' });
  assert.equal(r.item('loc_den_source').state, 'tv'); assert.equal(r.item('loc_den_source_problem').state, 'UNDEF');
});

// A fictional Den on output 1 of a matrix polled every 10 s (its hold one
// and a half polls), or behind an HDMI switch the matrix does not feed. A
// poll's answer is the Item's update, which routing_reads counts, and, if it
// changed, its change, which the readback rule takes: in either order.
function missRoom({ via = 'matrix' } = {}) {
  const clock = { now: 1000000 };
  const name = via === 'matrix' ? 'matrix_output1_input' : 'den_switch_input';
  const check = via === 'matrix' ? { device: 'matrix', label: 'Matrix', model: 'BK-808', action: 'read matrix output input' }
    : { device: 'den_switch', label: 'Switch', model: 'VRROOM', action: 'read selected input' };
  const den = { ...room('den', 'Den', 'matrix_output1_input'),
    diagnosis: { item: 'loc_den_source_problem', reported: ['0', '1', '9'], checks: { [name]: check } } };
  if (via === 'switch') Object.assign(den, { hdmi: null, switch: { item: name, matrix: null, readback: { 0: 'tv' } } });
  const r = load({ clock, table: { ...TABLE, rooms: { den }, reads: { [name]: 15000 } } });
  const item = r.item(name);
  item.state = via === 'switch' ? '0' : '1';
  r.item('den_tv_input').state = 'hdmi1';
  const rule = via === 'matrix' ? 'routing_den_readback' : 'routing_den_switch_readback';
  const poll = (state, after = 10000, changeFirst = false) => {
    clock.now += after;
    const before = item.state;
    item.state = state;
    const update = () => r.rules.routing_reads.execute({ itemName: name });
    const change = () => { if (before !== state) r.rules[rule].execute({ itemName: name, oldState: before, newState: state }); };
    if (changeFirst) { change(); update(); } else { update(); change(); }
  };
  const source = () => r.item('loc_den_source').state;
  const problem = () => r.item('loc_den_source_problem').state;
  const unknown = { version: 1, ...check, item: name, reason: 'unavailable', reported: null };
  // The device's first read since the load, and the readback it brings.
  poll(item.state, 0);
  r.rules[rule].execute({ itemName: name, newState: item.state });
  return { r, clock, name, poll, source, problem, unknown };
}

test('one missed read of a room\'s matrix output or HDMI switch keeps its Source and readback problem', () => {
  for (const via of ['matrix', 'switch']) {
    for (const changeFirst of [false, true]) {
      const { r, name, poll, source, problem } = missRoom({ via });
      assert.deepEqual(JSON.parse(JSON.stringify(r.rules.routing_reads.triggers)), [{ name, update: true }]);
      assert.deepEqual([source(), problem()], ['tv', 'UNDEF']);
      r.posted.length = 0;
      // The BK-808's Thing times out one poll, 2 s after it asked.
      poll('UNDEF', 2000, changeFirst);
      assert.deepEqual([source(), problem()], ['tv', 'UNDEF'], `${via}, change first: ${changeFirst}`);
      assert.deepEqual(r.posted.filter(([item, value]) => item.startsWith('loc_den_') && value !== 'tv'), []);
      assert.equal(r.pending(), 1, 'the hold ends at its own time');
    }
  }
});

test('two missed reads in a row, or a hold that ends unread, make the Source unknown as one did before', () => {
  const { r, poll, source, problem, unknown } = missRoom();
  poll('UNDEF', 2000);
  poll('UNDEF', 10000);
  assert.equal(source(), 'UNDEF');
  assert.deepEqual(JSON.parse(problem()), unknown);
  // More misses change nothing; nor does the hold's end.
  poll('UNDEF', 10000);
  r.advance(15000);
  assert.deepEqual([source(), JSON.parse(problem())], ['UNDEF', unknown]);
  // A device that is not read again (a rule backing off) is unknown once the
  // hold ends, one and a half polls after the miss.
  const quiet = missRoom({ via: 'switch' });
  quiet.poll('UNDEF', 4000);
  quiet.r.advance(14999);
  assert.equal(quiet.source(), 'tv');
  quiet.r.advance(1);
  assert.deepEqual([quiet.source(), JSON.parse(quiet.problem())], ['UNDEF', quiet.unknown]);
  // Nothing is held before a first read: when an Item is NULL at load, a miss shows at once.
  const fresh = load({ clock: { now: 0 }, initial: { matrix_output1_input: 'NULL' },
    table: { ...TABLE, reads: { matrix_output1_input: 15000 } } });
  fresh.item('matrix_output1_input').state = 'UNDEF';
  fresh.rules.routing_den_readback.execute({ itemName: 'matrix_output1_input', newState: 'UNDEF' });
  assert.equal(fresh.item('loc_den_source').state, 'UNDEF');
});

test('reads are seeded from existing Item states on reload, and early hold timers reschedule instead of stranding', () => {
  const clock = { now: 0 };
  const r = load({ clock, initial: { matrix_output1_input: '1', den_tv_power: 'ON', den_tv_input: 'hdmi1' },
    table: { ...TABLE, reads: { matrix_output1_input: 15000 } } });
  assert.equal(r.item('loc_den_source').state, 'tv');
  r.item('matrix_output1_input').state = 'UNDEF';
  r.rules.routing_reads.execute({ itemName: 'matrix_output1_input', newState: 'UNDEF' });
  r.rules.routing_den_readback.execute({ itemName: 'matrix_output1_input', newState: 'UNDEF' });
  assert.equal(r.item('loc_den_source').state, 'tv');
  // Simulate timer jitter firing 5 ms early: must reschedule and expire at deadline, never strand.
  r.fireNextEarly(5);
  assert.equal(r.item('loc_den_source').state, 'tv');
  assert.equal(r.pending(), 1);
  r.advance(5);
  assert.equal(r.item('loc_den_source').state, 'UNDEF');
});

test('dispose clears resends, holds, and reads timers even without lifecycle', () => {
  const clock = { now: 0 };
  const plain = { ...TABLE, lifecycle: null, reads: { matrix_output1_input: 15000 } };
  const r = load({ clock, initial: { matrix_output1_input: '1' }, table: plain });
  r.item('den_tv_power').state = 'OFF';
  r.command('den', 'tv');
  assert(r.pending() > 0, 'expected hold/resend timers');
  r.item('matrix_output1_input').state = 'UNDEF';
  r.rules.routing_reads.execute({ itemName: 'matrix_output1_input', newState: 'UNDEF' });
  r.dispose();
  assert.equal(r.pending(), 0);
});

test('an input read before the room\'s last choice is never held for it, and nothing else sees a held input', () => {
  const { r, name, poll, source, problem, unknown } = missRoom();
  poll('9', 10000);
  assert.equal(source(), 'off');
  r.command('den', 'tv');
  // The matrix misses its read after the choice: the room's Source is
  // unknown, not the off it was before, and the planner sees UNDEF.
  poll('UNDEF', 2000);
  assert.deepEqual([source(), JSON.parse(problem())], ['UNDEF', unknown]);
  r.rules.routing_lifecycle_observations.execute({ itemName: name });
  assert.deepEqual(r.observed[r.observed.length - 1], [name, null]);
  // A read since the choice is held as any other, and the planner still sees UNDEF.
  poll('1', 8000);
  assert.equal(source(), 'tv');
  poll('UNDEF', 2000);
  assert.equal(source(), 'tv');
  r.rules.routing_lifecycle_observations.execute({ itemName: name });
  assert.deepEqual(r.observed[r.observed.length - 1], [name, null]);
});

test('the next read after a miss shows what the device reads, and a later miss is held again', () => {
  const { r, poll, source, problem } = missRoom();
  poll('UNDEF', 2000);
  poll('1', 8000);
  assert.deepEqual([source(), problem(), r.pending()], ['tv', 'UNDEF', 0]);
  r.advance(30000);
  assert.equal(source(), 'tv');
  // A read that finds another input after a miss shows it at once.
  poll('UNDEF', 2000);
  poll('9', 8000);
  assert.equal(source(), 'off');
  // Each miss after a read is one miss: a matrix that misses every other
  // read never shows unknown.
  for (let n = 0; n < 3; n++) {
    poll('UNDEF', 2000); assert.equal(source(), 'off');
    poll('9', 8000); assert.equal(source(), 'off');
  }
  assert.equal(problem(), 'UNDEF');
});
test('the planner logs through the script console', () => {
  const r = load();
  r.deps().log.warn('routing deadline room=den');
  assert.deepEqual(r.logs, [['warn', 'routing deadline room=den']]);
});

test('origin===repair is recognized as policy enforcement and never yields the endpoint or sets a manual pin', () => {
  const realTable = { ...REAL, lifecycle: { ...REAL.lifecycle,
    endpoints: {
      den: { choices: {
        tv: { requirements: { matrix_output1_input: '1' }, uses: [] },
        off: { off: true, release: { matrix_output1_input: '9' } },
      } },
      office: { choices: {
        tv: { requirements: { matrix_output2_input: '1' }, uses: [] },
        off: { off: true, release: { matrix_output2_input: '9' } },
      } },
    } } };
  const r = load({ real: true, table: realTable });
  r.item('loc_den_source_lifecycle', 'NULL');
  r.command('den', 'tv');
  assert.equal(r.planner().snapshot().endpoints.den.yielded, false);
  // Policy repair command with origin === 'repair' holds transiently, does NOT yield den or add manual:*.
  const repairId = 'legacy-repair1';
  const webId = 'legacy-web1';
  r.legacy({ type: 'legacy', token: repairId, key: 'matrix', resource: 'matrix_output1_input', value: '9',
    managed: true, independent: true, origin: 'repair', actorSource: 'thermite.repair:1a2b3c4d', effectResources: [] });
  assert.equal(r.planner().snapshot().endpoints.den.yielded, false, 'repair must never yield endpoint');
  assert(!('manual:matrix_output1_input' in r.planner().snapshot().holds), 'repair must never leave manual:* pin');
  assert(repairId in r.planner().snapshot().holds, 'repair holds transiently while in flight');
  assert(r.calls.includes('authorize legacy-repair1'));
  r.legacy({ type: 'admitted', token: repairId, key: 'matrix', legacy: true, managed: true, resource: 'matrix_output1_input' });
  r.legacy({ type: 'terminal', token: repairId, key: 'matrix', legacy: true, managed: true, admitted: true });
  assert(!(repairId in r.planner().snapshot().holds));
  // Contrast: a manual web command DOES yield den and sets manual:* pin.
  r.legacy({ type: 'legacy', token: webId, key: 'matrix', resource: 'matrix_output1_input', value: '9',
    managed: true, independent: true, origin: 'web', actorSource: 'thermite.web:1a2b3c4d', effectResources: [] });
  assert.equal(r.planner().snapshot().endpoints.den.yielded, true);
  assert('manual:matrix_output1_input' in r.planner().snapshot().holds);
});

test('loc_<room>_source_lifecycle publishes Stage 2b status, waiting, and reasons', () => {
  const clock = { now: 1000 };
  const dispatches = [];
  const realTable = { ...REAL, lifecycle: { ...REAL.lifecycle, deadlineMs: 10000,
    endpoints: {
      den: { choices: {
        tv: { requirements: { matrix_output1_input: '1' }, uses: [] },
        off: { off: true, release: { matrix_output1_input: '9' } },
      } },
      office: { choices: {
        tv: { requirements: { matrix_output2_input: '1' }, uses: [] },
        off: { off: true, release: { matrix_output2_input: '9' } },
      } },
    } } };
  const r = load({ real: true, clock, table: realTable });
  const statusItem = r.item('loc_den_source_lifecycle', 'NULL');
  // Intercept planner dispatches by spying on deps().
  const origDispatch = r.deps().dispatch, origProbe = r.deps().probe;
  r.deps().dispatch = (op, rep) => { dispatches.push({ op, rep }); origDispatch(op, rep); };
  r.deps().probe = (op, rep) => { dispatches.push({ op, rep }); origProbe(op, rep); };
  r.command('den', 'tv');
  let payload = JSON.parse(statusItem.state);
  assert.equal(payload.status, 'running');
  assert(Array.isArray(payload.waiting) && payload.waiting.length > 0 && payload.waiting[0].startsWith('matrix_output1_input:'));
  assert.deepEqual(payload.reasons, []);
  // Complete probe + write -> status becomes 'done'.
  while (dispatches.length) {
    const { op, rep } = dispatches.shift();
    if (op.kind === 'probe') rep({ type: 'sample', token: op.token, value: '9', fresh: true, retired: true });
    else rep({ type: 'terminal', token: op.token, admitted: true, sample: { value: op.value, fresh: true, retired: true } });
  }
  payload = JSON.parse(statusItem.state);
  assert.equal(payload.status, 'done');
  assert.deepEqual(payload.waiting, []);
  assert.deepEqual(payload.reasons, []);
  // Refused write -> status becomes 'failed' with sorted reasons.
  r.command('den', 'off');
  while (dispatches.length) {
    const { op, rep } = dispatches.shift();
    if (op.kind === 'probe') rep({ type: 'sample', token: op.token, value: '1', fresh: true, retired: true });
    else rep({ type: 'terminal', token: op.token, admitted: true, refused: true, reason: 'device_refused' });
  }
  payload = JSON.parse(statusItem.state);
  assert.equal(payload.status, 'failed');
  assert.deepEqual(payload.reasons, ['device-refused']);
  // Manual takeover -> status becomes 'yielded'.
  r.command('den', 'tv');
  const yieldId = 'legacy-y1';
  r.legacy({ type: 'legacy', token: yieldId, key: 'matrix', resource: 'matrix_output1_input', value: '9',
    managed: true, independent: true, origin: 'web', effectResources: [] });
  payload = JSON.parse(statusItem.state);
  assert.equal(payload.status, 'yielded');
});

test('EDID write is ordered strictly before HDMI output dispatch under lifecycle', () => {
  const events = [];
  const realTable = { ...REAL, lifecycle: { ...REAL.lifecycle,
    endpoints: {
      den: { choices: {
        tv: { requirements: { matrix_output1_input: '1' }, uses: [] },
        off: { off: true, release: { matrix_output1_input: '9' } },
      } },
      office: { choices: {
        tv: { requirements: { matrix_output2_input: '1' }, uses: [] },
        off: { off: true, release: { matrix_output2_input: '9' } },
      } },
    } } };
  const r = load({ real: true, table: realTable });
  const edidItem = r.item('matrix_input1_edid');
  const origSend = edidItem.sendCommand.bind(edidItem);
  edidItem.sendCommand = (v) => { events.push(`edid:${v}`); origSend(v); };
  const origDispatch = r.deps().dispatch, origProbe = r.deps().probe;
  r.deps().dispatch = (op, rep) => { events.push(`dispatch:${op.resource}=${op.value}`); origDispatch(op, rep); };
  r.deps().probe = (op, rep) => { events.push(`probe:${op.resource}`); origProbe(op, rep); };
  r.command('office', 'tv');
  const edidIdx = events.indexOf('edid:6');
  const firstMatrixOp = events.findIndex(e => e.startsWith('probe:matrix_output2_input') || e.startsWith('dispatch:matrix_output2_input'));
  assert(edidIdx >= 0 && firstMatrixOp > edidIdx, `expected EDID before matrix dispatch: ${events.join(', ')}`);
});

test('manual command targeting a settling property is deferred until settled and then authorized', () => {
  const clock = { now: 1000 };
  const ops = [];
  const settleTable = { ...REAL, lifecycle: { ...REAL.lifecycle,
    resources: { ...REAL.lifecycle.resources,
      receiver_power: { key: 'receiver', values: ['ON', 'OFF'], capable: true, settleMs: { ON: 60000, OFF: 30000 } },
    },
    endpoints: {
      den: { choices: {
        tv: { requirements: { receiver_power: 'ON' }, uses: [] },
        off: { off: true, deactivate: { receiver_power: 'OFF' } },
      } },
      office: { choices: { tv: { requirements: { matrix_output2_input: '1' }, uses: [] }, off: { off: true } } },
    } } };
  const r = load({ real: true, clock, table: settleTable });
  r.deps().dispatch = (op, rep) => ops.push({ op, rep });
  r.deps().probe = (op, rep) => ops.push({ op, rep });
  r.command('den', 'tv');
  // Probe OFF -> write Power=ON accepted without readback -> Power is pending settle!
  let next = ops.shift();
  next.rep({ type: 'sample', token: next.op.token, value: 'OFF', fresh: true, retired: true });
  next = ops.shift();
  next.rep({ type: 'terminal', token: next.op.token, admitted: true });
  assert(r.planner().snapshot().resources.receiver_power.pending !== null);
  // Manual OFF arrives while settling: NOT authorized immediately!
  r.calls.length = 0;
  const deferId = 'legacy-defer1';
  r.legacy({ type: 'legacy', token: deferId, key: 'receiver', resource: 'receiver_power', value: 'OFF',
    managed: true, origin: 'web', effectResources: [] });
  assert(!r.calls.includes('authorize legacy-defer1'), 'must hold manual command while property is settling');
  // Advance to settle probe step and return stable ON -> deferred manual command is now authorized!
  r.advance(5000);
  const settleProbe = ops.find(e => e.op.phase === 'settle' && e.op.resource === 'receiver_power');
  assert(settleProbe, 'expected settle probe');
  settleProbe.rep({ type: 'sample', token: settleProbe.op.token, value: 'ON', fresh: true, retired: true });
  assert(r.calls.includes('authorize legacy-defer1'), 'must authorize deferred manual command once settled');
});

process.stdout.write(JSON.stringify({ passed: tests.length, tests }));
