'use strict';
// Runs the production thingHandler telemetry rule against stub openHAB and
// Java types. argv: threadpools.js. Each item change is one events.log line.
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const source = fs.readFileSync(process.argv[2], 'utf8');
const tests = [];
function test(name, fn) { fn(); tests.push(name); }
const VALUES = ['active', 'core', 'queued', 'overdue'];

function load() {
  // openHAB starts every item NULL.
  const items = Object.fromEntries([...VALUES, 'observed'].map(point => [`thermite_handler_${point}`, 'NULL']));
  const changes = [], stamped = new Map(), rules = {}, hooks = [];
  const pool = { exists: true, active: 0, core: 32, queued: 0, fail: false, probe: null };
  const clock = { now: 1_700_000_000_000 };
  // Whenever observed holds a time, every value item holds what that sample
  // posted. Recorded, not thrown: the rule's own catch would swallow it.
  const mixed = [];
  const consistent = () => {
    const observed = items.thermite_handler_observed;
    if (observed === 'NULL' || observed === 'UNDEF') return;
    const values = VALUES.map(point => items[`thermite_handler_${point}`]);
    if (JSON.stringify(values) !== JSON.stringify(stamped.get(observed))) mixed.push([observed, values]);
  };
  const openhab = {
    items: { getItem: (name, nullable) => ({ postUpdate(value) {
      assert.equal(nullable, true);
      if (items[name] !== value) changes.push([name, value]);
      items[name] = value;
      if (name === 'thermite_handler_observed' && value !== 'UNDEF') stamped.set(value, VALUES.map(point => items[`thermite_handler_${point}`]));
      consistent();
    } }) },
    rules: { JSRule(rule) { rules[rule.id] = rule; } },
    triggers: { GenericCronTrigger: cron => ({ cron }) },
  };
  const future = () => ({ done: false, cancelled: false, delay: 0,
    isDone() { return this.done; }, getDelay() { return this.delay; }, cancel() { this.cancelled = true; } });
  const types = {
    'org.openhab.core.common.ThreadPoolManager': {
      getPoolNames: () => ({ contains: name => name === 'thingHandler' && pool.exists }),
      getScheduledPool: () => ({
        schedule: () => (pool.probe = future()),
        getActiveCount: () => { if (pool.fail) throw new Error('fixture failure'); return pool.active; },
        getCorePoolSize: () => pool.core, getQueue: () => ({ size: () => pool.queued }),
      }),
    },
    'java.util.concurrent.TimeUnit': { MILLISECONDS: 'ms' },
    'com.google.common.util.concurrent.Runnables': { doNothing: () => ({}) },
  };
  const context = vm.createContext({
    require: name => name === 'openhab' ? openhab
      : name === '@runtime' ? { lifecycleTracker: { addDisposeHook: hook => hooks.push(hook) } }
        : assert.fail('unexpected module ' + name),
    Java: { type: name => types[name] || assert.fail('unexpected Java type ' + name) },
    Date: { now: () => clock.now }, Object, String, Number, Math, Error,
  });
  vm.runInContext(source, context);
  const rule = rules.thermite_handler_pool;
  assert.equal(rule.triggers[0].cron, '0/15 * * * * ?');
  return {
    items, pool, hooks,
    sample() {
      const before = changes.length;
      clock.now += 15_000; rule.execute();
      assert.deepEqual(mixed, [], 'a scrape could read mixed values under a current observed time');
      return changes.slice(before);
    },
    loaded: changes.slice(),
  };
}

test('the first sample brackets its values and then stamps them', () => {
  const r = load();
  assert.deepEqual(r.loaded.map(([name, value]) => name.replace('thermite_handler_', '') + (value === 'UNDEF' ? ':UNDEF' : '')),
    ['observed:UNDEF', 'active', 'core', 'queued', 'overdue', 'observed']);
  assert.equal(r.items.thermite_handler_core, '32');
  assert.equal(r.items.thermite_handler_observed, String(1_700_000_000));
});

test('a steady pool changes only observed: one events.log line per sample', () => {
  const r = load();
  r.pool.probe.done = true;
  for (let i = 0; i < 4; i += 1) {
    const changes = r.sample();
    assert.deepEqual(changes, [['thermite_handler_observed', r.items.thermite_handler_observed]]);
    assert.notEqual(changes[0][1], 'UNDEF');
  }
});

test('a changed value withdraws observed before any value moves', () => {
  const r = load();
  r.pool.probe.done = true;
  r.sample();
  r.pool.active = 3; r.pool.queued = 7;
  assert.deepEqual(r.sample().map(([name, value]) => name.replace('thermite_handler_', '') + (value === 'UNDEF' ? ':UNDEF' : '')),
    ['observed:UNDEF', 'active', 'queued', 'observed']);
  // A stalled probe reports how long ready work has waited.
  r.pool.probe.delay = -2500;
  assert.deepEqual(r.sample().map(([name]) => name), ['thermite_handler_observed', 'thermite_handler_overdue', 'thermite_handler_observed']);
  assert.equal(r.items.thermite_handler_overdue, '2.5');
});

test('a missing pool, invalid counts or a failure leave observed undefined', () => {
  const r = load();
  r.pool.probe.done = true;
  r.pool.exists = false;
  assert.deepEqual(r.sample(), [['thermite_handler_observed', 'UNDEF']]);
  assert.deepEqual(r.sample(), []);
  r.pool.exists = true;
  // Values are unchanged, but no time vouches for them until this sample does.
  assert.deepEqual(r.sample().map(([name]) => name), ['thermite_handler_observed']);
  r.pool.core = 0;
  assert.deepEqual(r.sample(), [['thermite_handler_observed', 'UNDEF']]);
  r.pool.core = 32; r.pool.fail = true;
  assert.deepEqual(r.sample(), []);
  assert.equal(r.items.thermite_handler_observed, 'UNDEF');
  r.pool.fail = false;
  assert.notEqual(r.sample()[0][1], 'UNDEF');
});

test('disposal cancels the probe, withdraws observed and stops sampling', () => {
  const r = load();
  const probe = r.pool.probe;
  r.hooks.forEach(hook => hook());
  assert.equal(probe.cancelled, true);
  assert.equal(r.items.thermite_handler_observed, 'UNDEF');
  assert.deepEqual(r.sample(), []);
});

process.stdout.write(JSON.stringify({ passed: tests.length, tests }));
