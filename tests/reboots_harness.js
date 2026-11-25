'use strict';
// Runs the production nightly reboots rule against stub openHAB, a fake
// clock and its timers, and a fake bounded I/O for the matrix's console
// that has no write; one test embeds the real _io.js, as the renderer does,
// over a scripted socket (integration_io_harness.js). argv: reboots.js
// _io.js.
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const [source, ioSource] = process.argv.slice(2).map(path => fs.readFileSync(path, 'utf8'));
const tests = [];
function test(name, fn) { fn(); tests.push(name); }

const RECEIVER = { id: 'rx', nightly: true, item: 'rx_reboot', answers: ['rx_main_power'],
  things: ['denonmarantz:avr:rx'], signs: [], idle: [{ item: 'rx_main_power', is: 'OFF' }, { item: 'rx_zone2_power', is: 'OFF' }] };
const SWITCH = { id: 'sw', nightly: true, item: 'sw_reboot', answers: ['sw_input'], things: [], signs: [],
  idle: [{ item: 'sw_signal', field: 'RX0', is: 'no signal', maxAge: 125, unreadIsIdle: true }] };
const OTHER = { id: 'other', nightly: false, item: 'other_reboot', answers: ['other_input'], things: [],
  signs: [], idle: [] };
const MATRIX = { id: 'usb', nightly: false, item: 'usb_reboot', answers: ['usb_device1_host'], things: ['http:url:usb'],
  signs: [], idle: [{ item: 'loc_den_source', is: 'off' }],
  console: { host: '192.0.2.21', port: 8000, ready: 'initialization finished!', polled: 12 } };
// A switch that restarts when its web API hangs while telnet answers.
const HUNG = { id: 'hv', nightly: false, item: 'hv_reboot', answers: ['hv_input'], things: [], signs: [],
  idle: [{ item: 'hv_signal', field: 'RX0', is: 'no signal', maxAge: 125, unreadIsIdle: true }],
  hung: { things: ['http:url:hv'], seconds: 300, idle: [{ item: 'loc_study_source', is: 'off' }] } };
const TABLE = { item: 'house_reboot', command: 'reboot', time: '03:00', cron: '0 0 3 * * ?',
  devices: [RECEIVER, SWITCH, OTHER, MATRIX, HUNG] };
// What the matrix's controller prints as it starts.
const BOOT = ['4x4 USB3.2 Gen1 Matrix Switcher', 'system initializing...', 'initialization finished!',
  'mcu fw version : v1.00.05'];
const START = Date.UTC(2026, 10, 14, 9, 0, 0);

// With `chunks`, the console is a socket that reads them in turn.
function load({ kept = 'NULL', unreadIsIdle = true, refused = false, send = true, chunks = null, hung = true } = {}) {
  const clock = { now: START };
  const signal = (rx0, age = 0) => JSON.stringify({ version: 1, observedAt: clock.now - age * 1000, values: { RX0: rx0 } });
  const items = { house_reboot: kept, rx_main_power: 'OFF', rx_zone2_power: 'OFF', sw_input: '1', sw_signal: signal('no signal'),
    other_input: '2', rx_reboot: 'NULL', sw_reboot: 'NULL', other_reboot: 'NULL', usb_device1_host: '4',
    loc_den_source: 'off', usb_reboot: 'NULL', hv_input: '1', hv_signal: 'UNDEF', hv_reboot: 'NULL',
    loc_study_source: 'off' };
  const things = { 'denonmarantz:avr:rx': { status: 'ONLINE' }, 'http:url:usb': { status: 'ONLINE' },
    'http:url:hv': { status: 'ONLINE' } };
  // events: each console opened and each command, in order.
  const commands = [], logs = [], rules = {}, consoles = [], disposers = [], events = [];
  let timers = [];
  const openhab = {
    items: { getItem(name, nullable) {
      assert.equal(nullable, true);
      if (!(name in items)) return null;
      return { get state() { return items[name]; },
        postUpdate(value) { items[name] = String(value); },
        sendCommand(value) {
          events.push(`command ${name}`);
          commands.push([name, String(value), clock.now - START]);
        } };
    } },
    things: { getThing: uid => things[uid] || null },
    rules: { JSRule(rule) { rules[rule.id] = rule; } },
    triggers: { GenericCronTrigger: cron => ({ cron }), ItemCommandTrigger: item => ({ item }) },
  };
  class FakeDate extends Date {
    constructor(...args) { super(...(args.length ? args : [clock.now])); }
    static now() { return clock.now; }
  }
  // Each console holds the lines it has sent and not been read; a read
  // that finds none runs out its short wait.
  const thermiteIO = {
    deadline: ms => clock.now + ms,
    open(host, port) {
      if (refused) throw new Error('Connection refused');
      const link = { host, port, opened: clock.now - START, lines: [], closed: false };
      consoles.push(link);
      events.push(`open ${host}:${port}`);
      return link;
    },
    line(link, timeout, keep) {
      assert.equal(keep, true);
      if (link.closed) return null;
      if (link.lines.length) return link.lines.shift();
      throw new Error('device operation deadline exceeded');
    },
    close(link) { link.closed = true; },
  };
  let runtime = { lifecycleTracker: { addDisposeHook: fn => disposers.push(fn) } }, Java;
  if (chunks) {
    const classes = {};
    ({ runtime } = require('./integration_io_harness.js').install(classes, {}, { Socket: class {} }));
    classes['java.net.InetSocketAddress'] = class { constructor(address, port) { Object.assign(this, { address, port }); } };
    classes['java.nio.channels.SocketChannel'] = class {
      static open() { const channel = new this(); consoles.push(channel); return channel; }
      configureBlocking() {} register() {}
      connect({ address, port }) {
        Object.assign(this, { host: address, port, opened: clock.now - START });
        events.push(`open ${address}:${port}`);
        return true;
      }
      read(buffer) {
        if (!chunks.length) return 0;
        const chunk = chunks.shift();
        for (const c of chunk) buffer.bytes[buffer.pos++] = c.charCodeAt(0);
        return chunk.length;
      }
      write() { assert.fail('nothing is sent to the console'); }
      close() { this.closed = true; }
    };
    Java = { type: name => classes[name] || assert.fail('unexpected Java type ' + name), to: values => values };
  }
  const context = vm.createContext({
    require: name => ({ openhab, '@runtime': runtime })[name] || assert.fail('unexpected module ' + name),
    ...(chunks ? { Java } : { thermiteIO }),
    console: { info: m => logs.push(`info: ${m}`), warn: m => logs.push(`warn: ${m}`) },
    setTimeout: (fn, ms) => { const timer = { at: clock.now + ms, fn }; timers.push(timer); return timer; },
    clearTimeout: timer => { timers = timers.filter(other => other !== timer); },
    Date: FakeDate, JSON, Map, Object, String, Number, Math, Array,
  });
  const table = JSON.parse(JSON.stringify(TABLE));
  table.devices[1].idle[0].unreadIsIdle = unreadIsIdle;
  if (!hung) table.devices = table.devices.filter(device => !device.hung);
  const rule = source.replace('const TABLE = null;', `const TABLE = ${JSON.stringify(table)};`)
    .replace('// THERMITE_BOUNDED_IO', () => chunks ? ioSource : '');
  vm.runInContext(send ? rule : rule.replace('const SEND = true;', 'const SEND = false;'), context);
  assert.deepEqual(rules.reboots_nightly.triggers, [{ cron: '0 0 3 * * ?' }]);
  assert.deepEqual(rules.reboots_command.triggers, [{ item: 'house_reboot' }]);
  if (hung) assert.deepEqual(rules.reboots_hung.triggers, [{ cron: '0 * * * * ?' }]);
  else assert.equal(rules.reboots_hung, undefined);
  return {
    items, things, commands, logs, signal, consoles, events, openhab,
    dispose() { for (const fn of disposers) fn(); },
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
    // `minutes` of the every-minute look for hangs, each at its minute's end.
    hung(minutes, before = () => {}) {
      for (let minute = 0; minute < minutes; minute++) {
        this.advance(60_000, before);
        rules.reboots_hung.execute();
      }
    },
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

test('a device with a console is back once it says it started and answers a poll later; nothing is sent there', () => {
  const r = load();
  r.command('usb');
  assert.deepEqual(r.events, ['open 192.0.2.21:8000', 'command usb_reboot']);
  // Its greeting to each connection is not a start.
  r.consoles[0].lines.push('****welcome**** fw version :v1.00.05');
  // It restarts in half a second; its Items never show it.
  r.advance(13_000, at => { if (at === 1000) r.consoles[0].lines.push(...BOOT); });
  assert.equal(r.report().usb.outcome, 'rebooting');
  r.advance(2000);
  assert.deepEqual(r.report().usb, { at: new Date(START + 14_000).toISOString(), by: 'command', outcome: 'back', seconds: 14 });
  assert.equal(r.consoles[0].closed, true);
  assert.equal(r.pending(), 0);
  // One whose ports stop reading as it starts is back once they read again.
  const away = load();
  away.command('usb');
  away.advance(20_000, at => {
    if (at === 1000) away.consoles[0].lines.push(...BOOT);
    away.items.usb_device1_host = at < 11_000 ? 'UNDEF' : '4';
  });
  assert.deepEqual([away.report().usb.outcome, away.report().usb.seconds], ['back', 12]);
});

test('a console that does not say it started, or does not open, or is not to be opened', () => {
  const quiet = load();
  quiet.command('usb');
  quiet.consoles[0].lines.push('****welcome**** fw version :v1.00.05');
  quiet.advance(31_000);
  assert.deepEqual(quiet.report().usb, { at: new Date(START + 30_000).toISOString(), by: 'command',
    outcome: 'no restart seen', detail: 'its console never said it started', seconds: 30 });
  assert.equal(quiet.consoles[0].closed, true);
  // One that says it started but stops answering is not back.
  const gone = load();
  gone.command('usb');
  gone.advance(301_000, at => {
    if (at === 1000) gone.consoles[0].lines.push(...BOOT);
    if (at === 10_000) gone.items.usb_device1_host = 'UNDEF';
  });
  assert.deepEqual([gone.report().usb.outcome, gone.report().usb.seconds], ['not back', 300]);
  assert.equal(gone.consoles[0].closed, true);
  const closed = load();
  closed.command('usb');
  closed.consoles[0].closed = true;
  closed.advance(31_000);
  assert.equal(closed.report().usb.outcome, 'no restart seen');
  const refused = load({ refused: true });
  refused.command('usb');
  assert.deepEqual([refused.commands, refused.report().usb.outcome, refused.report().usb.detail],
    [[], 'skipped', 'its console does not open: Error: Connection refused']);
  // The read-only copy opens no console, and watches its Items as any other.
  const silent = load({ send: false });
  silent.command('usb');
  assert.deepEqual([silent.commands.length, silent.consoles], [1, []]);
  silent.advance(91_000);
  assert.deepEqual([silent.report().usb.outcome, silent.report().usb.detail], ['no restart seen', 'it kept answering']);
});

test('the real bounded I/O keeps a line the console has not finished, and the console, for the next read', () => {
  const chunks = [];
  const r = load({ chunks });
  r.command('usb');
  assert.deepEqual(r.events, ['open 192.0.2.21:8000', 'command usb_reboot']);
  chunks.push('****welcome**** fw version :v1.00.05\r\n');
  r.advance(2000, at => {
    if (at === 1000) chunks.push('\r\n4x4 USB3.2 Gen1 Matrix Switcher\r\nsystem initializing...\r\ninitialization fin');
  });
  assert.equal(r.report().usb.outcome, 'rebooting');
  assert(!r.consoles[0].closed);
  r.advance(14_000, at => { if (at === 3000) chunks.push('ished!\r\nmcu fw version : v1.00.05\r\n'); });
  assert.deepEqual([r.report().usb.outcome, r.report().usb.seconds], ['back', 16]);
  assert(r.consoles[0].closed);
});

test('a reload stops the reboot under way and the wait before the next, and closes the console', () => {
  const r = load();
  r.command('usb');
  r.dispose();
  assert.equal(r.consoles[0].closed, true);
  assert.equal(r.pending(), 0);
  const waiting = load();
  waiting.command('usb');
  waiting.command('other');
  waiting.advance(31_000);
  assert.equal(waiting.report().usb.outcome, 'no restart seen');
  assert.equal(waiting.pending(), 1, 'the next waits its gap');
  waiting.dispose();
  assert.equal(waiting.pending(), 0);
  load().dispose();
});

test('a command that cannot be sent closes the console', () => {
  const r = load();
  const getItem = r.openhab.items.getItem;
  r.openhab.items.getItem = (name, nullable) => Object.assign(getItem(name, nullable) || {},
    name === 'usb_reboot' ? { sendCommand() { throw new Error('no such Item'); } } : {});
  r.command('usb');
  assert.deepEqual([r.report().usb.outcome, r.report().usb.detail], ['not sent', 'Error: no such Item']);
  assert.equal(r.consoles[0].closed, true);
  assert.equal(r.pending(), 0);
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

test('a switch whose web API stays hung while telnet answers restarts once its room is off, at most hourly', () => {
  const r = load();
  r.things['http:url:hv'].status = 'OFFLINE';
  // First seen at the end of minute 1; five minutes later it has stayed hung.
  r.hung(5);
  assert.deepEqual(r.commands, []);
  r.hung(1);
  assert.deepEqual(r.commands, [['hv_reboot', 'reboot', 360_000]]);
  r.advance(60_000, at => { if (at === 361_000) r.items.hv_input = 'UNDEF'; if (at === 380_000) r.items.hv_input = '1'; });
  assert.deepEqual(r.report().hv, { at: new Date(START + 380_000).toISOString(), by: 'hung', outcome: 'back', seconds: 20 });
  assert.deepEqual(r.logs, ['info: reboot hv: no hv_signal reading; restarting it anyway, as a restart may cure that',
    'info: reboot hv: rebooting', 'info: reboot hv: back after 20 s']);
  // A restart that did not cure it: none again within the hour, then one.
  r.hung(58);
  assert.equal(r.commands.length, 1);
  r.hung(1);
  assert.deepEqual(r.commands.map(([item, , at]) => [item, at]), [['hv_reboot', 360_000], ['hv_reboot', 3_960_000]]);
  assert.equal(r.report().hv.by, 'hung');
});

test('a short hang, a switch telnet does not reach, or one shown in a room in use is not restarted', () => {
  const short = load();
  short.hung(10, at => { short.things['http:url:hv'].status = at <= 240_000 ? 'OFFLINE' : 'ONLINE'; });
  // Gone again: a new hang counts its five minutes afresh.
  short.hung(5, () => { short.things['http:url:hv'].status = 'OFFLINE'; });
  const away = load();
  away.things['http:url:hv'].status = 'OFFLINE';
  away.items.hv_input = 'UNDEF';
  away.hung(10);
  // Not OFFLINE but unknown, as a Thing being set up is: not a hang.
  const unknown = load();
  unknown.things['http:url:hv'].status = 'UNKNOWN';
  unknown.hung(10);
  for (const r of [short, away, unknown]) {
    assert.deepEqual([r.commands, r.items.house_reboot, r.logs], [[], 'NULL', []]);
  }
  const used = load();
  used.things['http:url:hv'].status = 'OFFLINE';
  used.items.loc_study_source = 'apple_tv';
  used.hung(30);
  assert.deepEqual(used.commands, []);
  assert.deepEqual(used.logs, ['info: reboot hv: hung for 5 min; waiting, as loc_study_source is apple_tv, not off']);
  used.items.loc_study_source = 'off';
  used.hung(1);
  assert.deepEqual(used.commands, [['hv_reboot', 'reboot', 31 * 60_000]]);
  assert.equal(used.report().hv.by, 'hung');
});

test('a restart within the hour, for any reason and across a reload, holds a hang back', () => {
  const kept = JSON.stringify({ version: 1, devices: {
    hv: { at: new Date(START - 30 * 60_000).toISOString(), by: 'command', outcome: 'back', seconds: 18 } } });
  const r = load({ kept });
  r.things['http:url:hv'].status = 'OFFLINE';
  r.hung(29);
  assert.deepEqual(r.commands, []);
  r.hung(1);
  assert.deepEqual(r.commands, [['hv_reboot', 'reboot', 30 * 60_000]]);
  // A skip sent nothing, so it holds nothing back.
  const skipped = load({ kept: JSON.stringify({ version: 1, devices: {
    hv: { at: new Date(START - 60_000).toISOString(), by: 'command', outcome: 'skipped', detail: 'it does not answer' } } }) });
  skipped.things['http:url:hv'].status = 'OFFLINE';
  skipped.hung(6);
  assert.deepEqual(skipped.commands, [['hv_reboot', 'reboot', 360_000]]);
  // A switch commanded now is not restarted for a hang within the hour.
  const commanded = load();
  commanded.command('hv');
  commanded.things['http:url:hv'].status = 'OFFLINE';
  commanded.hung(30);
  assert.deepEqual(commanded.commands, [['hv_reboot', 'reboot', 0]]);
  // A house with nothing to restart when hung looks for no hangs.
  load({ hung: false });
});

test('unreadIsIdle enforces unreadIdle rooms on schedule and command, not only hung', () => {
  for (const trigger of ['nightly', 'command']) {
    const r = load();
    // Attach unreadIdle room check to sw (and test hv which already carries hung.idle).
    r.items.hv_signal = 'UNDEF';
    r.items.loc_study_source = 'apple_tv';
    if (trigger === 'command') {
      r.command('hv');
      assert.deepEqual(r.commands, []);
      assert.equal(r.report().hv.outcome, 'skipped');
      assert.equal(r.report().hv.detail, 'loc_study_source is apple_tv, not off');
      r.items.loc_study_source = 'off';
      r.command('hv');
      assert.deepEqual(r.commands, [['hv_reboot', 'reboot', 0]]);
    } else {
      // Schedule on a nightly switch with unreadIdle.
      const table = JSON.parse(JSON.stringify(TABLE));
      table.devices[1].unreadIdle = [{ item: 'loc_study_source', is: 'off' }];
      const items = { house_reboot: 'NULL', rx_main_power: 'ON', rx_zone2_power: 'OFF', sw_input: '1', sw_signal: 'UNDEF',
        sw_reboot: 'NULL', loc_study_source: 'apple_tv' };
      const commands = [], rules = {};
      const openhab = {
        items: { getItem: name => (name in items ? { get state() { return items[name]; },
          postUpdate: v => { items[name] = String(v); }, sendCommand: v => commands.push([name, String(v)]) } : null) },
        things: { getThing: () => ({ status: 'ONLINE' }) },
        rules: { JSRule(rule) { rules[rule.id] = rule; } },
        triggers: { GenericCronTrigger: c => ({ c }), ItemCommandTrigger: i => ({ i }) },
      };
      const ctx = vm.createContext({
        require: n => ({ openhab, '@runtime': { lifecycleTracker: { addDisposeHook() {} } } })[n],
        thermiteIO: { deadline: ms => START + ms }, console: { info() {}, warn() {} },
        setTimeout: () => 1, clearTimeout() {}, Date, JSON, Map, Object, String, Number, Math, Array,
      });
      vm.runInContext(source.replace('const TABLE = null;', `const TABLE = ${JSON.stringify(table)};`).replace('// THERMITE_BOUNDED_IO', ''), ctx);
      rules.reboots_nightly.execute();
      assert.deepEqual(commands, []);
      assert.equal(JSON.parse(items.house_reboot).devices.sw.detail, 'loc_study_source is apple_tv, not off');
      items.loc_study_source = 'off';
      rules.reboots_nightly.execute();
      assert.deepEqual(commands, [['sw_reboot', 'reboot']]);
    }
  }
});

test('said() bounds console reading instead of looping forever on chatty output, and dispose stops future work', () => {
  const r = load();
  r.command('usb');
  assert.equal(r.consoles.length, 1);
  for (let i = 0; i < 600; i += 1) r.consoles[0].lines.push(`noise ${i}`);
  r.advance(2000);
  // Bounded at 256 lines per poll; still leaves remaining chatter unread and does not hang.
  assert.equal(r.consoles[0].lines.length, 600 - 256);
  r.dispose();
  assert(r.consoles[0].closed);
  assert.equal(r.pending(), 0);
  r.command('other');
  assert.deepEqual(r.commands, [['usb_reboot', 'reboot', 0]]);
});

process.stdout.write(JSON.stringify({ passed: tests.length, tests }));
