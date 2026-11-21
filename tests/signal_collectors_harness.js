'use strict';
// Production collectors and _device.js, with no sockets or house data.
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const fixture = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const tests = [];
function test(name, fn) { fn(); tests.push(name); }

function device(name, {send = true} = {}) {
  const clock = {now:100000}, requests = [], opened = [], posted = [], rules = {}, hooks = [];
  const state = {input:'hdmi1', power:'on', route:[1, 4], routes:[1, 1, 1, 1], mode:'0', override:null};
  const signalItem = name === 'sony_adcp' ? 'example_video_signal' : name === 'vrroom_telnet' ? 'example_telnet_signal' : 'example_signal';
  const items = Object.fromEntries(['input', 'power', 'power_state', 'picture_mode', 'key', 'reboot', 'problem',
    'output1_input', 'output2_input', 'output3_input', 'output4_input', `${name}_commands`].map(key => [`example_${key}`, 'NULL']));
  items[signalItem] = 'NULL';
  function reply(request) {
    if (state.override) { const result = state.override(request); if (result !== undefined) return result; }
    if (name === 'vrroom_telnet') {
      if (request === 'get ipinterrupt') return ['ipinterrupt on'];
      if (request === 'get insel') return [`insel ${state.route.join(' ')}`];
      if (request === 'get opmode') return [`opmode ${state.mode}`];
      if (request.startsWith('get status ')) return [`${request.slice(11).toUpperCase()}: example format`];
      const set = /^set insel (\d) (\d)$/.exec(request);
      if (set) { state.route = [Number(set[1]), Number(set[2])]; return [`insel ${state.route.join(' ')}`]; }
    } else if (name === 'sony_adcp') {
      const value = {'power_status ?':state.power, 'input ?':state.input,
        'picture_mode ?':'cinema_film1', 'signal ?':'3840x2160/60p'}[request];
      if (value !== undefined) return [JSON.stringify(value)];
      const set = /^input "(hdmi[12])"$/.exec(request);
      if (set) { state.input = set[1]; return ['ok']; }
    } else {
      if (request === 'GET OUT0 VS') return state.routes.map((input, index) => `OUT${index + 1} VS IN${input}`);
      if (/^GET (IN|OUT)\d+ /.test(request)) return [request, `${request.slice(4)} example raw reply`];
      const set = /^SET OUT(\d) VS IN(\d)$/.exec(request);
      if (set) { state.routes[Number(set[1]) - 1] = Number(set[2]); return [`OUT${set[1]} VS IN${set[2]}`]; }
    }
    return [];
  }
  // Production deadlines use System.nanoTime(), not Date.now(): use an
  // unrelated, negative nanosecond origin so mixing the clocks fails.
  const monotonic = () => -900000000000 + (clock.now - 100000) * 1000000;
  const thermiteIO = {
    deadline:ms => monotonic() + ms * 1000000,
    remaining:end => { const ms = (end - monotonic()) / 1000000; if (ms <= 0) throw Error('deadline'); return ms; },
    open() { const link = {closed:false, lines:name === 'sony_adcp' ? ['NOKEY'] : []}; opened.push(link); return link; },
    renew(link, end) { link.deadline = end; },
    line(link, ms) {
      if (link.closed) return null;
      if (link.lines.length) { clock.now += 1; return link.lines.shift(); }
      clock.now += Math.max(0, Math.min(ms, link.deadline === undefined ? ms : (link.deadline - monotonic()) / 1000000));
      throw Error('quiet');
    },
    quietTimeout:(_link, error) => error.message === 'quiet',
    waitedOut:(_link, error) => error.message === 'quiet',
    write(link, text) {
      assert(!link.closed, 'write on closed session');
      const request = text.trim(); requests.push(request); link.lines.push(...reply(request));
    },
    close(link) { if (link) link.closed = true; },
  };
  const openhab = {
    items:{getItem(name, nullable) {
      if (!(name in items)) { if (nullable) return null; throw Error(`unknown Item ${name}`); }
      return {get state() { return items[name]; }, postUpdate(value) { items[name] = String(value); posted.push([name, String(value)]); }};
    }},
    rules:{JSRule(rule) { rules[rule.id] = rule; }},
    triggers:{GenericCronTrigger:cron => ({cron}), ItemCommandTrigger:name => ({name})},
  };
  const context = vm.createContext({require:name => name === '@runtime'
    ? {lifecycleTracker:{addDisposeHook:hook => hooks.push(hook)}} : (assert.equal(name, 'openhab'), openhab),
  console:{info() {}, warn() {}, error() {}}, Date:{now:() => clock.now}, thermiteIO});
  let source = fixture.sources[name].replace('// THERMITE_DEVICE', () => fixture.shared[name])
    .replace('const DEVICES = null;', `const DEVICES = ${JSON.stringify([{id:'example', host:'192.0.2.10', port:1234, inputs:4, outputs:4, control:true}])};`)
    .replace('const INPUTS = null;', 'const INPUTS = ["hdmi1", "hdmi2"];')
    .replace('const KEYS = null;', 'const KEYS = {};');
  if (!send) source = source.replace('const SEND = true;', 'const SEND = false;');
  vm.runInContext(source, context);
  const poll = () => rules[`${name}_example_poll`].execute();
  const collect = () => {
    const start = clock.now, health = items.example_problem, tally = items[`example_${name}_commands`], count = opened.length;
    rules[`${name}_example_signal`].execute();
    // The AVPro's reads have 1 s, and its fence after a missed one 2 s more.
    const budget = name === 'avpro_telnet' ? 3000 : 1000;
    assert(clock.now - start <= budget, `optional collection exceeded its ${budget / 1000} s budget`);
    assert.equal(opened.length, count, 'optional collection opened a connection');
    assert.equal(items.example_problem, health, 'optional collection changed routing health');
    assert.equal(items[`example_${name}_commands`], tally, 'optional collection changed command counters');
    return items[signalItem] === 'NULL' ? null : JSON.parse(items[signalItem]);
  };
  return {clock, requests, opened, posted, state, items, poll, collect, context,
    listen:() => rules[`${name}_example_listen`].execute()};
}

test('collectors never connect by themselves, and SEND false issues no queries', () => {
  for (const name of Object.keys(fixture.sources)) {
    const r = device(name);
    assert.equal(r.collect().reason, 'no_response'); assert.deepEqual(r.requests, []);
    const held = device(name, {send:false}); held.poll(); assert.equal(held.collect(), null);
    assert.deepEqual(held.opened, []); assert.deepEqual(held.requests, []);
  }
});

test('VRROOM reads bounded native status on the existing session with actual stable input selectors', () => {
  const r = device('vrroom_telnet'); r.poll(); r.requests.length = 0;
  const value = r.collect();
  assert.equal(value.values.portseltx0, '1'); assert.equal(value.values.portseltx1, '1');
  assert.equal(value.values.RX0, 'example format'); assert.equal(value.values.TX1, 'example format');
  assert.equal(value.values.AUDOUT, 'example format'); assert(!('RX1' in value.values));
  assert(!r.requests.includes('get status rx1')); assert(r.requests.every(query => query.startsWith('get ')));
  assert.equal(r.opened.length, 1);
  r.state.mode = '2'; r.state.route = [2, 3];
  const matrix = r.collect(); assert.equal(matrix.values.RX1, 'example format');
  assert.equal(matrix.values.portseltx0, '2'); assert.equal(matrix.values.portseltx1, '3');
  r.state.route = [4, 4];
  const cycle = r.collect(); assert(!('portseltx0' in cycle.values)); assert(!('portseltx1' in cycle.values));
});

test('VRROOM rejects route and processing-mode transitions during signal collection', () => {
  for (const change of [r => { r.state.route = [2, 4]; }, r => { r.state.mode = '2'; }]) {
    const r = device('vrroom_telnet'); r.poll();
    r.state.override = request => { if (request === 'get status tx0') change(r); };
    const result = r.collect(); assert.equal(result.reason, 'route_changed'); assert.deepEqual(result.values, {});
  }
});

test('VRROOM rotates unsupported queries, clears stale values, and closes timed-out sessions', () => {
  const r = device('vrroom_telnet'); r.poll(); assert(r.collect().values.RX0);
  r.requests.length = 0;
  r.state.override = request => request.startsWith('get status ') ? [] : undefined;
  for (let cycle = 0; cycle < 5; cycle += 1) {
    const value = r.collect(); assert.equal(value.reason, 'no_response'); assert.deepEqual(value.values, {});
    assert(r.opened.at(-1).closed); r.poll();
  }
  for (const target of ['rx0', 'tx0', 'aud0', 'audout', 'tx1', 'aud1', 'tx0sink', 'tx1sink', 'spd0']) {
    assert(r.requests.includes(`get status ${target}`), `starved ${target}`);
  }
  r.state.override = request => request === 'get status rx0' ? ['TX0: late wrong port', 'get status rx0'] : undefined;
  const result = r.collect(); assert(!('RX0' in result.values));
  assert(r.opened.at(-1).closed);
});

test('VRROOM sanitizes status strings and fails closed for unknown opmode', () => {
  const r = device('vrroom_telnet'); r.poll();
  r.state.override = request => request === 'get status rx0' ? [`RX0: \u202e${'a'.repeat(300)}`] : undefined;
  assert.equal(r.collect().values.RX0, 'a'.repeat(192));
  r.state.mode = '9'; const result = r.collect(); assert.equal(result.reason, 'invalid_response'); assert.deepEqual(result.values, {});
});

test('VRROOM optional reads preserve pending and interleaved native change notices', () => {
  const r = device('vrroom_telnet'); r.poll(); assert.equal(r.items.example_input, '1');
  r.state.route = [2, 4]; r.opened.at(-1).lines.push('int');
  r.collect(); r.listen(); assert.equal(r.items.example_input, '2');
  r.state.override = request => {
    if (request === 'get status tx0') { r.state.route = [3, 4]; return ['int', 'TX0: 3840x2160']; }
  };
  assert.equal(r.collect().reason, 'route_changed');
  r.listen(); assert.equal(r.items.example_input, '3');
});

test('Sony timing belongs to the input read before and after the signal query', () => {
  const r = device('sony_adcp'); r.poll(); r.requests.length = 0;
  assert.deepEqual(r.collect().values, {input:'hdmi1', signal:'3840x2160/60p'});
  assert.deepEqual(r.requests, ['power_status ?', 'input ?', 'signal ?', 'input ?']);
  r.state.override = request => { if (request === 'signal ?') r.state.input = 'hdmi2'; };
  const changed = r.collect(); assert.equal(changed.reason, 'route_changed'); assert.deepEqual(changed.values, {});
});

test('Sony unsupported timing cannot leave an old answer available to a control poll', () => {
  const r = device('sony_adcp'); r.poll(); assert(r.collect().values.signal);
  r.state.override = request => request === 'signal ?' ? [] : undefined;
  const missing = r.collect(); assert.equal(missing.reason, 'no_response'); assert.deepEqual(missing.values, {});
  const old = r.opened.at(-1); assert(old.closed); old.lines.push('"old timing"');
  assert.equal(r.collect().reason, 'no_response');
  r.state.override = null; r.poll(); assert.equal(r.opened.length, 2);
  assert.equal(r.collect().values.signal, '3840x2160/60p'); assert.equal(r.items.example_power, 'ON');
});

test('Sony preserves Invalid as unknown and reports standby without asking for a signal', () => {
  const r = device('sony_adcp'); r.poll();
  r.state.override = request => request === 'signal ?' ? ['"Invalid"'] : undefined;
  assert.equal(r.collect().values.signal, 'Invalid');
  r.state.power = 'standby'; r.requests.length = 0;
  assert.equal(r.collect().reason, 'not_active'); assert.deepEqual(r.requests, ['power_status ?']);
});

test('AVPro retains only matching physical port and query text as unparsed metadata', () => {
  const r = device('avpro_telnet'); r.poll(); r.requests.length = 0;
  r.state.override = request => request === 'GET IN1 VID FMT INF'
    ? [request, 'OUT1 VS IN2', 'IN2 VID FMT INF wrong input', 'IN1 SIG STA wrong field', 'IN01 VID FMT INF vendor-specific 4K wording'] : undefined;
  const result = r.collect();
  assert.equal(result.values.input['1'].format, 'IN01 VID FMT INF vendor-specific 4K wording');
  assert.equal(result.values.output['4'].hpd, 'OUT4 HPD example raw reply');
  assert(r.requests.every(query => query.startsWith('GET '))); assert.equal(r.opened.length, 1);
});

test('AVPro unsupported grammar is unavailable, bounded, rotated, and keeps its one session', () => {
  const r = device('avpro_telnet'); r.poll(); assert(r.collect().values.input);
  r.requests.length = 0;
  r.state.override = request => request !== 'GET OUT0 VS' ? ['unidentified proprietary reply'] : undefined;
  for (let cycle = 0; cycle < 24; cycle += 1) {
    const result = r.collect(); assert.equal(result.reason, 'no_response'); assert.deepEqual(result.values, {});
    assert(!r.opened.at(-1).closed); r.poll();
  }
  assert.equal(r.opened.length, 1);
  for (let port = 1; port <= 4; port += 1) {
    for (const query of [`IN${port} VID FMT INF`, `IN${port} SIG STA`, `OUT${port} SIG STA`, `OUT${port} HPD`]) {
      assert(r.requests.includes(`GET ${query}`), `starved ${query}`);
    }
  }
  assert.equal(r.items.example_output1_input, '1');
});

test('AVPro takes a SET on its one session after a late or refused diagnostic reply', () => {
  const r = device('avpro_telnet'); r.poll(); r.requests.length = 0;
  const link = r.opened[0];
  let late = [];
  // The matrix answers in order: a reply that missed its window comes
  // before the answer to the next request.
  r.state.override = request => {
    link.lines.push(...late); late = [];
    if (request.startsWith('GET ') && request !== 'GET OUT0 VS') {
      late = [`${request.slice(4)} late reply`, 'CMD ERR'];
      return [];
    }
  };
  assert.equal(r.collect().reason, 'no_response');
  assert.deepEqual(r.requests, ['GET IN1 VID FMT INF', 'GET OUT0 VS']);
  assert(!link.closed); assert.deepEqual(link.lines, []);
  const result = r.context.routingProvider().execute('example_output1_input', '2');
  assert.equal(result.outcome, 'applied'); assert.equal(result.sample.value, '2');
  assert.equal(r.opened.length, 1); assert.equal(r.items.example_output1_input, '2');
});

test('AVPro ends its session only when the matrix answers neither a diagnostic read nor GET OUT0 VS', () => {
  const r = device('avpro_telnet'); r.poll();
  r.state.override = () => [];
  assert.equal(r.collect().reason, 'no_response'); assert(r.opened[0].closed);
  r.state.override = null; r.poll();
  assert.equal(r.opened.length, 2); assert.equal(r.items.example_output1_input, '1');
});

test('the next control reconnects and confirms its result after an optional timeout', () => {
  for (const [name, item, value, optional] of [
    ['vrroom_telnet', 'example_input', '2', request => request.startsWith('get status ')],
    ['sony_adcp', 'example_input', 'hdmi2', request => request === 'signal ?'],
  ]) {
    const r = device(name); r.poll();
    r.state.override = request => optional(request) ? [] : undefined;
    assert.equal(r.collect().status, 'unavailable'); assert(r.opened[0].closed);
    r.state.override = null;
    const result = r.context.routingProvider().execute(item, value);
    assert.equal(result.outcome, 'applied'); assert.equal(result.sample.value, value);
    assert.equal(r.opened.length, 2); assert.equal(r.items[item], value);
  }
});

test('an I/O exception advances rotating work before the next collection', () => {
  for (const name of ['vrroom_telnet', 'avpro_telnet']) {
    const r = device(name), attempted = []; r.poll();
    r.state.override = request => {
      if (request.startsWith('get status ') || (request.startsWith('GET ') && request !== 'GET OUT0 VS')) {
        attempted.push(request); throw Error('deadline expired during write');
      }
    };
    assert.equal(r.collect().reason, 'no_response'); r.poll();
    assert.equal(r.collect().reason, 'no_response');
    assert.equal(attempted.length, 2); assert.notEqual(attempted[0], attempted[1]);
  }
});

function http(data) { return JSON.parse(vm.runInNewContext(fixture.http, {input:JSON.stringify(data), Date:{now:() => 123456}})); }
test('HTTP fallback resolves native follower selections and excludes RX1 without a matrix mode', () => {
  const base = {portseltx0:'2', portseltx1:'4', RX0:'3840x2160', RX1:'stale second path', TX1:'1920x1080'};
  const result = http(base); assert.equal(result.values.selectedtx1, '2'); assert.equal(result.values.TX1, '1920x1080');
  assert(!('RX1' in result.values)); assert.equal(http({...base, opmode:'2'}).values.RX1, 'stale second path');
  const cycle = http({...base, portseltx0:'4'}); assert(!('selectedtx0' in cycle.values)); assert(!('selectedtx1' in cycle.values));
});

test('HTTP fallback preserves a failed acquisition envelope without inventing values', () => {
  const result = JSON.parse(vm.runInNewContext(fixture.http, {input:'<!--#infoPage-->', Date:{now:() => 123456}}));
  assert.equal(result.status, 'unavailable'); assert.equal(result.reason, 'invalid_response'); assert.deepEqual(result.values, {});
});

process.stdout.write(JSON.stringify({passed:tests.length, tests}));
