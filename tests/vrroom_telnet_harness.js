'use strict';
// Runs the production VRROOM telnet rule against a stub openHAB and a
// scripted switch. argv: vrroom_telnet.js _device.js _io.js. No device or
// network access: the rule's thermiteIO is a fixture, and what the scripts
// share (_device.js) is embedded as the renderer embeds it (_io.py). The
// last test runs the real _io.js and _device.js on a scripted socket
// (integration_io_harness.js).
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const [ruleSource, deviceSource, ioSource] = process.argv.slice(2).map(path => fs.readFileSync(path, 'utf8'));
const tests = [];
function test(name, fn) { fn(); tests.push(name); }

// A VRROOM on telnet. It answers get insel with `insel <tx0> <tx1>`, get
// and set ipinterrupt with `ipinterrupt on|off`, set insel with the new
// insel line (`decline`: with the old one; `err`: refused, as the firmware
// says it, `insel err`), and says nothing to anything else; `echo` repeats
// each request before answering it, and `answers` maps a request to the
// line that answers it instead. With ipinterrupt on, `int` follows a
// change of input. `refuse`
// makes it accept a connection and close it at once, `mute` close one at
// its next request, unanswered, and `hangup` close one at that request,
// unanswered. `silent` takes set insel without answering it, and a request
// in `unanswered` gets no answer at all. close()
// closes every connection while idle; notice() says `int` on each.
// `format` shapes each answer.
function vrroom({ send = true, interrupts = false } = {}) {
  const clock = { now: 0 }, hooks = [];
  const vr = { refuse: false, mute: false, hangup: null, silent: false, decline: false, err: false, echo: false,
    insel: [0, 4], interrupts,
    opened: [], requests: [], links: [], unanswered: new Set(), answers: {}, format: line => line,
    close() { for (const link of vr.links) link.peerClosed = true; },
    notice(line = 'int') { for (const link of vr.links) if (!link.closed && !link.peerClosed) link.lines.push(line); } };
  const thermiteIO = {
    deadline: ms => clock.now + ms,
    remaining: end => { if (end <= clock.now) throw Error('deadline'); return end - clock.now; },
    open() {
      vr.opened.push(clock.now / 1000);
      const link = { closed: false, peerClosed: vr.refuse, lines: [] };
      vr.links.push(link);
      return link;
    },
    renew(link, end) { link.deadline = end; },
    line(link) {
      if (link.lines.length) return link.lines.shift();
      if (link.peerClosed) { link.closed = true; return null; }
      throw Error('quiet');
    },
    quietTimeout: () => true, waitedOut: () => true,
    write(link, text) {
      if (link.closed || link.peerClosed) throw Error('closed');
      assert(text.endsWith('\n') && !text.endsWith('\r\n'), JSON.stringify(text));
      const request = text.slice(0, -1);
      vr.requests.push(request);
      if (vr.mute || request === vr.hangup) { link.peerClosed = true; return; }
      if (vr.echo) link.lines.push(request);
      if (vr.unanswered.has(request)) return;
      if (request in vr.answers) { link.lines.push(vr.answers[request]); return; }
      const set = /^set insel (\d) (\d)$/.exec(request);
      const insel = () => vr.format(`insel ${vr.insel[0]} ${vr.insel[1]}`);
      if (request === 'get insel') link.lines.push(insel());
      else if (request === 'get ipinterrupt') link.lines.push(`ipinterrupt ${vr.interrupts ? 'on' : 'off'}`);
      else if (request === 'set ipinterrupt on') { vr.interrupts = true; link.lines.push('ipinterrupt on'); }
      else if (set && vr.err) link.lines.push(vr.format('insel err'));
      else if (set && vr.decline) link.lines.push(insel());
      else if (set) {
        const changed = Number(set[1]) !== vr.insel[0];
        vr.insel = [Number(set[1]), Number(set[2])];
        if (!vr.silent) link.lines.push(insel());
        if (changed && vr.interrupts) link.lines.push('int');
      }
    },
    close(link) { link.closed = true; },
  };
  const rules = {}, logs = [], posted = [];
  const items = { switch_input: 'NULL', switch_reboot: 'NULL' };
  const openhab = {
    items: { getItem: (name, nullable) => {
      if (!(name in items)) { if (nullable) return null; throw Error(`Item ${name} does not exist`); }
      return { get state() { return items[name]; }, postUpdate(value) { posted.push([name, String(value)]); items[name] = String(value); } };
    } },
    rules: { JSRule(rule) { rules[rule.id] = rule; } },
    triggers: { ItemCommandTrigger: name => ({ name }), GenericCronTrigger: cron => ({ cron }) },
  };
  const context = vm.createContext({
    require: name => {
      if (name === '@runtime') return { lifecycleTracker: { addDisposeHook: hook => hooks.push(hook) } };
      assert.equal(name, 'openhab'); return openhab;
    },
    console: { info: m => logs.push(['info', m]), warn: m => logs.push(['warn', m]), error: m => logs.push(['error', m]) },
    Date: { now: () => clock.now }, JSON, Map, Set, Object, String, Number, Boolean, Array, Error, Math, RegExp, thermiteIO,
  });
  let text = send ? ruleSource : ruleSource.replace('const SEND = true;', 'const SEND = false;');
  text = text.replace('// THERMITE_BOUNDED_IO', '').replace('// THERMITE_DEVICE', () => deviceSource)
    .replace('const DEVICES = null;', `const DEVICES = ${JSON.stringify([{ id: 'switch', host: '192.0.2.40', port: 2222, inputs: 4 }])};`);
  vm.runInContext(text, context);
  const warnings = () => logs.filter(([level]) => level === 'warn').map(([, message]) => message);
  const posts = () => posted.filter(([item]) => item === 'switch_input').map(([, value]) => value);
  return { rules, logs, posted, posts, context, clock, vr, items, warnings, unload: () => hooks.forEach(hook => hook()),
    poll: () => { rules.vrroom_telnet_switch_poll.execute(); clock.now += 1000; },
    listen: () => { rules.vrroom_telnet_switch_listen.execute(); clock.now += 1000; },
    // Ten seconds of the rule's life: the poll, then a listen each second.
    tick: () => { rules.vrroom_telnet_switch_poll.execute(); for (let s = 0; s < 10; s++) { rules.vrroom_telnet_switch_listen.execute(); clock.now += 1000; } },
    command: (item, value, eventSource) => rules.vrroom_telnet_switch_command.execute({
      itemName: `switch_${item}`, receivedCommand: String(value), eventSource }) };
}

test('a VRROOM is read every 10 s on one kept connection, which turns its change notices on, closed when the script unloads', () => {
  const r = vrroom();
  r.tick(); r.tick(); r.tick();
  assert.deepEqual(r.vr.opened, [0]);
  assert.deepEqual(r.vr.requests, ['get ipinterrupt', 'set ipinterrupt on', 'get insel', 'get insel', 'get insel']);
  assert.deepEqual(r.posts(), ['0', '0', '0']);
  assert(r.logs.some(([level, message]) => level === 'info' && message === 'switch: sent set ipinterrupt on, so that it says when a stream changes'));
  // The setting is the switch's own: a new connection that finds it on leaves it.
  r.vr.close(); r.tick();
  assert.deepEqual(r.vr.opened, [0, 30]); assert.deepEqual(r.vr.requests.slice(5), ['get ipinterrupt', 'get insel']);
  assert.deepEqual(r.warnings(), []);
  r.unload();
  assert(r.vr.links.every(link => link.closed));
});

test('a change notice between polls is read within a second; an unasked line of any wording counts, silence costs nothing', () => {
  const r = vrroom({ interrupts: true });
  r.tick();
  const asked = r.vr.requests.length;
  r.listen(); r.listen();
  assert.equal(r.vr.requests.length, asked);
  r.vr.insel = [2, 4]; r.vr.notice();
  r.listen();
  assert.deepEqual(r.vr.requests.slice(asked), ['get insel']); assert.equal(r.items.switch_input, '2');
  r.vr.insel = [3, 4]; r.vr.notice('interrupt'); r.listen();
  assert.equal(r.items.switch_input, '3');
  // A notice that comes with an answer is not read twice.
  r.vr.links[0].lines.push('int'); r.poll(); const after = r.vr.requests.length; r.listen();
  assert.equal(r.vr.requests.length, after);
  assert.deepEqual(r.vr.opened, [0]); assert.deepEqual(r.warnings(), []);
});

test('set insel is acknowledged, then read back on the same connection; a dropped first letter is still an answer', () => {
  const r = vrroom({ interrupts: true });
  r.tick();
  r.command('input', 2);
  assert.deepEqual(r.vr.requests.slice(-2), ['set insel 2 4', 'get insel']);
  assert.equal(r.items.switch_input, '2'); assert.deepEqual(r.vr.opened, [0]);
  // The int that follows the switch was seen while waiting: nothing reads it again.
  const asked = r.vr.requests.length; r.listen(); assert.equal(r.vr.requests.length, asked);
  r.vr.format = line => line.slice(1);
  r.command('input', '1.0');
  assert.equal(r.items.switch_input, '1');
  for (const bad of ['4', '-1', 'x', '1.5']) r.command('input', bad);
  assert.deepEqual(r.vr.requests.filter(line => line.startsWith('set insel')), ['set insel 2 4', 'set insel 1 4']);
  assert.deepEqual(r.warnings(), ["switch_input: switch has no input '4', dropped", "switch_input: switch has no input '-1', dropped",
    "switch_input: switch has no input 'x', dropped", "switch_input: switch has no input '1.5', dropped"]);
});

test('a VRROOM that stops answering is UNDEF and tried after 10, 20, 40 s, then every minute; a command connects at once', () => {
  const r = vrroom();
  r.tick(); r.vr.mute = true; r.tick();
  assert.equal(r.items.switch_input, 'UNDEF'); assert(r.vr.links[0].closed);
  r.vr.mute = false; r.vr.refuse = true;
  for (let poll = 2; poll < 22; poll++) r.tick();
  assert.deepEqual(r.vr.opened, [0, 20, 40, 80, 140, 200]);
  assert.deepEqual(r.warnings(), ['switch: closed the connection; its Items are UNDEF until it answers']);
  assert.deepEqual(r.posts().slice(1), Array(6).fill('UNDEF'));
  r.vr.refuse = false; r.command('input', 3);
  assert.equal(r.items.switch_input, '3');
  assert(r.logs.some(([level, message]) => level === 'info' && message === 'switch: answering over telnet again'));
  r.tick(); assert.equal(r.vr.opened.length, 7);
});

test('the VRROOM routing provider is unsent before set insel, refused when another input is named, unknown unanswered, applied once answered', () => {
  const r = vrroom({ interrupts: true }), provider = r.context.routingProvider();
  r.tick();
  assert.deepEqual(provider.execute('switch_input', '2'),
    { outcome: 'applied', sample: { value: '2', fresh: true, basis: 'owned_native_read', observedAt: 10000 } });
  r.vr.decline = true;
  assert.deepEqual(provider.execute('switch_input', '3'), { outcome: 'refused' });
  assert.equal(r.items.switch_input, '2');
  r.vr.decline = false; r.vr.silent = true;
  assert.deepEqual(provider.execute('switch_input', '3'), { outcome: 'unknown' });
  r.vr.silent = false; r.vr.hangup = 'set insel 1 4';
  assert.deepEqual(provider.execute('switch_input', '1'), { outcome: 'unknown' });
  // An acknowledged switch whose read then fails is applied; the executor re-reads.
  r.vr.hangup = 'get insel';
  assert.deepEqual(provider.execute('switch_input', '0'), { outcome: 'applied', sample: null });
  r.vr.hangup = null; r.vr.refuse = true; r.vr.close();
  assert.deepEqual(provider.execute('switch_input', '2'), { outcome: 'unsent' });
  r.vr.refuse = false;
  assert.deepEqual(provider.probe('switch_input'), { value: '0', fresh: true, basis: 'owned_native_read', observedAt: 10000 });
  assert(provider.has('switch_input', '3') && provider.has('switch_input', '0.0'));
  assert(!provider.has('switch_input', '4') && !provider.has('switch_reboot', '1') && !provider.has('other_input', '1'));
  assert(provider.oneShot('switch_reboot') && !provider.oneShot('switch_input'));
});

test('reboot is sent only when asked for, logged with who asked, and leaves the input UNDEF until the switch answers', () => {
  const r = vrroom({ interrupts: true }), provider = r.context.routingProvider();
  r.tick();
  r.command('reboot', 'REBOOT'); r.command('reboot', 'ON');
  assert(!r.vr.requests.includes('set reboot'));
  r.command('reboot', 'reboot', 'org.openhab.ui');
  assert.equal(r.vr.requests[r.vr.requests.length - 1], 'set reboot');
  assert(r.vr.links[0].closed); assert.equal(r.items.switch_input, 'UNDEF');
  assert.deepEqual(r.warnings(), ["switch_reboot: 'REBOOT' is not reboot, dropped", "switch_reboot: 'ON' is not reboot, dropped",
    'switch: sent set reboot (command from org.openhab.ui); it restarts and does not answer for about 15 s']);
  r.tick(); assert.equal(r.items.switch_input, '0');
  assert(provider.has('switch_reboot', 'reboot'));
  assert.deepEqual(provider.execute('switch_reboot', 'reboot'), { outcome: 'applied' });
  r.vr.refuse = true; r.tick();
  assert.deepEqual(provider.execute('switch_reboot', 'reboot'), { outcome: 'unsent' });
  assert.equal(r.vr.requests.filter(line => line === 'set reboot').length, 2);
});

test('a read-only copy connects to no VRROOM and only logs what it would send', () => {
  const r = vrroom({ send: false });
  r.tick(); r.tick(); r.command('input', 2); r.command('reboot', 'reboot');
  assert.deepEqual(r.vr.opened, []); assert.deepEqual(r.posted, []);
  assert.deepEqual(r.logs, [['info', 'switch: SEND is false, not sending set insel 2 4'],
    ['info', 'switch: SEND is false, not sending set reboot']]);
});

test('the 1 s rule reads only on a live connection that gave notice, and never connects; reconnects keep the poll backoff', () => {
  // A notice, then the switch closes and refuses (a restart): the listen
  // ticks connect to nothing; the poll's backoff alone reconnects.
  let r = vrroom({ interrupts: true });
  r.tick();
  r.vr.notice(); r.vr.close(); r.vr.refuse = true;
  for (let s = 0; s < 30; s++) r.listen();
  assert.deepEqual(r.vr.opened, [0]);
  r = vrroom({ interrupts: true });
  r.tick();
  r.vr.notice(); r.vr.close(); r.vr.refuse = true;
  for (let i = 0; i < 6; i++) r.tick();
  assert.deepEqual(r.vr.opened, [0, 10, 20, 40]);
  assert.deepEqual(r.posts().filter(v => v === 'UNDEF').length, 3);
  // A notice whose read fails: one UNDEF, no reconnect until the poll.
  r = vrroom({ interrupts: true });
  r.tick();
  r.vr.notice(); r.vr.hangup = 'get insel';
  r.listen(); r.vr.hangup = null;
  for (let s = 0; s < 8; s++) r.listen();
  assert.deepEqual(r.posts(), ['0', 'UNDEF']); assert.deepEqual(r.vr.opened, [0]);
  r.poll(); assert.deepEqual(r.posts(), ['0', 'UNDEF', '0']); assert.deepEqual(r.vr.opened, [0, 19]);
  // A switch that takes connections and answers nothing: one connection per poll at most.
  r = vrroom({ interrupts: true });
  r.tick();
  r.vr.notice(); r.vr.close(); r.vr.unanswered = new Set(['get ipinterrupt', 'set ipinterrupt on', 'get insel']);
  for (let i = 0; i < 3; i++) r.tick();
  assert.deepEqual(r.vr.opened, [0, 10, 20]);
});

test('a connection turns ipinterrupt on only when the switch says it is off, never when it does not answer', () => {
  const r = vrroom();
  r.vr.unanswered.add('get ipinterrupt');
  r.tick();
  assert.deepEqual(r.vr.requests, ['get ipinterrupt', 'get insel']);
  assert.equal(r.vr.interrupts, false);
  r.vr.unanswered.clear(); r.vr.close(); r.tick();
  assert.deepEqual(r.vr.requests.slice(2), ['get ipinterrupt', 'set ipinterrupt on', 'get insel']);
});

test('set insel is applied only on an insel answer naming it: insel err or another input is refused, any other line skipped, none unknown', () => {
  const r = vrroom({ interrupts: true }), provider = r.context.routingProvider(), link = () => r.vr.links[0];
  r.tick();
  r.vr.err = true;
  assert.deepEqual(provider.execute('switch_input', '2'), { outcome: 'refused' });
  assert.deepEqual(r.warnings(), ['switch: answered set insel 2 4 with insel err']);
  r.vr.format = line => line.slice(1);
  assert.deepEqual(provider.execute('switch_input', '2'), { outcome: 'refused' });
  r.vr.format = line => line; r.vr.err = false;
  // Stale or unrelated lines before a silent switch are no acknowledgement.
  r.vr.silent = true;
  for (const stale of ['ipinterrupt on', 'nt', 'interrupt', 'int', 'reboot']) {
    link().lines.push(stale);
    assert.deepEqual(provider.execute('switch_input', '3'), { outcome: 'unknown' }, stale);
  }
  r.vr.silent = false; r.vr.insel = [0, 4];
  // An echo of the command is not its answer; the answer that follows is.
  r.vr.echo = true; r.vr.decline = true;
  assert.deepEqual(provider.execute('switch_input', '1'), { outcome: 'refused' });
  r.vr.decline = false;
  assert.deepEqual(provider.execute('switch_input', '1'),
    { outcome: 'applied', sample: { value: '1', fresh: true, basis: 'owned_native_read', observedAt: 10000 } });
  // A get ipinterrupt answered with an error turns nothing on.
  r.vr.echo = false; r.vr.interrupts = false; r.vr.answers['get ipinterrupt'] = 'ipinterrupt err'; r.vr.close();
  const asked = r.vr.requests.length;
  r.tick();
  assert.deepEqual(r.vr.requests.slice(asked), ['get ipinterrupt', 'get insel']);
});

test('a reboot ordered by the routing actor is logged with who asked for it', () => {
  const r = vrroom({ interrupts: true });
  r.tick();
  // An actor that queues each command, as lifecycle_io.js does, stamping it.
  const queued = [];
  vm.runInContext('routingLifecycleActor = actor', Object.assign(r.context, { actor: {
    legacy(resource, value, sensitive, source) { queued.push({ resource, value, queuedAt: r.clock.now }); return true; } } }));
  const provider = r.context.routingProvider();
  r.command('reboot', 'reboot', 'thermite.remote:c1=>org.openhab.core.io.rest$owner');
  r.clock.now += 5;
  r.command('reboot', 'reboot', 'org.openhab.ui');
  r.command('input', '2', 'org.openhab.ui');
  assert.equal(queued.length, 3); assert(!r.vr.requests.includes('set reboot'));
  for (const operation of queued.filter(op => op.resource === 'switch_reboot')) {
    assert.deepEqual(provider.execute(operation.resource, operation.value, operation), { outcome: 'applied' });
  }
  // One whose command was never queued here names nobody.
  assert.deepEqual(provider.execute('switch_reboot', 'reboot', { queuedAt: r.clock.now + 60000 }), { outcome: 'applied' });
  assert.deepEqual(r.warnings().filter(line => line.includes('set reboot')), [
    'switch: sent set reboot (command from thermite.remote:c1=>org.openhab.core.io.rest$owner); it restarts and does not answer for about 15 s',
    'switch: sent set reboot (command from org.openhab.ui); it restarts and does not answer for about 15 s',
    'switch: sent set reboot; it restarts and does not answer for about 15 s']);
});

test('listening keeps the bytes of a line not yet complete and the connection; a session still starts clean', () => {
  const classes = {}, chunks = [], opened = [];
  const { runtime, hooks } = require('./integration_io_harness.js').install(classes, {}, { Socket: class {} });
  classes['java.net.InetSocketAddress'] = class { constructor(address, port) { Object.assign(this, { address, port }); } };
  // A socket whose bytes come in the chunks given, as TCP may split them.
  classes['java.nio.channels.SocketChannel'] = class {
    static open() { const channel = new this(); opened.push(channel); return channel; }
    configureBlocking() {} connect() { return true; } register() {} close() { this.closed = true; }
    read(buffer) {
      if (!chunks.length) return 0;
      const chunk = chunks.shift();
      for (const c of chunk) buffer.bytes[buffer.pos++] = c.charCodeAt(0);
      return chunk.length;
    }
    write(buffer) { let n = 0; while (buffer.hasRemaining()) { buffer.get(); n++; } return n; }
  };
  const context = vm.createContext({
    Java: { type: name => classes[name] || assert.fail('unexpected Java type ' + name), to: values => values },
    require: name => name === '@runtime' ? runtime : { items: { getItem: () => null }, rules: { JSRule() {} }, triggers: {} },
    console: { info() {}, warn() {}, error() {} },
  });
  vm.runInContext(`${ioSource}\n${deviceSource}`, context);
  const [thermiteIO, thermiteDevice] = vm.runInContext('[thermiteIO, thermiteDevice]', context);
  const heard = [], device = { id: 'switch' };
  const links = thermiteDevice.links((_, end) => thermiteIO.open('192.0.2.40', 2222, 2000, end), (_, line) => heard.push(line));
  const first = links.connection(device, thermiteIO.deadline(12000));
  chunks.push('in');
  assert.equal(links.listen(device, thermiteIO.deadline(1000)), first);
  assert.deepEqual(heard, []); assert(!first.closed);
  chunks.push('t\r\n');
  assert.equal(links.listen(device, thermiteIO.deadline(1000)), first);
  assert.deepEqual(heard, ['int']);
  chunks.push('int\r\nin');
  assert.equal(links.listen(device, thermiteIO.deadline(1000)), first);
  chunks.push('t\r\n');
  assert.equal(links.listen(device, thermiteIO.deadline(1000)), first);
  assert.deepEqual(heard, ['int', 'int', 'int']); assert.equal(opened.length, 1);
  // Before a command, as for the AVPro and the projectors, a part line
  // still closes the connection: it could not answer what is asked next.
  chunks.push('in');
  const second = links.connection(device, thermiteIO.deadline(12000));
  assert(first.closed && !second.closed && second !== first); assert.equal(opened.length, 2);
  hooks.forEach(hook => hook());
});

process.stdout.write(JSON.stringify({ passed: tests.length, tests }));
