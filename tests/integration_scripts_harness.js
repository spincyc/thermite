'use strict';
// Runs production integration scripts against stub openHAB and Java types.
// argv: heos.js globalcache_ir.js bk808_cec.js androidtv_googletv.js
// avpro_telnet.js sony_adcp.js sony_ip_control.js. No device or network
// access: HTTP and sockets are fixtures.
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const [heosSource, irSource, cecSource, androidSource, avproSource, adcpSource, sonyTvSource] =
  process.argv.slice(2).map(path => fs.readFileSync(path, 'utf8'));
const tests = [];
function test(name, fn) { fn(); tests.push(name); }

// A minimal DOM for well-formed element/text XML, as the scripts walk it.
function parseXml(text) {
  const root = { type: 9, children: [] }, stack = [root];
  for (const token of text.replace(/<\?[^>]*\?>/g, '').match(/<[^>]+>|[^<]+/g) || []) {
    const top = stack[stack.length - 1];
    if (token.startsWith('</')) stack.pop();
    else if (token.startsWith('<')) { const node = { type: 1, name: token.slice(1, -1).trim(), children: [] }; top.children.push(node); stack.push(node); }
    else top.children.push({ type: 3, text: token });
  }
  const wrap = node => ({
    getNodeType: () => node.type, getNodeName: () => node.name,
    getChildNodes: () => ({ getLength: () => node.children.length, item: i => wrap(node.children[i]) }),
    getTextContent: () => node.type === 3 ? node.text : node.children.map(child => wrap(child).getTextContent()).join(''),
    getDocumentElement: () => wrap(node.children.find(child => child.type === 1)),
  });
  return wrap(root);
}

function openhabStub({ things = {}, items = {}, rules }) {
  const posted = [];
  return {
    posted,
    module: {
      items: { getItem: (name, nullable) => {
        if (!(name in items)) { if (nullable) return null; items[name] = 'NULL'; }
        return { get state() { return items[name]; }, postUpdate(value) { posted.push([name, String(value)]); items[name] = String(value); } };
      } },
      things: { getThing: uid => things[uid] || null },
      rules: { JSRule(rule) { rules[rule.id] = rule; } },
      triggers: { ItemCommandTrigger: name => ({ name }), ThingStatusChangeTrigger: uid => ({ uid }), GenericCronTrigger: cron => ({ cron }) },
    },
  };
}

function thing(status, description = '', extra = {}) {
  const handled = [];
  const value = { status, handled, enabled: [], description,
    rawThing: { getHandler: () => value.handler, getStatusInfo: () => ({ getDescription: () => value.description }),
      getConfiguration: () => ({ get: key => ({ ipAddress: '192.0.2.20', googletvPort: '6466' })[key] }) },
    handler: { handleCommand(uid, command) { handled.push([uid.uid, command.value]); } },
    setEnabled(enabled) { value.enabled.push(enabled); },
  };
  return Object.assign(value, extra);
}

function run(source, table, { things, items, java = {}, clock, globals = {}, runtime = null }) {
  const rules = {}, logs = [];
  const openhab = openhabStub({ things, items, rules });
  const types = Object.assign({
    'org.openhab.core.thing.ChannelUID': class { constructor(uid) { this.uid = uid; } },
    'org.openhab.core.library.types.StringType': class { constructor(value) { this.value = value; } },
  }, java);
  const context = vm.createContext({
    require: name => {
      if (runtime && name === '@runtime') return runtime;
      assert.equal(name, 'openhab'); return openhab.module;
    },
    Java: { type: name => types[name] || assert.fail('unexpected Java type ' + name) },
    console: { info: m => logs.push(['info', m]), warn: m => logs.push(['warn', m]), error: m => logs.push(['error', m]) },
    Date: { now: () => clock.now }, JSON, Map, Set, Object, String, Number, Boolean, Array, Error, Math, RegExp,
    ...globals,
  });
  let text = source;
  for (const [mark, value] of Object.entries(table)) text = text.replace(`const ${mark} = null;`, `const ${mark} = ${JSON.stringify(value)};`);
  vm.runInContext(text, context);
  return { rules, logs, posted: openhab.posted, context };
}

const VERIFIED = name => '<?xml version="1.0" encoding="utf-8"?>\r\n<tx>\r\n<cmd id="1">' + name + '</cmd>\r\n</tx>\r\n';

function heos(replies) {
  const requests = [];
  const java = {
    'java.net.URL': class { constructor(url) { this.url = url; }
      openConnection() {
        let body = '', reply = null, offset = 0;
        return { setConnectTimeout() {}, setReadTimeout() {}, setInstanceFollowRedirects() {}, setUseCaches() {},
          setRequestMethod() {}, setDoOutput() {}, setRequestProperty() {}, setFixedLengthStreamingMode() {}, disconnect() {},
          getOutputStream: () => ({ write: bytes => { body += Buffer.from(bytes).toString('utf8'); }, close() {} }),
          getResponseCode: () => { requests.push(body); reply = Buffer.from(replies(body), 'utf8'); return 200; },
          getContentLengthLong: () => reply.length,
          getInputStream: () => ({ read(buffer, start, length) {
            if (offset >= reply.length) return -1;
            const count = reply.copy(buffer, start, offset, Math.min(reply.length, offset + length)); offset += count; return count;
          }, close() {} }) };
      } },
    'java.net.Proxy': { NO_PROXY: {} },
    'java.nio.charset.StandardCharsets': { UTF_8: 'utf8' },
    'java.lang.String': class { constructor(text) { this.text = text; } getBytes() { return Buffer.from(this.text, 'utf8'); } },
    'java.io.ByteArrayOutputStream': class { constructor() { this.parts = []; }
      write(buffer, start, count) { this.parts.push(Buffer.from(buffer.subarray(start, start + count))); }
      size() { return Buffer.concat(this.parts).length; } toByteArray() { return Buffer.concat(this.parts); } },
    'java.lang.System': { nanoTime: () => Number(process.hrtime.bigint()) },
    'byte[]': function (length) { return Buffer.alloc(length); },
    'java.io.ByteArrayInputStream': class { constructor(bytes) { this.bytes = bytes; } close() {} },
    'javax.xml.parsers.DocumentBuilderFactory': { newInstance: () => ({ setFeature() {}, setAttribute() {}, setXIncludeAware() {},
      setExpandEntityReferences() {}, newDocumentBuilder: () => ({ setErrorHandler() {}, parse: input => parseXml(Buffer.from(input.bytes).toString('utf8')) }) }) },
    'org.xml.sax.helpers.DefaultHandler': class {},
    'javax.xml.XMLConstants': { FEATURE_SECURE_PROCESSING: 'secure', ACCESS_EXTERNAL_DTD: 'dtd', ACCESS_EXTERNAL_SCHEMA: 'schema' },
  };
  const things = { 'heos:player:1': thing('ONLINE'), 'denon:avr:1': thing('ONLINE') };
  const items = { avr_main_power: 'ON', avr_main_input: 'NET', avr_heos_playback: 'NULL' };
  const loaded = run(heosSource, { DEVICES: [{ id: 'avr', thing: 'heos:player:1', receiver: 'denon:avr:1',
    readURL: 'http://192.0.2.10:8080/goform/AppCommand.xml' }] }, { things, items, java, clock: { now: 0 } });
  loaded.rules.heos_avr_transport.execute({ itemName: 'avr_key', receivedCommand: 'play' });
  return { requests, handled: things['heos:player:1'].handled, logs: loaded.logs };
}

test('HEOS reads the receiver one verified AppCommand at a time before a transport press', () => {
  // Like the firmware: anything but the verified serialization gets <rx> empty.
  const firmware = (power, source) => body => body === VERIFIED('GetAllZonePowerStatus') ? `<rx><cmd><zone1>${power}</zone1><zone2>ON</zone2></cmd></rx>` :
    body === VERIFIED('GetSourceStatus') ? `<rx><cmd><source>${source}</source></cmd></rx>` : '<rx>\n</rx>';
  const pressed = heos(firmware('ON', 'NET'));
  assert.deepEqual(pressed.requests, [VERIFIED('GetAllZonePowerStatus'), VERIFIED('GetSourceStatus')]);
  assert.deepEqual(pressed.handled, [['heos:player:1:Control', 'PLAY']]);
  for (const [power, source] of [['OFF', 'NET'], ['ON', 'TUNER']]) {
    const refused = heos(firmware(power, source));
    assert.deepEqual(refused.handled, []);
    assert(refused.logs.some(([level, message]) => level === 'warn' && /fresh main-zone ON/.test(message)));
  }
});

function keys(source, table, uid, command, clock, status = 'ONLINE') {
  const things = { [uid]: thing(status) };
  const loaded = run(source, table, { things, items: {}, clock });
  const rule = Object.values(loaded.rules)[0];
  return { send: value => rule.execute({ receivedCommand: value }), logs: loaded.logs, thing: things[uid] };
}

test('IR and CEC keys that cannot be sent warn once a minute per device and reason, without the key', () => {
  for (const [source, table, uid, good] of [
    [irSource, { DEVICES: [{ id: 'bluray', thing: 'globalcache:itachIR:1', channel: 'ir-m1#c1', keys: ['PLAY'] }] }, 'globalcache:itachIR:1', 'PLAY'],
    [cecSource, { DEVICES: [{ id: 'player', thing: 'bk808:cec:1', requests: { PLAY: 'cec play' } }] }, 'bk808:cec:1', 'PLAY']]) {
    const clock = { now: 1000 }, r = keys(source, table, uid, good, clock);
    r.send('SECRETKEY'); r.send('SECRETKEY');
    r.thing.status = 'OFFLINE'; r.send(good); r.send(good);
    let warnings = r.logs.filter(([level]) => level === 'warn');
    assert.equal(warnings.length, 2, source.slice(0, 40));
    assert(/not a/.test(warnings[0][1])); assert(new RegExp(`${uid} is OFFLINE`).test(warnings[1][1]));
    assert(!r.logs.some(([, message]) => message.includes('SECRETKEY')));
    clock.now += 60000; r.send('SECRETKEY');
    warnings = r.logs.filter(([level]) => level === 'warn'); assert.equal(warnings.length, 3);
    r.thing.status = 'ONLINE'; r.send(good); assert.equal(r.thing.handled.length, 1);
  }
});

function android(options = {}) {
  const clock = { now: 1000000 }, files = new Set(options.files || []), moves = [];
  const tv = thing(options.status || 'OFFLINE', options.description || 'PIN Process Incomplete');
  const java = {
    'java.net.InetSocketAddress': class { constructor(host, port) { this.host = host; this.port = port; } },
    'java.net.Socket': class { connect() {} close() {} },
    'java.nio.file.Files': {
      exists: path => files.has(String(path)),
      move(from, to) { moves.push([String(from), String(to)]); if (options.moveFails) throw new Error('Device or resource busy'); files.delete(String(from)); files.add(String(to)); },
      deleteIfExists: path => files.delete(String(path)),
    },
    'java.nio.file.Paths': { get: path => path },
    'java.nio.file.StandardCopyOption': { REPLACE_EXISTING: 'REPLACE_EXISTING' },
    'org.openhab.core.OpenHAB': { getUserDataFolder: () => '/srv/openhab/userdata' },
    'java.lang.System': { getProperty: () => null },
  };
  const loaded = run(androidSource, { DEVICES: [{ id: 'tv', thing: 'androidtv:googletv:tv', remote: 'android', volume: options.volume || null,
    keystore: 'androidtv/googletv.tv.keystore' }], PRESSES: { android: {} } },
  { things: { 'androidtv:googletv:tv': tv }, items: { tv_pairing_status: 'NULL', tv_key: 'NULL', tv_pairing: 'NULL', ...options.items }, java, clock });
  const status = () => { const rows = loaded.posted.filter(([name]) => name === 'tv_pairing_status'); return rows.length ? rows[rows.length - 1][1] : null; };
  return { clock, files, moves, tv, logs: loaded.logs, status,
    poll: () => loaded.rules.androidtv_googletv_pairing_poll.execute({}),
    pair: command => loaded.rules.androidtv_googletv_tv_command.execute({ itemName: 'tv_pairing', receivedCommand: command }),
    volume: command => loaded.rules.androidtv_googletv_tv_command.execute({ itemName: 'tv_android_volume', receivedCommand: command }) };
}

test('an Android TV volume is stepped anywhere on its own scale and never clamped', () => {
  const presses = (r, from) => r.tv.handled.slice(from).map(([channel, code]) => channel + ' ' + code);
  const r = android({ status: 'ONLINE', description: '', volume: { min: 0, max: 100, step: 1, unit: '%' }, items: { tv_android_volume: '98' } });
  r.volume('100');
  assert.deepEqual(presses(r, 0), ['androidtv:googletv:tv:keycode 24', 'androidtv:googletv:tv:keycode 24']);
  for (const command of ['101', '150', '-1', '99.5', 'INCREASE']) {
    const before = r.tv.handled.length;
    r.volume(command);
    assert.equal(r.tv.handled.length, before, command + ' reached the TV');
  }
  assert(r.logs.some(([level, message]) => level === 'warn' && message.includes("'101' is not a volume on its scale")));
  r.clock.now += 10000; r.volume('0');
  assert.equal(presses(r, 2).length, 98); assert(presses(r, 2).every(press => press === 'androidtv:googletv:tv:keycode 25'));
});

test('a missing-PIN report right after Thing start is checking until it lasts two minutes', () => {
  const r = android();
  r.poll(); assert.equal(r.status(), 'checking');
  r.clock.now += 119999; r.poll(); assert.equal(r.status(), 'checking');
  r.clock.now += 1; r.poll(); assert.equal(r.status(), 'unpaired');
  r.tv.status = 'ONLINE'; r.poll(); assert.equal(r.status(), 'paired');
  r.tv.status = 'OFFLINE'; r.clock.now += 5000; r.poll(); assert.equal(r.status(), 'checking');
  r.tv.description = 'Connection refused'; r.poll(); assert.equal(r.status(), 'unreachable');
});

test('a failed restore of the previous keystore stops retrying and shows failed until CANCEL or a manual fix', () => {
  const keystore = '/srv/openhab/userdata/androidtv/googletv.tv.keystore', previous = keystore + '.previous';
  const r = android({ status: 'ONLINE', description: '', files: [keystore, previous], moveFails: true });
  r.poll();
  assert.deepEqual(r.moves, [[previous, keystore]]); assert.deepEqual(r.tv.enabled, [false, true]);
  const errors = () => r.logs.filter(([level]) => level === 'error');
  assert.equal(errors().length, 1); assert(errors()[0][1].includes(previous)); assert(errors()[0][1].includes(keystore));
  assert.equal(r.status(), 'failed');
  for (let n = 0; n < 5; n++) { r.clock.now += 5000; r.poll(); }
  assert.equal(r.moves.length, 1); assert.equal(r.tv.enabled.length, 2); assert.equal(errors().length, 1); assert.equal(r.status(), 'failed');
  r.pair('REQUEST'); assert.equal(r.moves.length, 1); assert.deepEqual(r.tv.handled, []);
  r.pair('CANCEL'); assert.equal(r.moves.length, 2); assert.equal(errors().length, 2); assert.equal(r.status(), 'failed');
  r.files.delete(previous); r.poll();
  assert(r.logs.some(([level, message]) => level === 'info' && message.includes('is gone'))); assert.equal(r.status(), 'paired');
});

// An AVPro matrix on telnet. On connect it lists its routes and audio
// levels; it answers GET OUT0 VS with every route, SET with the new route,
// and a SET it refuses (`error`) with CMD ERR. `refuse` makes it accept a
// connection and close it at once, `mute` close one at its next request,
// unanswered, and `hangup` close one at that request, unanswered. `silent`
// takes a SET without answering it. close() closes every connection while
// idle. `format` shapes each answer.
function avpro({ send = true } = {}) {
  const clock = { now: 0 }, hooks = [];
  const matrix = { refuse: false, mute: false, hangup: null, silent: false, error: false, routes: [1, 1, 1, 1], opened: [],
    requests: [], links: [],
    format: line => line, close() { for (const link of matrix.links) link.peerClosed = true; } };
  const listing = () => [...matrix.routes.map((input, i) => `OUT${i + 1} VS IN${input}`),
    ...matrix.routes.map((_, i) => `OUT${i + 1} EXAUD LEV100`)];
  const thermiteIO = {
    deadline: ms => clock.now + ms,
    remaining: end => { if (end <= clock.now) throw Error('deadline'); return end - clock.now; },
    open() {
      matrix.opened.push(clock.now / 10000);
      const link = { closed: false, peerClosed: matrix.refuse, lines: matrix.refuse ? [] : listing() };
      matrix.links.push(link);
      return link;
    },
    renew(link, end) { link.deadline = end; },
    line(link) {
      if (link.lines.length) return link.lines.shift();
      if (link.peerClosed) { link.closed = true; return null; }
      throw Error('quiet');
    },
    quietTimeout: () => true,
    write(link, text) {
      if (link.closed || link.peerClosed) throw Error('closed');
      const request = text.replace(/\r\n$/, '');
      matrix.requests.push(request);
      if (matrix.mute || request === matrix.hangup) { link.peerClosed = true; return; }
      const set = /^SET OUT(\d+) VS IN(\d+)$/.exec(request);
      if (set && matrix.error) link.lines.push('CMD ERR');
      else if (set && matrix.silent) matrix.routes[set[1] - 1] = Number(set[2]);
      else if (set) { matrix.routes[set[1] - 1] = Number(set[2]); link.lines.push(matrix.format(`SET OUT${set[1]} VS IN${set[2]}`)); }
      else if (request === 'GET OUT0 VS') link.lines.push(...matrix.routes.map((input, i) => matrix.format(`OUT${i + 1} VS IN${input}`)));
    },
    close(link) { link.closed = true; },
  };
  const items = {};
  const source = send ? avproSource : avproSource.replace('const SEND = true;', 'const SEND = false;');
  const loaded = run(source.replace('// THERMITE_BOUNDED_IO', ''), { DEVICES: [{ id: 'matrix', host: '192.0.2.30', port: 23,
    inputs: 4, outputs: 4 }] }, { items, clock, globals: { thermiteIO },
    runtime: { lifecycleTracker: { addDisposeHook: hook => hooks.push(hook) } } });
  const warnings = () => loaded.logs.filter(([level]) => level === 'warn').map(([, message]) => message);
  return { ...loaded, clock, matrix, items, warnings, unload: () => hooks.forEach(hook => hook()),
    poll: () => { loaded.rules.avpro_telnet_matrix_poll.execute(); clock.now += 10000; },
    command: (output, input) => loaded.rules.avpro_telnet_matrix_command.execute({
      itemName: `matrix_output${output}_input`, receivedCommand: String(input) }) };
}

test('an AVPro matrix is read every 10 s and switched on one kept connection, closed when the script unloads', () => {
  const r = avpro();
  r.poll(); r.poll(); r.poll();
  assert.deepEqual(r.matrix.opened, [0]); assert.deepEqual(r.matrix.requests, Array(3).fill('GET OUT0 VS'));
  assert.equal(r.items.matrix_output4_input, '1');
  r.command(4, 2);
  assert.deepEqual(r.matrix.opened, [0]); assert.deepEqual(r.matrix.requests.slice(3), ['SET OUT4 VS IN2', 'GET OUT0 VS']);
  assert.equal(r.items.matrix_output4_input, '2');
  // A line it sent unasked, since superseded, does not answer the next read.
  r.matrix.links[0].lines.push('OUT4 VS IN3', 'OUT3 VS IN4'); r.matrix.routes[2] = 4;
  r.poll();
  assert.equal(r.items.matrix_output4_input, '2'); assert.equal(r.items.matrix_output3_input, '4');
  assert.deepEqual(r.warnings(), []);
  r.unload();
  assert(r.matrix.links[0].closed);
});

test('a connection the AVPro closed while idle is replaced in the poll; a refusing one is tried after 10, 20, 40 s, then every minute', () => {
  const r = avpro();
  r.poll(); r.matrix.close(); r.poll();
  assert.deepEqual(r.matrix.opened, [0, 1]); assert.deepEqual(r.warnings(), []); assert.equal(r.items.matrix_output4_input, '1');
  r.matrix.refuse = true; r.matrix.close();
  for (let poll = 2; poll < 22; poll++) r.poll();
  assert.deepEqual(r.matrix.opened.slice(2), [2, 3, 5, 9, 15, 21]);
  assert.equal(r.items.matrix_output4_input, 'UNDEF');
  assert.deepEqual(r.warnings(), ['matrix: closed the connection; its Items are UNDEF until it answers']);
  // A switch connects at once, and ends the backoff.
  r.matrix.refuse = false; r.command(4, 2);
  assert.deepEqual(r.matrix.opened.slice(8), [22]); assert.equal(r.items.matrix_output4_input, '2');
  assert(r.logs.some(([level, message]) => level === 'info' && message === 'matrix: answering over telnet again'));
  r.poll(); r.poll();
  assert.deepEqual(r.matrix.opened.slice(8), [22]); assert.deepEqual(r.matrix.requests.slice(-2), ['GET OUT0 VS', 'GET OUT0 VS']);
});

test('AVPro answers may carry A00, repeat SET or zero-pad; CMD ERR and a matrix that stops answering are failures', () => {
  const r = avpro();
  r.matrix.format = line => 'A00 ' + line.replace(/(OUT|IN)(\d)/g, '$10$2');
  r.command(4, 3);
  assert.equal(r.items.matrix_output4_input, '3'); assert.deepEqual(r.warnings(), []);
  r.matrix.error = true; r.command(4, 2);
  assert.deepEqual(r.warnings(), ['matrix: answered SET OUT4 VS IN2 with CMD ERR', 'matrix: output 4 shows input 3 after SET OUT4 VS IN2']);
  assert.equal(r.items.matrix_output4_input, '3');
  r.matrix.mute = true; r.poll();
  assert.equal(r.items.matrix_output4_input, 'UNDEF'); assert(r.matrix.links[0].closed);
  assert.equal(r.warnings()[2], 'matrix: closed the connection; its Items are UNDEF until it answers');
  r.matrix.mute = false; r.poll();
  assert.deepEqual(r.matrix.opened, [0, 1]); assert.equal(r.items.matrix_output4_input, '3');
});

test('every AVPro poll posts each output\'s input, changed or not, so a reading stays fresh', () => {
  const r = avpro();
  const posts = name => r.posted.filter(([item]) => item === name).map(([, value]) => value);
  r.poll(); r.poll(); r.poll();
  for (let output = 1; output <= 4; output++) assert.deepEqual(posts(`matrix_output${output}_input`), ['1', '1', '1']);
  r.matrix.routes[3] = 2; r.poll();
  assert.deepEqual(posts('matrix_output4_input'), ['1', '1', '1', '2']);
  // A matrix that stops answering has UNDEF posted at each poll that tries it.
  r.matrix.mute = true; r.poll(); r.matrix.mute = false; r.matrix.refuse = true; r.poll();
  assert.deepEqual(posts('matrix_output4_input').slice(4), ['UNDEF', 'UNDEF']);
});

test('the AVPro routing provider is unsent before SET, refused on CMD ERR, unknown without its answer, applied once answered', () => {
  const r = avpro(), provider = r.context.routingProvider();
  r.poll();
  assert.deepEqual(provider.execute('matrix_output4_input', '2'),
    { outcome: 'applied', sample: { value: '2', fresh: true, basis: 'owned_native_read', observedAt: 10000 } });
  // CMD ERR is the matrix declining: terminal, never re-read as a slow switch.
  r.matrix.error = true;
  assert.deepEqual(provider.execute('matrix_output4_input', '3'), { outcome: 'refused' });
  assert.equal(r.items.matrix_output4_input, '2');
  r.matrix.error = false;
  // A SET with no answer, whether the matrix stays connected or hangs up, may
  // still take effect: unknown, even when the read after it succeeds.
  r.matrix.silent = true;
  assert.deepEqual(provider.execute('matrix_output4_input', '3'), { outcome: 'unknown' });
  r.matrix.silent = false; r.matrix.hangup = 'SET OUT4 VS IN4';
  assert.deepEqual(provider.execute('matrix_output4_input', '4'), { outcome: 'unknown' });
  // An answered SET whose read then fails is applied; the executor re-reads.
  r.matrix.hangup = 'GET OUT0 VS';
  assert.deepEqual(provider.execute('matrix_output4_input', '1'), { outcome: 'applied', sample: null });
  assert.equal(r.matrix.routes[3], 1);
  // No connection, so no SET byte: unsent.
  r.matrix.hangup = null; r.matrix.refuse = true; r.matrix.close();
  assert.deepEqual(provider.execute('matrix_output4_input', '2'), { outcome: 'unsent' });
  assert.deepEqual(r.matrix.requests.filter(line => line.startsWith('SET')),
    ['SET OUT4 VS IN2', 'SET OUT4 VS IN3', 'SET OUT4 VS IN3', 'SET OUT4 VS IN4', 'SET OUT4 VS IN1']);
});

test('a read-only copy connects to no AVPro matrix and only logs what it would send', () => {
  const r = avpro({ send: false });
  r.poll(); r.poll(); r.command(4, 2);
  assert.deepEqual(r.matrix.opened, []); assert.deepEqual(r.posted, []);
  assert(r.logs.some(([level, message]) => level === 'info' && message === 'matrix: SEND is false, not sending SET OUT4 VS IN2'));
});

// A Sony projector on ADCP. It greets each connection with `greeting`
// (NOKEY: no authentication), then answers one request at a time:
// power_status, input and, while it is on, picture_mode with a quoted
// value; power, input and key with ok, or with `refuse` (an err_* token);
// anything else with err_cmd. `down` refuses connections. `mute` answers
// nothing, and `asleep` that many requests, as `wake` does the first request
// of that many coming connections; such an answer comes late, after the
// read that waited for it gave up. `errors` answers that many requests
// err_cmd, and `hangup` closes a connection at that request, unanswered.
// `after` is the power_status a power "on" / "off" leaves (on, standby).
// close() closes every connection while idle; cycle() forgets them, as a
// power cycle does, so that the next request on one is reset.
function adcp({ send = true } = {}) {
  const clock = { now: 0 }, hooks = [];
  const projector = { power: 'on', input: 'hdmi1', mode: 'reference', greeting: 'NOKEY', refuse: null, down: false,
    mute: false, asleep: 0, wake: 0, errors: 0, hangup: null, after: {on: 'on', off: 'standby'}, opened: [], requests: [], links: [],
    close() { for (const link of projector.links) link.peerClosed = true; },
    cycle() { for (const link of projector.links) link.forgotten = true; } };
  function answer(request) {
    const set = /^(power|input|key) "([^"]*)"$/.exec(request);
    if (set && projector.refuse) return projector.refuse;
    if (set && set[1] === 'power') projector.power = projector.after[set[2] === 'on' ? 'on' : 'off'];
    else if (set && set[1] === 'input') projector.input = set[2];
    if (set) return 'ok';
    if (request === 'power_status ?') return `"${projector.power}"`;
    if (request === 'input ?') return `"${projector.input}"`;
    if (request === 'picture_mode ?') return projector.power === 'on' ? `"${projector.mode}"` : 'err_inactive';
    return 'err_cmd';
  }
  const thermiteIO = {
    deadline: ms => clock.now + ms,
    open() {
      if (projector.down) throw Error('refused');
      projector.opened.push(clock.now / 15000);
      const link = { closed: false, peerClosed: false, forgotten: false, reset: false, waking: projector.wake > 0,
        lines: [projector.greeting], late: [] };
      if (link.waking) projector.wake -= 1;
      projector.links.push(link);
      return link;
    },
    renew(link, end) { link.deadline = end; },
    line(link) {
      if (link.closed) return null;
      if (link.lines.length) return link.lines.shift();
      if (link.reset) { link.closed = true; throw Error('reset'); }
      if (link.peerClosed) { link.closed = true; return null; }
      link.lines.push(...link.late.splice(0));
      throw Error('quiet');
    },
    quietTimeout: link => !link.closed,
    write(link, text) {
      if (link.closed) throw Error('closed');
      if (link.forgotten) link.reset = true;
      if (link.reset || link.peerClosed) return;
      const request = text.replace(/\r\n$/, '');
      projector.requests.push(request);
      if (request === projector.hangup) { link.peerClosed = true; return; }
      const late = projector.mute || projector.asleep > 0 || link.waking;
      if (projector.asleep > 0) projector.asleep -= 1;
      link.waking = false;
      if (!late && projector.errors > 0) { projector.errors -= 1; link.lines.push('err_cmd'); }
      else (late ? link.late : link.lines).push(answer(request));
    },
    close(link) { link.closed = true; },
  };
  const items = {};
  const source = send ? adcpSource : adcpSource.replace('const SEND = true;', 'const SEND = false;');
  const loaded = run(source.replace('// THERMITE_BOUNDED_IO', ''), { DEVICES: [{ id: 'projector', host: '192.0.2.31',
    port: 53595, control: true }], KEYS: { menu: 'menu' }, INPUTS: ['hdmi1', 'hdmi2'] }, { items, clock, globals: { thermiteIO },
    runtime: { lifecycleTracker: { addDisposeHook: hook => hooks.push(hook) } } });
  const warnings = () => loaded.logs.filter(([level]) => level === 'warn').map(([, message]) => message);
  return { ...loaded, clock, projector, items, warnings, unload: () => hooks.forEach(hook => hook()),
    poll: () => { loaded.rules.sony_adcp_projector_poll.execute(); clock.now += 15000; },
    command: (point, value) => loaded.rules.sony_adcp_projector_command.execute({
      itemName: `projector_${point}`, receivedCommand: value }) };
}

const READS = ['power_status ?', 'input ?', 'picture_mode ?'];

test('a Sony projector is read every 15 s and commanded on one kept ADCP connection, closed when the script unloads', () => {
  const r = adcp();
  r.poll(); r.poll(); r.poll();
  assert.deepEqual(r.projector.opened, [0]); assert.deepEqual(r.projector.requests, Array(3).fill(READS).flat());
  assert.deepEqual([r.items.projector_power, r.items.projector_input, r.items.projector_picture_mode], ['ON', 'hdmi1', 'reference']);
  r.command('input', 'hdmi2');
  assert.deepEqual(r.projector.opened, [0]);
  assert.deepEqual(r.projector.requests.slice(9), ['power_status ?', 'input "hdmi2"', ...READS]);
  assert.equal(r.items.projector_input, 'hdmi2');
  // In standby it answers on the same connection; picture_mode is not asked.
  r.projector.power = 'standby'; r.poll();
  assert.deepEqual(r.projector.requests.slice(14), ['power_status ?', 'input ?']);
  assert.deepEqual([r.items.projector_power, r.items.projector_picture_mode], ['OFF', 'UNDEF']);
  assert.deepEqual(r.projector.opened, [0]); assert.deepEqual(r.warnings(), []);
  r.unload();
  assert(r.projector.links[0].closed);
});

test('a connection the projector closed while idle, or lost to a power cycle, is replaced within the poll', () => {
  const r = adcp();
  r.poll(); r.poll();
  r.projector.close(); r.poll(); r.poll();
  r.projector.cycle(); r.poll(); r.poll();
  assert.deepEqual(r.projector.opened, [0, 2, 4]); assert.deepEqual(r.projector.requests, Array(6).fill(READS).flat());
  assert(!r.posted.some(([, state]) => state === 'UNDEF')); assert.deepEqual(r.warnings(), []);
});

test('a projector waking from standby is asked again: after err_cmd on the same connection, after silence on a new one', () => {
  const r = adcp();
  r.poll();
  r.projector.errors = 1; r.poll();
  assert.deepEqual(r.projector.requests.slice(3), ['power_status ?', ...READS]);
  // Silent on the kept connection, then on the first request of a new one.
  r.projector.asleep = 1; r.poll();
  r.projector.close(); r.projector.wake = 1; r.poll(); r.poll();
  assert.deepEqual(r.projector.opened, [0, 2, 3, 3]);
  assert(!r.posted.some(([, state]) => state === 'UNDEF')); assert.deepEqual(r.warnings(), []);
});

test('a projector that does not answer is UNDEF and tried after 15 and 30 s, then every minute; a command connects at once', () => {
  const r = adcp();
  r.poll();
  r.projector.mute = true;
  for (let poll = 1; poll < 14; poll++) r.poll();
  // The kept connection, then each try's, is asked once more on a new one.
  assert.deepEqual(r.projector.opened, [0, 1, 2, 2, 4, 4, 8, 8, 12, 12]);
  assert.equal(r.items.projector_power, 'UNDEF');
  assert.deepEqual(r.warnings(), ['projector: does not answer power_status ?; its Items are UNDEF until it answers']);
  r.projector.mute = false; r.command('power', 'OFF');
  assert.deepEqual(r.projector.opened.slice(10), [14]); assert.equal(r.items.projector_power, 'OFF');
  assert(r.logs.some(([level, message]) => level === 'info' && message === 'projector: answering over ADCP again'));
  r.poll(); r.poll();
  assert.deepEqual(r.projector.opened.slice(10), [14]);
});

test('a projector that closes the connection while answering, or asks for a password, is UNDEF and backed off', () => {
  const r = adcp();
  r.poll();
  r.projector.hangup = 'input ?'; r.poll();
  assert.equal(r.items.projector_power, 'UNDEF'); assert(r.projector.links[0].closed);
  assert.deepEqual(r.warnings(), ['projector: closed the connection; its Items are UNDEF until it answers']);
  r.projector.hangup = null; r.poll(); r.poll();
  assert.deepEqual(r.projector.opened, [0, 2]); assert.equal(r.items.projector_power, 'ON');
  // Its challenge is never answered, and the greeting is read on connect only.
  const locked = adcp();
  locked.projector.greeting = '9f86d081';
  for (let poll = 0; poll < 5; poll++) locked.poll();
  assert.deepEqual(locked.projector.opened, [0, 1, 3]); assert.deepEqual(locked.projector.requests, []);
  assert.deepEqual(locked.warnings(), ['projector: asks for unsupported ADCP authentication; its Items are UNDEF until it answers']);
});

test('the routing provider is unsent before the command line, refused or unknown after it, and probes at once', () => {
  const r = adcp(), provider = r.context.routingProvider();
  r.poll();
  assert.deepEqual(provider.execute('projector_input', 'hdmi2'),
    { outcome: 'applied', sample: { value: 'hdmi2', fresh: true, basis: 'owned_native_read', observedAt: 15000 } });
  r.projector.refuse = 'err_inactive';
  assert.deepEqual(provider.execute('projector_input', 'hdmi1'), { outcome: 'refused' });
  assert.deepEqual(r.projector.opened, [0]);
  // The kept connection lost and no new one: nothing was sent.
  r.projector.refuse = null; r.projector.cycle(); r.projector.down = true;
  assert.deepEqual(provider.execute('projector_power', 'OFF'), { outcome: 'unsent' });
  r.projector.down = false; r.projector.mute = true;
  assert.deepEqual(provider.execute('projector_power', 'OFF'), { outcome: 'unsent' });
  r.projector.mute = false;
  const opened = r.projector.opened.length;
  assert.equal(provider.probe('projector_power').value, 'ON'); assert.equal(r.projector.opened.length, opened + 1);
  r.projector.hangup = 'input "hdmi1"';
  assert.deepEqual(provider.execute('projector_input', 'hdmi1'), { outcome: 'unknown' });
  // Taken, but its read-back closed: applied, unconfirmed.
  r.projector.hangup = 'input ?';
  assert.deepEqual(provider.execute('projector_power', 'OFF'), { outcome: 'applied', sample: null });
  assert.deepEqual(r.projector.requests.filter(line => !line.endsWith(' ?')),
    ['input "hdmi2"', 'input "hdmi1"', 'input "hdmi1"', 'power "off"']);
});

test('the routing provider reads startup and cooling as power transitions toward ON and OFF', () => {
  const r = adcp(), provider = r.context.routingProvider();
  r.poll();
  const read = (status, resource = 'projector_power') => { r.projector.power = status; return provider.probe(resource); };
  assert.deepEqual(read('startup'), { value: 'ON', fresh: true, basis: 'owned_native_read', observedAt: 15000, transition: true });
  assert.deepEqual(read('cooling1'), { value: 'OFF', fresh: true, basis: 'owned_native_read', observedAt: 15000, transition: true });
  assert.equal(read('cooling2').transition, true);
  // Settled power, and the input at any time, carry no flag.
  assert(!('transition' in read('on'))); assert(!('transition' in read('standby')));
  assert(!('transition' in read('startup', 'projector_input')));
  // The Items keep their display mapping and the raw power_status.
  assert.deepEqual([r.items.projector_power, r.items.projector_power_state], ['ON', 'startup']);
  read('cooling2');
  assert.deepEqual([r.items.projector_power, r.items.projector_power_state], ['OFF', 'cooling2']);
  // A power command the projector answers with its startup or cooling is
  // applied, with that transition as its readback.
  r.projector.power = 'standby'; r.projector.after = { on: 'startup', off: 'cooling1' };
  assert.deepEqual(provider.execute('projector_power', 'ON'),
    { outcome: 'applied', sample: { value: 'ON', fresh: true, basis: 'owned_native_read', observedAt: 15000, transition: true } });
  r.projector.power = 'on';
  assert.deepEqual(provider.execute('projector_power', 'OFF').sample.transition, true);
});

test('a read-only copy connects to no Sony projector and only logs what it would send', () => {
  const r = adcp({ send: false });
  r.poll(); r.poll(); r.command('power', 'OFF'); r.command('key', 'menu'); r.unload();
  assert.deepEqual(r.projector.opened, []); assert.deepEqual(r.posted, []);
  assert.deepEqual(r.logs, [['info', 'projector: SEND is false, not sending power "off"'],
    ['info', 'projector: SEND is false, not sending key "menu"']]);
});

// A Sony BRAVIA TV on IP control, paired: its registration is in userdata.
// It answers getPowerStatus with `power` (active or standby), and while it is
// active getPlayingContentInfo with HDMI `input` and getVolumeInformation
// with `volume` and `mute`; in standby Sony's error 40005. Its power Item is
// sony_rest's, so a test sets it.
function sonyTv() {
  const clock = { now: 0 }, day = 24 * 60 * 60 * 1000;
  const tv = { power: 'active', input: 2, volume: 20, mute: false, requests: [] };
  const answer = body => ({ getStatus: () => 200, getContentAsString: () => JSON.stringify(Object.assign({ id: 1 }, body)),
    getHeaders: () => ({ getValuesList: () => ({ size: () => 0, get: () => null }) }) });
  const thermiteIO = {
    unreached: () => false,
    http(url, method, body) {
      const request = JSON.parse(body).method;
      tv.requests.push(request);
      if (request === 'getPowerStatus') return answer({ result: [{ status: tv.power }] });
      if (tv.power !== 'active') return answer({ error: [40005, 'Display Is Turned off'] });
      if (request === 'getPlayingContentInfo') return answer({ result: [{ uri: `extInput:hdmi?port=${tv.input}` }] });
      if (request === 'getVolumeInformation') {
        return answer({ result: [[{ target: 'speaker', volume: tv.volume, mute: tv.mute, minVolume: 0, maxVolume: 100 }]] });
      }
      return answer({ result: [] });
    },
  };
  // A fictional session id; no PIN is stored, and none is needed to read.
  const session = '00000000-0000-4000-8000-000000000002';
  const registration = JSON.stringify({ clientid: 'openHAB:00000000-0000-4000-8000-000000000000', nickname: 'thermite',
    pin: null, cookie: session, expires: 30 * day, refused: false });
  const path = { resolve: () => path, toString: () => 'userdata/sony_ip_control/tv.json' };
  const java = {
    'java.nio.file.Files': { exists: () => true, readString: () => registration },
    'java.nio.file.Paths': { get: () => path },
    'java.nio.file.StandardCopyOption': {},
    'java.nio.file.attribute.PosixFilePermissions': {},
    'java.util.UUID': { randomUUID: () => '00000000-0000-4000-8000-000000000001' },
    'org.openhab.core.OpenHAB': { getUserDataFolder: () => 'userdata' },
  };
  const items = Object.fromEntries(['input', 'volume', 'mute', 'key', 'pairing', 'pairing_status', 'pairing_result', 'problem']
    .map(point => [`tv_${point}`, 'NULL']));
  items.tv_power = 'ON';
  const loaded = run(sonyTvSource.replace('// THERMITE_BOUNDED_IO', ''), {
    DEVICES: [{ id: 'tv', address: '192.0.2.40', inputs: [1, 2, 3, 4], volume: { min: 0, max: 100, step: 1 }, speakerAudio: true }],
    PRESSES: { home: 'AAAAAQAAAAEAAABgAw==' } }, { items, clock, java, globals: { thermiteIO } });
  return { ...loaded, clock, tv, items, poll: () => { loaded.rules.sony_ip_control_tv_poll.execute(); clock.now += 15000; } };
}

test('every Sony TV poll posts its input, volume and mute, changed or not; its problem and pairing only on change', () => {
  const r = sonyTv();
  const posts = name => r.posted.filter(([item]) => item === name).map(([, value]) => value);
  r.poll(); r.poll(); r.poll();
  assert.deepEqual(posts('tv_input'), ['hdmi2', 'hdmi2', 'hdmi2']);
  assert.deepEqual(posts('tv_volume'), ['20', '20', '20']);
  assert.deepEqual(posts('tv_mute'), ['OFF', 'OFF', 'OFF']);
  r.tv.input = 3; r.poll();
  assert.deepEqual(posts('tv_input').slice(3), ['hdmi3']);
  // In standby the cleared readings are posted at each poll too.
  r.tv.power = 'standby'; r.items.tv_power = 'OFF'; r.poll(); r.poll();
  assert.deepEqual(posts('tv_input').slice(4), ['UNDEF', 'UNDEF']);
  assert.deepEqual(posts('tv_mute').slice(4), ['UNDEF', 'UNDEF']);
  assert.deepEqual(posts('tv_problem'), ['UNDEF']); assert.deepEqual(posts('tv_pairing_status'), ['paired']);
  assert.deepEqual(r.tv.requests.filter(request => request === 'getPlayingContentInfo').length, 4);
  assert.deepEqual(r.logs.filter(([level]) => level !== 'info'), []);
});

process.stdout.write(JSON.stringify({ passed: tests.length, tests }));
