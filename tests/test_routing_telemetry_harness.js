'use strict';
// Routing telemetry: the production routing.js (argv[2]) with a fake clock,
// timers and Items, and the production executor (argv[3], lifecycle_io.js)
// around a fake broker and provider. No device or network access.
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const routingSource = fs.readFileSync(process.argv[2], 'utf8');
const ioSource = fs.readFileSync(process.argv[3], 'utf8');
const tests = [];
function test(name, fn) { fn(); tests.push(name); }

const METRICS = 'house_routing_metrics';
const NAME = /^[a-z][a-z_]{0,31}$/;
const room = (id, label, output) => ({
  item: `loc_${id}_source`, label, hdmi: output,
  choices: { off: [[output, '9']], tv: [[output, '1']] },
  power: { off: [], tv: [[`${id}_tv_power`, 'ON']] },
  inputs: { off: [], tv: [[`${id}_tv_input`, 'hdmi1']] },
  after: { off: [[`${id}_tv_power`, 'OFF']], tv: [] },
  readback: { 9: 'off', 1: 'tv' }, switch: null,
  stages: [{ power: `${id}_tv_power`, input: `${id}_tv_input`, feed: 'hdmi1', readback: {} }],
  diagnosis: { item: `loc_${id}_source_problem`, checks: {} },
});
const ROOMS = { den: room('den', 'Den', 'matrix_output1_input'), office: room('office', 'Office', 'matrix_output2_input') };

// routing.js in a world whose Items change only when a test applies a value.
function load(options = {}) {
  const w = { clock: 1000, items: new Map(), rules: {}, timers: new Map(), serial: 0, logs: [], sent: [], posts: [],
    disposers: [], executorCalls: [] };
  const states = { den_tv_power: 'ON', den_tv_input: 'hdmi1', office_tv_power: 'ON', office_tv_input: 'hdmi1',
    matrix_output1_input: '9', matrix_output2_input: '9', ...(options.states || {}) };
  const names = new Set(Object.keys(states));
  for (const r of Object.values(ROOMS)) names.add(r.item).add(r.diagnosis.item);
  if (options.metrics !== false) names.add(METRICS);
  for (const name of names) {
    w.items.set(name, { state: states[name] || 'NULL',
      sendCommand(value) { w.sent.push([name, String(value)]); },
      postUpdate(value) {
        if (name === METRICS && options.brokenItem) throw new Error('broken metrics Item');
        w.posts.push([name, String(value), w.clock]); this.state = String(value);
      } });
  }
  const table = { rooms: ROOMS, edid: null, off: 'off', holdMs: 15000 };
  if (options.lifecycle) {
    table.lifecycle = { resources: {}, io: { wake: 'house_routing_io_events' }, consumers: [] };
    w.items.set('house_routing_io_events', { state: 'NULL', sendCommand() {}, postUpdate() {} });
  }
  w.counts = { operations: {}, failures: {} };
  const executor = { transaction: fn => fn(), invalidateStale() {}, dispatch() {}, probe() {}, authorizeLegacy() {},
    onLegacy() {}, events() { w.executorCalls.push('events'); },
    telemetry() { if (options.brokenExecutor) throw new Error('broken counts'); return JSON.parse(JSON.stringify(w.counts)); } };
  const lifecycle = { request() {}, observe() {}, snapshot: () => ({ slots: {}, holds: {}, endpoints: {} }), close() {} };
  const modules = {
    openhab: {
      items: { getItem: (name, nullable) => w.items.get(name) || (nullable ? null : assert.fail('no Item ' + name)) },
      rules: { JSRule(rule) { w.rules[rule.id] = rule; } },
      triggers: { ItemCommandTrigger: name => ({ kind: 'command', name }), ItemStateChangeTrigger: name => ({ kind: 'change', name }) },
    },
    'thermite-routing-io': { create: () => executor, nativeJournal: () => ({ load: () => null, save() {}, path: '/userdata/journal.json' }) },
    'thermite-routing-lifecycle': { createCoordinator: () => lifecycle },
    '@runtime': { lifecycleTracker: { addDisposeHook(fn) { w.disposers.push(fn); } } },
  };
  const context = vm.createContext({
    require: name => modules[name] || assert.fail('unexpected module ' + name),
    console: { info: m => w.logs.push(['info', m]), warn: m => w.logs.push(['warn', m]), error: m => w.logs.push(['error', m]) },
    setTimeout: (fn, ms) => { const id = ++w.serial; w.timers.set(id, { at: w.clock + ms, fn }); return id; },
    clearTimeout: id => w.timers.delete(id),
    Date: { now: () => w.clock }, JSON, Map, Set, Object, String, Number, Error, Math, Array,
  });
  vm.runInContext(routingSource.replace('const TABLE = null;', 'const TABLE = ' + JSON.stringify(table) + ';'), context);
  w.command = (id, value) => w.rules[`routing_${id}_command`].execute({ receivedCommand: value });
  // A device shows a value: the rules watching its Item run.
  w.apply = (name, value) => {
    const item = w.items.get(name), previous = item.state;
    if (previous === String(value)) return;
    item.state = String(value);
    for (const rule of Object.values(w.rules)) {
      if (rule.triggers.some(t => t.kind === 'change' && t.name === name)) rule.execute({ itemName: name, oldState: previous, newState: item.state });
    }
  };
  w.advance = (ms) => {
    const until = w.clock + ms;
    for (;;) {
      const due = [...w.timers].filter(([, t]) => t.at <= until).sort((a, b) => a[1].at - b[1].at)[0];
      if (!due) break;
      w.timers.delete(due[0]);
      w.clock = Math.max(w.clock, due[1].at);
      due[1].fn();
    }
    w.clock = until;
  };
  w.published = () => w.posts.filter(([name]) => name === METRICS);
  w.metrics = () => JSON.parse(w.items.get(METRICS).state);
  w.source = id => w.items.get(ROOMS[id].item).state;
  return w;
}

// Publish whatever is outstanding: the throttle's trailing timer.
const flush = w => { w.advance(10000); return w.metrics(); };
const histogram = (buckets, count, sum) => ({ buckets, count, sum });

test('a choice confirmed by its readback is observed once, in its bucket', () => {
  const w = load();
  w.command('den', 'tv');
  w.advance(1500);
  w.apply('matrix_output1_input', '1');
  assert.equal(w.source('den'), 'tv');
  w.apply('den_tv_input', 'hdmi2'); w.apply('den_tv_input', 'hdmi1');  // later readbacks
  const m = flush(w);
  assert.deepEqual(m.route, { den: { confirmed: histogram([0, 1, 1, 1, 1, 1, 1], 1, 1.5) } });
  assert.deepEqual(Object.keys(m).sort(), ['failures', 'operations', 'resends', 'route', 'version']);
  assert.equal(m.version, 1);
});

test('a held choice the readback never shows counts its deadline', () => {
  const w = load({ states: { den_tv_power: 'OFF' } });
  assert.equal(w.source('den'), 'off');
  w.command('den', 'tv');
  w.advance(14999);
  assert.equal(w.timers.size, 1, 'only the hold waits: nothing is observed before the deadline');
  w.advance(1);
  assert.equal(w.source('den'), 'off');
  assert.deepEqual(flush(w).route, { den: { deadline: histogram([0, 0, 0, 0, 1, 1, 1], 1, 15) } });
});

test('a newer choice supersedes, and an immediate readback confirms at once', () => {
  const w = load({ states: { den_tv_power: 'OFF' } });
  w.command('den', 'tv');
  w.advance(3000);
  w.command('den', 'off');
  assert.equal(w.source('den'), 'off');
  assert.deepEqual(flush(w).route, { den: {
    superseded: histogram([0, 0, 1, 1, 1, 1, 1], 1, 3), confirmed: histogram([1, 1, 1, 1, 1, 1, 1], 1, 0) } });
});

test('a choice still open after its deadline counts the deadline when a newer one comes', () => {
  const w = load({ states: { office_tv_input: 'hdmi2' } });
  w.command('office', 'tv');
  w.apply('office_tv_input', 'hdmi1');  // released; the matrix has not switched
  w.advance(40000);
  w.command('office', 'tv');
  w.apply('matrix_output2_input', '1');
  assert.deepEqual(flush(w).route.office, {
    deadline: histogram([0, 0, 0, 0, 1, 1, 1], 1, 15), confirmed: histogram([1, 1, 1, 1, 1, 1, 1], 1, 0) });
});

test('only resend rounds that send are counted', () => {
  const w = load({ states: { den_tv_input: 'hdmi2' } });
  w.command('den', 'tv');
  w.apply('matrix_output1_input', '1');  // held: no readback yet
  w.advance(5000);
  assert(w.sent.filter(([name]) => name === 'den_tv_input').length === 2, 'the 5 s round sends again');
  w.advance(1000);
  w.apply('den_tv_input', 'hdmi1');
  w.advance(30000);
  assert.equal(w.sent.filter(([name]) => name === 'den_tv_input').length, 2, 'later rounds send nothing');
  const m = flush(w);
  assert.deepEqual(m.resends, { den: 1 });
  assert.deepEqual(m.route, { den: { confirmed: histogram([0, 0, 0, 1, 1, 1, 1], 1, 6) } });
});

test('posts only changes, at most once per 10 s, the last change by a timer', () => {
  const w = load();
  assert.deepEqual(w.published().map(([, , at]) => at), [1000], 'the load publishes empty counts');
  assert.deepEqual(w.metrics(), { version: 1, route: {}, operations: {}, failures: {}, resends: {} });
  w.command('den', 'tv'); w.advance(1500); w.apply('matrix_output1_input', '1');
  w.advance(2000);
  w.command('office', 'tv'); w.advance(500); w.apply('matrix_output2_input', '1');
  assert.equal(w.published().length, 1, 'changes inside the window wait');
  assert.equal(w.timers.size, 1, 'one trailing timer');
  w.advance(5999);
  assert.equal(w.published().length, 1);
  w.advance(1);
  assert.deepEqual(w.published().map(([, , at]) => at), [1000, 11000]);
  assert.deepEqual(Object.keys(w.metrics().route).sort(), ['den', 'office']);
  w.advance(20000);
  assert.equal(w.published().length, 2, 'nothing changed, nothing posted');
  w.command('den', 'off');
  assert.equal(w.published().length, 2, 'a choice alone is not yet an observation');
  w.apply('den_tv_power', 'OFF');
  assert.deepEqual(w.published().map(([, , at]) => at), [1000, 11000, 31000], 'a change after a quiet window posts at once');
  assert.deepEqual(w.metrics().route.den.confirmed.count, 2);
  assert.equal(w.timers.size, 0);
});

test('dispose cancels the trailing publish', () => {
  const w = load();
  w.command('den', 'tv'); w.apply('matrix_output1_input', '1');
  assert.equal(w.timers.size, 1);
  w.disposers.forEach(fn => fn());
  assert.equal(w.timers.size, 0);
  w.advance(60000);
  assert.equal(w.published().length, 1);
});

test('executor counts are published from the events rule', () => {
  const w = load({ lifecycle: true });
  w.counts = { operations: { applied: 2, unsent: 1 }, failures: { matrix: { unsent: 1 } } };
  w.advance(10000);
  w.rules.routing_lifecycle_events.execute({});
  assert.deepEqual(w.executorCalls.slice(-1), ['events']);
  const m = w.metrics();
  assert.deepEqual(m.operations, { applied: 2, unsent: 1 });
  assert.deepEqual(m.failures, { matrix: { unsent: 1 } });
});

test('a failing telemetry path never breaks routing', () => {
  for (const options of [{ brokenItem: true }, { brokenExecutor: true, lifecycle: true }]) {
    const w = load(options);
    w.command('den', 'tv'); w.advance(1500); w.apply('matrix_output1_input', '1');
    assert.equal(w.source('den'), 'tv', JSON.stringify(options));
    assert(w.sent.some(([name, value]) => name === 'matrix_output1_input' && value === '1') || options.lifecycle);
    if (options.lifecycle) w.rules.routing_lifecycle_events.execute({});
    w.advance(30000); w.command('den', 'off'); w.apply('den_tv_power', 'OFF');
    assert.equal(w.source('den'), 'off');
    assert.equal(w.logs.filter(([level, m]) => level === 'warn' && /telemetry/.test(m)).length, 1, 'logged once');
    assert(!w.logs.some(([, m]) => /broken/.test(m)), 'no exception text in the log');
  }
  const w = load({ metrics: false });
  w.command('den', 'tv'); w.apply('matrix_output1_input', '1');
  assert.equal(w.source('den'), 'tv', 'a house without the Item still routes');
  assert.equal(w.logs.filter(([level]) => level === 'warn').length, 0);
});

// The executor: one controller and actor over a fake broker.
class NativeMap extends Map { get(key) { return super.has(key) ? super.get(key) : null; } put(key, value) { this.set(key, value); } }
class Queue {
  constructor() { this.rows = []; }
  offer(value) { if (this.rows.length >= 512) return false; this.rows.push(value); return true; }
  poll() { return this.rows.length ? this.rows.shift() : null; }
  isEmpty() { return this.rows.length === 0; }
  size() { return this.rows.length; }
}
const counter = () => ({ n: 0, get() { return this.n; }, incrementAndGet() { return ++this.n; }, decrementAndGet() { return --this.n; } });
function executor() {
  const broker = { states: new NativeMap(), events: new Queue(), active: counter(),
    eventWake: { value: false, set(v) { this.value = v; }, compareAndSet(a, b) { if (this.value !== a) return false; this.value = b; return true; } },
    overflow: { value: false, get() { return this.value; }, set(v) { this.value = v; } }, lock: { lock() {}, unlock() {} } };
  const descriptor = { io: { epoch: 'config-1', wake: 'events', actors: { switch: { wake: 'switchWake', resources: ['D1', 'D2'] },
    other: { wake: 'otherWake', resources: ['P'] } } } };
  const context = { module: { exports: {} }, require() { throw Error('unexpected native dependency'); }, Date, JSON, Map, Object, String, Error };
  vm.runInNewContext(ioSource, context);
  const module = context.module.exports;
  const f = { time: 1000, serial: 0, failJournal: false, reports: [], module, broker, descriptor };
  const log = { error() {}, warn() {}, info() {} };
  f.env = { broker, noRules: true, wake() {}, now: () => f.time, id: () => String(++f.serial), log, sleep() {} };
  f.bridge = module.create(descriptor, f.env);
  f.bridge.onLegacy(e => { if (e.type === 'legacy') f.bridge.authorizeLegacy(e.token); });
  f.journal = { data: null, load() { return this.data; }, save(row) { if (f.failJournal) throw Error('disk'); this.data = JSON.parse(JSON.stringify(row)); } };
  f.provider = { has: (r, v) => ['D1', 'D2'].includes(r) && ['1', '2', '3', '4'].includes(String(v)),
    execute: (r, v) => ({ outcome: 'applied', sample: { value: String(v), fresh: true } }), probe: () => ({ value: '3', fresh: true }) };
  f.actor = module.actor({ key: 'switch', io: descriptor.io }, f.provider, { ...f.env, journal: f.journal });
  f.submit = (token, resource, value = '3') => f.bridge.dispatch({ token, key: 'switch', resource, value, phase: 'acquire' }, r => f.reports.push(r));
  f.run = () => { f.actor.drain(); f.drain(); };
  f.drain = () => { do f.bridge.events(); while (!broker.events.isEmpty()); };
  return f;
}

test('each executor operation counts once at its final outcome, failures by device', () => {
  const f = executor();
  f.drain();
  f.submit('applied', 'D1'); f.run();
  f.submit('old', 'D1', '2'); f.submit('new', 'D1', '3'); f.run();
  f.provider.execute = () => ({ outcome: 'unsent' }); f.submit('offline', 'D1'); f.run();
  f.provider.execute = () => ({ outcome: 'refused' }); f.submit('declined', 'D2'); f.run();
  f.bridge.probe({ token: String('read'), key: 'switch', resource: 'D1' }, r => f.reports.push(r)); f.run();
  f.provider.probe = () => { throw Error('standby'); };
  f.bridge.probe({ token: String('unread'), key: 'switch', resource: 'D1' }, r => f.reports.push(r)); f.run();
  f.failJournal = true; f.submit('disk', 'D1'); f.run(); f.failJournal = false;
  f.actor.legacy('D2', '4'); f.drain(); f.provider.execute = () => ({ outcome: 'applied' }); f.run();
  const readonly = f.module.actor({ key: 'other', io: f.descriptor.io }, f.provider, { ...f.env, readonly: true, journal: { load: () => null, save() {} } });
  f.bridge.dispatch({ token: String('ro'), key: 'other', resource: 'P', value: 'ON' }, r => f.reports.push(r)); readonly.drain(); f.drain();
  f.provider.execute = () => { throw Error('lost reply'); }; f.submit('lost', 'D1'); f.run();
  f.submit('fenced', 'D2'); f.run();
  const counts = f.bridge.telemetry();
  assert.deepEqual(counts.operations, { applied: 3, superseded: 1, unsent: 1, refused: 1, sampled: 1, probe_failed: 1,
    journal_failed: 1, read_only: 1, unknown: 1, quarantined: 1 });
  assert.deepEqual(counts.failures, { switch: { unsent: 1, refused: 1, probe_failed: 1, journal_failed: 1, unknown: 1, quarantined: 1 } });
  const final = f.reports.filter(r => ['terminal', 'sample', 'uncertain'].includes(r.type)).length;
  const legacy = 1;
  assert.equal(Object.values(counts.operations).reduce((a, b) => a + b, 0), final + legacy, 'one count per final report');
  f.drain(); f.drain();
  assert.deepEqual(f.bridge.telemetry(), counts, 'idle polling counts nothing again');
  // A failed recovery read is the device's; the note never overflows the queue.
  f.time += 60000;
  f.actor.recover(); f.drain();
  assert.deepEqual(f.bridge.telemetry().failures.switch.recovery_failed, 1);
  for (let n = 0; n < 512; n += 1) f.broker.events.offer(JSON.stringify({ type: 'ready', key: 'nobody' }));
  f.actor.recover();
  assert.equal(f.broker.overflow.get(), false, 'a full queue drops the note, not routing');
  f.drain();
  assert.equal(f.bridge.telemetry().failures.switch.recovery_failed, 1);
  const json = JSON.stringify(f.bridge.telemetry());
  for (const secret of ['"old"', 'lost', 'fenced', 'D1', 'D2', 'standby', 'disk', 'reply']) assert(!json.includes(secret), secret);
});

test('reloads, supersessions and overflow revoke without double counting', () => {
  const f = executor();
  f.drain();
  // A manual command replaced before admission, then the controller reloads.
  f.actor.legacy('D1', '2'); f.actor.legacy('D1', '3');
  const next = f.module.create(f.descriptor, f.env);
  next.onLegacy(() => {});
  do next.events(); while (!f.broker.events.isEmpty());
  assert.deepEqual(next.telemetry().operations, { superseded: 1, revoked: 1 });
  assert.deepEqual(f.bridge.telemetry().operations, {}, 'a replaced controller counts nothing');
  // Overflow: live tickets are reported uncertain once, unadmitted work revoked.
  const g = executor();
  g.drain();
  g.submit('lost', 'D1'); g.broker.overflow.set(true); g.drain();
  g.actor.drain(); g.drain();
  assert.deepEqual(g.bridge.telemetry().operations, { unknown: 1 });
  assert.deepEqual(g.bridge.telemetry().failures, {}, 'the queue overflowed, not the device');
});

test('outcome and reason names are fixed, few and exporter-safe', () => {
  const f = executor();
  const { OPERATION_OUTCOMES, FAILURE_REASONS } = f.module.telemetryNames;
  for (const names of [OPERATION_OUTCOMES, FAILURE_REASONS]) {
    assert(Object.isFrozen(names));
    assert(names.length <= 12);
    assert.equal(new Set(names).size, names.length);
    assert(names.every(name => NAME.test(name)), names.join());
  }
  assert.deepEqual([...OPERATION_OUTCOMES], ['applied', 'sampled', 'probe_failed', 'unsent', 'refused', 'unknown',
    'quarantined', 'superseded', 'revoked', 'journal_failed', 'read_only', 'other']);
  assert.deepEqual([...FAILURE_REASONS], ['unsent', 'refused', 'unknown', 'quarantined', 'probe_failed', 'journal_failed',
    'recovery_failed']);
  // An unrecognized terminal reason is `other`, never its text.
  const owner = JSON.parse(f.broker.states.get('@controller')).owner;
  f.bridge.onLegacy(() => {});
  f.broker.events.offer(JSON.stringify({ type: 'terminal', legacy: true, owner, key: 'switch', token: String('x'), reason: 'Some Device Text' }));
  f.broker.events.offer(JSON.stringify({ type: 'terminal', legacy: true, owner, key: 'unknown_device', token: String('y'), reason: 'device_refused' }));
  f.broker.events.offer(JSON.stringify({ type: 'telemetry', key: 'switch', reason: 'free text' }));
  f.drain();
  assert.deepEqual(f.bridge.telemetry(), { operations: { other: 1 }, failures: {} });
});

process.stdout.write(JSON.stringify({ passed: tests.length, tests }));
