'use strict';
// Runs the production nightly reboots rule against stub openHAB, a fake
// clock and its timers. argv: reboots.js.
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const source = fs.readFileSync(process.argv[2], 'utf8');
const tests = [];
function test(name, fn) { fn(); tests.push(name); }

const RECEIVER = { id: 'rx', nightly: true, item: 'rx_reboot', answers: ['rx_main_power'],
  things: ['denonmarantz:avr:rx'], signs: [], idle: [{ item: 'rx_main_power', is: 'OFF' }, { item: 'rx_zone2_power', is: 'OFF' }] };
const SWITCH = { id: 'sw', nightly: true, item: 'sw_reboot', answers: ['sw_input'], things: [], signs: [],
  idle: [{ item: 'sw_signal', field: 'RX0', is: 'no signal', maxAge: 125, unreadIsIdle: true }] };
const OTHER = { id: 'other', nightly: false, item: 'other_reboot', answers: ['other_input'], things: [],
  signs: [], idle: [] };
const TABLE = { item: 'house_reboot', command: 'reboot', time: '03:00', cron: '0 0 3 * * ?', devices: [RECEIVER, SWITCH, OTHER] };
const START = Date.UTC(2026, 10, 14, 9, 0, 0);

function load({ kept = 'NULL', unreadIsIdle = true } = {}) {
  const clock = { now: START };
  const signal = (rx0, age = 0) => JSON.stringify({ version: 1, observedAt: clock.now - age * 1000, values: { RX0: rx0 } });
  const items = { house_reboot: kept, rx_main_power: 'OFF', rx_zone2_power: 'OFF', sw_input: '1', sw_signal: signal('no signal'),
    other_input: '2', rx_reboot: 'NULL', sw_reboot: 'NULL', other_reboot: 'NULL' };
  const things = { 'denonmarantz:avr:rx': { status: 'ONLINE' } };
  const commands = [], logs = [], rules = {};
  let timers = [];
  const openhab = {
    items: { getItem(name, nullable) {
      assert.equal(nullable, true);
      if (!(name in items)) return null;
      return { get state() { return items[name]; },
        postUpdate(value) { items[name] = String(value); },
        sendCommand(value) { commands.push([name, String(value), clock.now - START]); } };
    } },
    things: { getThing: uid => things[uid] || null },
    rules: { JSRule(rule) { rules[rule.id] = rule; } },
    triggers: { GenericCronTrigger: cron => ({ cron }), ItemCommandTrigger: item => ({ item }) },
  };
  class FakeDate extends Date {
    constructor(...args) { super(...(args.length ? args : [clock.now])); }
    static now() { return clock.now; }
  }
  const context = vm.createContext({
    require: name => name === 'openhab' ? openhab : assert.fail('unexpected module ' + name),
    console: { info: m => logs.push(`info: ${m}`), warn: m => logs.push(`warn: ${m}`) },
    setTimeout: (fn, ms) => { const timer = { at: clock.now + ms, fn }; timers.push(timer); return timer; },
    Date: FakeDate, JSON, Map, Object, String, Number, Math, Array,
  });
  const table = JSON.parse(JSON.stringify(TABLE));
  table.devices[1].idle[0].unreadIsIdle = unreadIsIdle;
  vm.runInContext(source.replace('const TABLE = null;', `const TABLE = ${JSON.stringify(table)};`), context);
  assert.deepEqual(rules.reboots_nightly.triggers, [{ cron: '0 0 3 * * ?' }]);
  assert.deepEqual(rules.reboots_command.triggers, [{ item: 'house_reboot' }]);
  return {
    items, things, commands, logs, signal,
    // Runs every timer due within `ms`, in order, after `before` (at each
    // second) has set what the devices report. The switch's web page keeps
    // its snapshot fresh.
    advance(ms, before = () => {}) {
      const end = clock.now + ms;
      while (clock.now < end) {
        clock.now = Math.min(end, clock.now + 1000);
        try { items.sw_signal = signal(JSON.parse(items.sw_signal).values.RX0); } catch (_) { /* no snapshot */ }
        before(clock.now - START);
        for (;;) {
          const due = timers.filter(timer => timer.at <= clock.now).sort((a, b) => a.at - b.at)[0];
          if (!due) break;
          timers = timers.filter(timer => timer !== due);
          due.fn();
        }
      }
    },
    nightly() { rules.reboots_nightly.execute(); },
    command(value) { rules.reboots_command.execute({ itemName: 'house_reboot', receivedCommand: value }); },
    report() { return JSON.parse(items.house_reboot).devices; },
    pending: () => timers.length,
  };
}

test('each nightly device restarts in turn, the receiver before the switch, and is back once it answers again', () => {
  const r = load();
  r.nightly();
  assert.deepEqual(r.commands, [['rx_reboot', 'reboot', 0]]);
  assert.equal(r.report().rx.outcome, 'rebooting');
  // The receiver's Thing goes offline at its next poll and is back at 40 s.
  r.advance(45_000, at => { r.things['denonmarantz:avr:rx'].status = at >= 10_000 && at < 40_000 ? 'OFFLINE' : 'ONLINE'; });
  assert.deepEqual(r.report().rx, { at: new Date(START + 40_000).toISOString(), by: 'schedule', outcome: 'back', seconds: 40 });
  assert.equal(r.commands.length, 1, 'the switch waits its gap');
  r.advance(115_000);
  assert.deepEqual(r.commands, [['rx_reboot', 'reboot', 0], ['sw_reboot', 'reboot', 160_000]]);
  // The switch's rule posts its input UNDEF as it sends set reboot.
  r.advance(30_000, at => { if (at === 161_000) r.items.sw_input = 'UNDEF'; if (at === 178_000) r.items.sw_input = '1'; });
  assert.equal(r.report().sw.outcome, 'back');
  assert.equal(r.report().sw.seconds, 18);
  assert.equal(r.report().other, undefined);
  assert.equal(r.pending(), 0);
  assert.deepEqual(r.logs, ['info: reboot rx: rebooting', 'info: reboot rx: back after 40 s',
    'info: reboot sw: rebooting', 'info: reboot sw: back after 18 s']);
});

test('a device in use, or not answering, is skipped, and the next is tried at once', () => {
  const cases = [
    [r => { r.items.rx_main_power = 'ON'; }, 'rx_main_power is ON, not OFF'],
    [r => { r.items.rx_zone2_power = 'ON'; }, 'rx_zone2_power is ON, not OFF'],
    [r => { r.things['denonmarantz:avr:rx'].status = 'OFFLINE'; }, 'it does not answer'],
    [r => { r.items.rx_main_power = 'UNDEF'; }, 'it does not answer'],
  ];
  for (const [set, why] of cases) {
    const r = load();
    set(r);
    r.nightly();
    assert.deepEqual(r.report().rx, { at: new Date(START).toISOString(), by: 'schedule', outcome: 'skipped', detail: why });
    assert.deepEqual(r.commands, [['sw_reboot', 'reboot', 0]], why);
  }
  for (const [set, why, unreadIsIdle] of [
    [r => { r.items.sw_signal = r.signal('1080p60 YCbCr'); }, 'sw_signal RX0 is 1080p60 YCbCr, not no signal', true],
    [r => { r.items.sw_signal = r.signal('no signal', 126); }, 'sw_signal is out of date', false],
    [r => { r.items.sw_signal = 'UNDEF'; }, 'no sw_signal reading', false],
  ]) {
    const r = load({ unreadIsIdle });
    set(r);
    r.items.rx_main_power = 'ON';
    r.nightly();
    assert.equal(r.report().sw.detail, why);
    assert.deepEqual(r.commands, []);
    assert.equal(r.pending(), 0);
  }
});

test('a snapshot that stops when the device hangs does not hold its restart back', () => {
  for (const [set, unread] of [
    [r => { r.items.sw_signal = 'UNDEF'; }, 'no sw_signal reading'],
    [r => { r.items.sw_signal = r.signal('no signal', 126); }, 'sw_signal is out of date'],
  ]) {
    const r = load();
    set(r);
    r.items.rx_main_power = 'ON';
    r.nightly();
    assert.deepEqual(r.commands, [['sw_reboot', 'reboot', 0]]);
    assert.deepEqual(r.logs.slice(1), [`info: reboot sw: ${unread}; restarting it anyway, as a restart may cure that`,
      'info: reboot sw: rebooting']);
  }
});

test('a device that keeps answering was not seen to restart; one that stays away is not back', () => {
  const r = load();
  r.items.rx_main_power = 'ON';
  r.nightly();
  r.advance(91_000);
  assert.deepEqual(r.report().sw, { at: new Date(START + 90_000).toISOString(), by: 'schedule', outcome: 'no restart seen',
    detail: 'it kept answering', seconds: 90 });
  const away = load();
  away.items.rx_main_power = 'ON';
  away.nightly();
  away.items.sw_input = 'UNDEF';
  away.advance(301_000);
  assert.deepEqual(away.report().sw, { at: new Date(START + 300_000).toISOString(), by: 'schedule', outcome: 'not back',
    detail: 'it does not answer', seconds: 300 });
  assert.equal(away.logs[away.logs.length - 1], 'warn: reboot sw: not back (it does not answer) after 300 s');
});

test('a command restarts one device now, nightly or not, past the same checks', () => {
  const r = load();
  r.command('other');
  assert.deepEqual(r.commands, [['other_reboot', 'reboot', 0]]);
  assert.equal(r.report().other.by, 'command');
  r.command('other');
  r.command('nowhere');
  assert.deepEqual(r.logs.slice(1), ['info: reboot other: already under way or waiting',
    'warn: reboot: nowhere is not a device thermite can restart']);
  // The nightly run waits behind it.
  r.nightly();
  assert.equal(r.commands.length, 1);
  r.advance(30_000, at => { r.items.other_input = at < 20_000 ? 'UNDEF' : '2'; });
  assert.equal(r.report().other.outcome, 'back');
  r.advance(120_000);
  assert.deepEqual(r.commands.map(([item]) => item), ['other_reboot', 'rx_reboot']);
  const busy = load();
  busy.items.rx_main_power = 'ON';
  busy.command('rx');
  assert.deepEqual([busy.commands, busy.report().rx.outcome], [[], 'skipped']);
});

test('a reload keeps the report, and marks the reboot it interrupted', () => {
  const kept = { version: 1, devices: {
    rx: { at: '2026-11-14T09:00:00.000Z', by: 'schedule', outcome: 'rebooting' },
    sw: { at: '2026-11-13T09:02:00.000Z', by: 'schedule', outcome: 'back', seconds: 18 },
    gone: { at: '2026-11-13T09:02:00.000Z', by: 'schedule', outcome: 'back', seconds: 9 } } };
  const r = load({ kept: JSON.stringify(kept) });
  assert.deepEqual(r.report(), { rx: Object.assign({}, kept.devices.rx, { outcome: 'interrupted' }), sw: kept.devices.sw });
  const quiet = load({ kept: JSON.stringify({ version: 1, devices: { sw: kept.devices.sw } }) });
  assert.equal(quiet.items.house_reboot, JSON.stringify({ version: 1, devices: { sw: kept.devices.sw } }));
  assert.deepEqual(load({ kept: 'UNDEF' }).items.house_reboot, 'UNDEF');
});

process.stdout.write(JSON.stringify({ passed: tests.length, tests }));
