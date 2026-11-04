'use strict';
// Zone sync end to end: the production routing.js with the TABLEs that
// routing.table() renders for the synthetic house in test_zone_sync.py (on
// stdin), the production lifecycle planner, and a fake ordered executor whose
// device model applies admitted writes and answers owned reads. A device
// change fires the rules that watch its Item. No device or network access.
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const source = fs.readFileSync(process.argv[2], 'utf8');
const planner = require(process.argv[3]);
const TABLES = JSON.parse(fs.readFileSync(0, 'utf8'));
const tests = [];
function test(name, fn) { tests.push([name, fn]); }

const CINEMA = 'example_cinema', LOUNGE = 'example_lounge';
const COMPUTER = 'example_computer', DISC = 'example_disc', GAME = 'example_game';
const OUT1 = 'example_matrix_output1_input', OUT2 = 'example_matrix_output2_input';
const ZONE2 = 'example_avr_zone2_input';
const SYNCED = choice => `${choice}@${CINEMA}`;
// Everything off: outputs parked, stages off on their matrix feeds.
const OFF = { [OUT1]: '8', [OUT2]: '8', example_usb_device1_host: '4', example_usb_device2_host: '4',
  example_avr_main_power: 'OFF', example_avr_main_input: 'BD', example_avr_zone2_power: 'OFF', [ZONE2]: 'AUX2',
  example_screen_power: 'OFF', example_screen_input: 'hdmi1', example_tv_power: 'OFF', example_tv_input: 'hdmi2' };

function names(table) {
  const out = new Set(Object.keys(OFF));
  for (const [id, room] of Object.entries(table.rooms)) {
    out.add(room.item).add(room.diagnosis.item).add(`loc_${id}_source_lifecycle`);
    for (const field of ['choices', 'power', 'inputs', 'after'])
      for (const steps of Object.values(room[field])) for (const [name] of steps) out.add(name);
  }
  for (const name of Object.keys((table.edid || {}).inputs || {})) out.add(table.edid.inputs[name]);
  return out;
}

function load(table, options = {}) {
  const world = { clock: 1000, items: new Map(), rules: [], timers: new Map(), serial: 0, log: [], logs: [], sent: [],
    writes: [], effects: [], pending: [], tx: 0, depth: 0, journal: options.journal || { data: null } };
  const state = (name) => (world.items.has(name) ? world.items.get(name).state : 'NULL');
  const fire = (name, previous, value) => {
    for (const rule of world.rules.slice()) {
      if (rule.triggers.some((t) => t.kind === 'change' && t.name === name)) {
        rule.execute({ itemName: name, oldState: previous, newState: value });
      }
    }
  };
  // A device shows a value: its Item changes and the rules watching it run.
  world.apply = (name, value) => {
    const item = world.items.get(name);
    const previous = item.state;
    if (previous === String(value)) return;
    item.state = String(value);
    world.log.push(`apply ${name}=${value}`);
    fire(name, previous, item.state);
  };
  for (const name of names(table)) {
    world.items.set(name, { name, state: has(options.states || {}, name) ? options.states[name] : has(OFF, name) ? OFF[name] : 'NULL',
      sendCommand(value) {
        world.sent.push([name, String(value)]); world.log.push(`send ${name}=${value}`);
        if (has(OFF, name)) world.effects.push([name, String(value)]);
      },
      postUpdate(value) { this.state = String(value); } });
  }
  let coordinator = null;
  const executor = {
    transaction(fn) {
      if (world.depth++ === 0) world.tx += 1;
      try { return fn(); } finally { world.depth -= 1; }
    },
    invalidateStale() {
      world.pending = world.pending.filter((entry) => {
        if (entry.operation.isCurrent()) return true;
        world.log.push(`revoke ${entry.operation.resource}`);
        entry.report({ type: 'terminal', token: entry.operation.token, admitted: false, reason: 'superseded_before_admission' });
        return false;
      });
    },
    dispatch(operation, report) {
      world.log.push(`dispatch ${operation.resource}=${operation.value}`);
      world.pending.push({ operation, report, write: true });
    },
    probe(operation, report) { world.pending.push({ operation, report, write: false }); },
    onLegacy(callback) { world.legacy = callback; }, events() {}, authorizeLegacy() {},
  };
  const lifecycle = {
    createCoordinator(descriptor, dependencies) {
      coordinator = planner.createCoordinator(descriptor, dependencies);
      const request = coordinator.request;
      coordinator.request = (id, choice) => {
        world.log.push(`request ${id} ${choice} tx${world.tx}`);
        if (options.refuse && options.refuse[id] === choice) {
          throw new Error(`lifecycle resource conflict: ${OUT1} (${CINEMA},${LOUNGE})`);
        }
        return request(id, choice);
      };
      return coordinator;
    },
  };
  const modules = {
    openhab: {
      items: { getItem: (name, nullable) => world.items.get(name) || (nullable ? null : assert.fail('no Item ' + name)) },
      rules: { JSRule(rule) { world.rules.push(rule); } },
      triggers: { ItemCommandTrigger: (name) => ({ kind: 'command', name }), ItemStateChangeTrigger: (name) => ({ kind: 'change', name }) },
    },
    'thermite-routing-io': { create: () => executor,
      nativeJournal: () => ({ load: () => world.journal.data, save(value) { world.journal.data = JSON.parse(JSON.stringify(value)); },
        path: '/userdata/thermite-routing/executor_coordinator.json' }) },
    'thermite-routing-lifecycle': lifecycle,
    '@runtime': { lifecycleTracker: { addDisposeHook() {} } },
  };
  const context = vm.createContext({
    require: (name) => modules[name] || assert.fail('unexpected module ' + name),
    console: { info: (m) => world.logs.push(['info', m]), warn: (m) => world.logs.push(['warn', m]), error: (m) => world.logs.push(['error', m]) },
    setTimeout: (fn, ms) => { const id = ++world.serial; world.timers.set(id, { at: world.clock + ms, fn }); return id; },
    clearTimeout: (id) => world.timers.delete(id),
    Date: { now: () => world.clock }, JSON, Map, Set, Object, String, Number, Error, Math, Array,
  });
  vm.runInContext(source.replace('const TABLE = null;', 'const TABLE = ' + JSON.stringify(table) + ';'), context);
  world.coordinator = () => coordinator;
  world.state = state;
  world.command = (room, value) => {
    for (const rule of world.rules.slice()) {
      if (rule.triggers.some((t) => t.kind === 'command' && t.name === `loc_${room}_source`)) rule.execute({ receivedCommand: value });
    }
  };
  // Devices act: commands sent take effect, admitted writes apply and reads
  // answer, in order, until nothing is left.
  world.settle = () => {
    for (let step = 0; step < 2000; step += 1) {
      if (world.effects.length) { world.apply(...world.effects.shift()); continue; }
      const entry = world.pending.shift();
      if (!entry) return world;
      const { operation, report } = entry;
      if (!operation.isCurrent()) {
        report({ type: 'terminal', token: operation.token, admitted: false, reason: 'superseded_before_admission' });
        continue;
      }
      report({ type: 'admitted', token: operation.token });
      if (!entry.write) {
        report({ type: 'sample', token: operation.token, value: state(operation.resource), fresh: true, retired: true });
        continue;
      }
      world.writes.push(`${operation.resource}=${operation.value}`);
      world.apply(operation.resource, operation.value);
      report({ type: 'terminal', token: operation.token, sample: { value: operation.value, fresh: true, retired: true } });
    }
    throw new Error('the house did not settle');
  };
  world.advance = (ms) => {
    world.clock += ms;
    for (const [id, timer] of [...world.timers].sort((a, b) => a[1].at - b[1].at)) {
      if (timer.at <= world.clock && world.timers.delete(id)) timer.fn();
    }
  };
  world.source = (room) => state(`loc_${room}_source`);
  world.clear = () => { world.log.length = 0; world.logs.length = 0; world.sent.length = 0; world.writes.length = 0; };
  world.requests = () => world.log.filter((line) => line.startsWith('request '));
  // The requests, without their transaction: for checks across a settle.
  world.asked = () => world.requests().map((line) => line.replace(/ tx\d+$/, ''));
  world.endpoint = (room) => coordinator.snapshot().endpoints[room];
  return world;
}

function has(object, key) { return Object.prototype.hasOwnProperty.call(object, key); }

// Cinema on `choice`, then the lounge on `lounge`, each settled.
function both(choice, lounge = choice, options = {}) {
  const w = load(TABLES.lifecycle, options);
  w.command(CINEMA, choice); w.settle();
  w.command(LOUNGE, lounge); w.settle();
  w.clear();
  return w;
}

test('(1) a follower choosing the source its leader shows takes the leader input', () => {
  const w = load(TABLES.lifecycle);
  w.command(CINEMA, COMPUTER); w.settle();
  w.clear();
  w.command(LOUNGE, COMPUTER);
  assert.deepEqual(w.requests(), [`request ${LOUNGE} ${SYNCED(COMPUTER)} tx${w.tx}`]);
  w.settle();
  assert.equal(w.state(ZONE2), 'BD');
  assert.equal(w.state(OUT2), '1', 'its own output stays on the source: hot standby');
  assert(!w.writes.some((write) => write.startsWith(OUT1)), 'the follower never writes the leader output');
  assert.equal(w.source(LOUNGE), COMPUTER);
  assert.equal(w.endpoint(LOUNGE).leases[OUT1], '1');
  assert(w.endpoint(LOUNGE).pathReady);
});

test('(2) break before: a leader move unsyncs its follower first, in one transaction', () => {
  const w = both(COMPUTER);
  w.command(CINEMA, DISC);
  assert.deepEqual(w.requests(), [`request ${LOUNGE} ${COMPUTER} tx${w.tx}`, `request ${CINEMA} ${DISC} tx${w.tx}`]);
  assert.deepEqual(w.logs.filter(([level]) => level === 'warn'), []);
  w.settle();
  assert.deepEqual(w.writes.filter((write) => write.startsWith(OUT1) || write.startsWith(ZONE2)).sort(),
    [`${OUT1}=2`, `${ZONE2}=AUX2`].sort(), 'the leader output goes to the disc, never parked between');
  assert.equal(w.state(OUT2), '1');
  assert.equal(w.source(LOUNGE), COMPUTER);
  assert.equal(w.source(CINEMA), DISC);
  assert.equal(w.endpoint(LOUNGE).choice, COMPUTER);
  w.clear();
  w.command(LOUNGE, DISC);
  assert.deepEqual(w.requests(), [`request ${LOUNGE} ${SYNCED(DISC)} tx${w.tx}`]);
  w.settle();
  assert.equal(w.state(ZONE2), 'BD');
  assert.equal(w.source(LOUNGE), DISC);
});

test('(3) leader Off: its follower unsyncs first, and the output is parked once both let go', () => {
  const w = both(COMPUTER);
  w.command(CINEMA, 'off');
  assert.deepEqual(w.requests(), [`request ${LOUNGE} ${COMPUTER} tx${w.tx}`, `request ${CINEMA} off tx${w.tx}`]);
  const parked = w.log.indexOf(`dispatch ${OUT1}=8`);
  assert(parked === -1 || parked > w.log.indexOf(`request ${CINEMA} off tx${w.tx}`));
  w.settle();
  assert.equal(w.writes.filter((write) => write === `${OUT1}=8`).length, 1);
  assert.equal(w.state(ZONE2), 'AUX2');
  assert.equal(w.source(LOUNGE), COMPUTER);
  assert.equal(w.source(CINEMA), 'off');
});

test('(4) make after: a follower syncs only once the leader output shows the shared choice', () => {
  const w = load(TABLES.lifecycle);
  w.command(LOUNGE, COMPUTER); w.settle();
  assert.equal(w.state(ZONE2), 'AUX2');
  w.clear();
  w.command(CINEMA, COMPUTER);
  assert.deepEqual(w.requests(), [`request ${CINEMA} ${COMPUTER} tx${w.tx}`], 'not while the output still shows Off');
  w.settle();
  const shown = w.log.indexOf(`apply ${OUT1}=1`);
  const synced = w.log.findIndex((line) => line.startsWith(`request ${LOUNGE} ${SYNCED(COMPUTER)}`));
  assert(shown >= 0 && synced > shown, w.log.join('\n'));
  assert.equal(w.state(ZONE2), 'BD');
  assert.equal(w.source(LOUNGE), COMPUTER);
  // Leader mid change: its output still shows the computer, its choice is
  // the disc; a follower choosing the computer stays on its own route.
  w.command(CINEMA, DISC);
  w.command(LOUNGE, COMPUTER);
  assert.equal(w.endpoint(LOUNGE).choice, COMPUTER);
  w.settle();
  assert.equal(w.state(ZONE2), 'AUX2');
});

test('(5) a leader output changed elsewhere breaks the sync; an unknown one does not', () => {
  const w = both(COMPUTER);
  w.apply(OUT1, 'UNDEF');
  assert.deepEqual(w.requests(), []);
  w.apply(OUT1, '1');
  w.apply(OUT1, '2');
  assert.deepEqual(w.requests(), [`request ${LOUNGE} ${COMPUTER} tx${w.tx}`]);
  w.settle();
  assert.equal(w.state(ZONE2), 'AUX2');
  assert.equal(w.source(LOUNGE), COMPUTER);
  w.clear();
  w.apply(OUT1, '1');
  assert.deepEqual(w.requests(), [`request ${LOUNGE} ${SYNCED(COMPUTER)} tx${w.tx}`], 'the leader still chose it');
});

test('(6) a follower on its leader input reads its source from the leader output', () => {
  const on = { example_avr_zone2_power: 'ON', example_tv_power: 'ON' };
  const check = (states, expected, problem) => {
    const w = load(TABLES.lifecycle, { states: { ...OFF, ...on, ...states } });
    assert.equal(w.source(LOUNGE), expected, JSON.stringify(states));
    const diagnosis = w.state(`loc_${LOUNGE}_source_problem`);
    assert.equal(diagnosis === 'UNDEF' ? null : JSON.parse(diagnosis).item, problem);
  };
  check({ [ZONE2]: 'BD', [OUT1]: '2', [OUT2]: '1' }, DISC, null);
  check({ [ZONE2]: 'GAME1', [OUT1]: '3' }, GAME, null);
  check({ [ZONE2]: 'BD', [OUT1]: '3' }, 'UNDEF', ZONE2);
  check({ [ZONE2]: 'BD', [OUT1]: '8' }, 'UNDEF', OUT1);
  check({ [ZONE2]: 'BD', [OUT1]: '2', example_tv_input: 'hdmi1' }, 'UNDEF', 'example_tv_input');
  check({ [ZONE2]: 'AUX2', [OUT1]: '2', [OUT2]: '1' }, COMPUTER, null);
});

test('(7) without the planner, the zone input is sent: break before the leader, make after', () => {
  const w = load(TABLES.plain);
  w.command(LOUNGE, COMPUTER); w.settle();
  w.command(CINEMA, COMPUTER);
  assert(!w.sent.some(([name, value]) => name === ZONE2 && value === 'BD'), 'not before the output shows it');
  w.settle();
  assert.equal(w.state(ZONE2), 'BD');
  assert.equal(w.source(LOUNGE), COMPUTER);
  w.clear();
  w.command(CINEMA, DISC);
  const zone = w.sent.findIndex(([name, value]) => name === ZONE2 && value === 'AUX2');
  const output = w.sent.findIndex(([name, value]) => name === OUT1 && value === '2');
  assert(zone >= 0 && zone < output, JSON.stringify(w.sent));
  w.settle();
  assert.equal(w.source(LOUNGE), COMPUTER);
  w.clear();
  w.command(LOUNGE, DISC);
  assert(w.sent.some(([name, value]) => name === ZONE2 && value === 'BD'));
  w.settle();
  assert.equal(w.source(LOUNGE), DISC);
});

test('a refused leader claim puts its followers back before anything leaves the transaction', () => {
  const w = both(COMPUTER, COMPUTER, { refuse: { [CINEMA]: DISC } });
  w.command(CINEMA, DISC);
  assert.deepEqual(w.requests(), [`request ${LOUNGE} ${COMPUTER} tx${w.tx}`, `request ${CINEMA} ${DISC} tx${w.tx}`,
    `request ${LOUNGE} ${SYNCED(COMPUTER)} tx${w.tx}`]);
  assert(w.logs.some(([level, message]) => level === 'warn'
    && message === `Example Cinema: ${DISC} was not routed: ${OUT1} is held by Example Cinema and Example Lounge`));
  w.settle();
  assert.deepEqual(w.writes, []);
  assert(!w.sent.some(([name]) => name.endsWith('_edid')));
  assert.equal(w.state(ZONE2), 'BD');
  assert.equal(w.endpoint(LOUNGE).choice, SYNCED(COMPUTER));
  assert.equal(w.source(LOUNGE), COMPUTER);
  // The lounge is still synced: the next leader move breaks it first.
  w.command(CINEMA, GAME);
  assert.equal(w.requests()[3], `request ${LOUNGE} ${COMPUTER} tx${w.tx}`);
});

test('a refused sync is logged and leaves the follower on its own route', () => {
  const w = load(TABLES.lifecycle, { refuse: { [LOUNGE]: SYNCED(DISC) } });
  w.command(LOUNGE, DISC); w.settle();
  w.command(CINEMA, DISC); w.settle();
  assert(w.logs.some(([level, message]) => level === 'warn'
    && message === `Example Lounge: ${DISC} was not synced with Example Cinema: ${OUT1} is held by Example Cinema and Example Lounge`));
  assert.equal(w.state(ZONE2), 'AUX2');
  assert.equal(w.endpoint(LOUNGE).choice, DISC);
  assert.equal(w.source(LOUNGE), DISC);
});

test('after a reload the planner journal says who is synced, so the next leader move breaks first', () => {
  const journal = { data: null };
  const before = both(COMPUTER, COMPUTER, { journal });
  const saved = JSON.stringify(journal.data);
  const states = Object.fromEntries([...before.items].map(([name, item]) => [name, item.state]));
  const w = load(TABLES.lifecycle, { journal: { data: JSON.parse(saved) }, states });
  w.command(CINEMA, DISC);
  assert.deepEqual(w.requests(), [`request ${LOUNGE} ${COMPUTER} tx${w.tx}`, `request ${CINEMA} ${DISC} tx${w.tx}`]);
  assert.deepEqual(w.logs.filter(([level]) => level === 'warn'), []);
  // A synced follower whose zone reads nothing has left its route as far as
  // the rule knows: its lease ends without a write, and the leader moves.
  const unread = load(TABLES.lifecycle, { journal: { data: JSON.parse(saved) }, states: { ...states, [ZONE2]: 'NULL' } });
  unread.command(CINEMA, DISC);
  assert.deepEqual(unread.requests(), [`request ${CINEMA} ${DISC} tx${unread.tx}`]);
  assert.deepEqual(unread.logs.filter(([level]) => level === 'warn'), []);
  assert(unread.endpoint(LOUNGE).yielded);
});

test('a synced follower turned off at its own devices is never turned back on by its leader', () => {
  const w = both(COMPUTER);
  w.apply('example_tv_power', 'OFF');
  assert.equal(w.source(LOUNGE), 'off');
  w.command(CINEMA, DISC);
  assert.deepEqual(w.requests(), [`request ${CINEMA} ${DISC} tx${w.tx}`]);
  assert.deepEqual(w.logs.filter(([level]) => level === 'warn'), []);
  w.settle();
  assert(w.endpoint(LOUNGE).yielded);
  assert(!w.writes.some((write) => write.startsWith('example_tv_') || write.startsWith('example_avr_zone2')), w.writes.join());
  w.clear();
  w.command(CINEMA, COMPUTER); w.settle();
  assert.deepEqual(w.asked(), [`request ${CINEMA} ${COMPUTER}`]);
  assert.equal(w.state('example_tv_power'), 'OFF');
});

test('a follower turned off at its own devices is not synced either', () => {
  const w = load(TABLES.lifecycle);
  w.command(LOUNGE, COMPUTER); w.settle();
  w.apply('example_tv_power', 'OFF');
  w.clear();
  w.command(CINEMA, COMPUTER); w.settle();
  assert.deepEqual(w.asked(), [`request ${CINEMA} ${COMPUTER}`]);
  assert(!w.writes.includes('example_tv_power=ON'));
  assert.equal(w.state(ZONE2), 'AUX2');
});

test('without a journal, what the rooms show says who is synced', () => {
  const before = both(COMPUTER);
  const states = Object.fromEntries([...before.items].map(([name, item]) => [name, item.state]));
  const w = load(TABLES.plain, { states });
  w.command(CINEMA, DISC);
  const zone = w.sent.findIndex(([name, value]) => name === ZONE2 && value === 'AUX2');
  assert(zone >= 0 && zone < w.sent.findIndex(([name]) => name === OUT1));
});

test('a follower yielded to a manual command is not moved for zone sync', () => {
  const w = both(COMPUTER);
  w.legacy({ type: 'legacy', token: ['legacy', 'zone'].join('-'), key: 'example_avr', resource: ZONE2, value: 'TV', managed: true,
    effectResources: [] });
  w.command(CINEMA, DISC);
  assert.deepEqual(w.requests(), [`request ${CINEMA} ${DISC} tx${w.tx}`]);
});

let failed = 0;
for (const [name, fn] of tests) {
  try { fn(); } catch (error) { failed += 1; process.stderr.write(`${name}\n${error.stack}\n`); }
}
process.stdout.write(JSON.stringify({ passed: tests.length - failed, failed, tests: tests.map(([name]) => name) }) + '\n');
if (failed) process.exitCode = 1;
