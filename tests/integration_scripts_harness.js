'use strict';
// Runs production integration scripts against stub openHAB and Java types.
// argv: heos.js globalcache_ir.js bk808_cec.js androidtv_googletv.js
// avpro_telnet.js sony_adcp.js sony_ip_control.js pyatv_http.js, then a JSON
// file {integration name: _device.js as its script embeds it, `vocabulary`:
// the command record's codes, drops and outcomes} (_io.device(), _commands).
// No device or network access: HTTP and sockets are fixtures. A script's
// thermiteIO is a fixture too, but what the scripts share (_device.js) is
// embedded as the renderer embeds it (_io.py).
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const { RECORD, records } = require('./command_record.js');
const sources = process.argv.slice(2).map(path => fs.readFileSync(path, 'utf8'));
const shared = JSON.parse(sources.pop());
const VOCABULARY = shared.vocabulary;
const [heosSource, irSource, cecSource, androidSource, avproSource, adcpSource, sonyTvSource, pyatvSource] =
  ['heos', 'ir', 'bk808_cec', 'androidtv_googletv', 'avpro_telnet', 'sony_adcp', 'sony_ip_control', 'pyatv_http']
    .map((name, at) => sources[at].replace('// THERMITE_DEVICE', () => shared[name]));
// _device.js alone, as a script of an integration named `test` embeds it.
const deviceSource = shared.test;
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

// `strict` throws for a missing Item, as openhab-js does unless asked for
// null; else one is made up.
function openhabStub({ things = {}, items = {}, rules, strict = false }) {
  const posted = [];
  return {
    posted,
    module: {
      items: { getItem: (name, nullable) => {
        if (!(name in items)) {
          if (nullable) return null;
          if (strict) throw Error(`Item ${name} does not exist`);
          items[name] = 'NULL';
        }
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

function run(source, table, { things, items, java = {}, clock, globals = {}, runtime = null, strict = false }) {
  const rules = {}, logs = [];
  const openhab = openhabStub({ things, items, rules, strict });
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
  ran.push({ logs, posted: openhab.posted });
  return { rules, logs, posted: openhab.posted, context, ...records(logs, openhab.posted) };
}

// Every script run here, for the last test: whatever a scenario above did,
// nothing but the record's vocabulary reached a problem Item or a record.
const ran = [];

const VERIFIED = name => '<?xml version="1.0" encoding="utf-8"?>\r\n<tx>\r\n<cmd id="1">' + name + '</cmd>\r\n</tx>\r\n';

// The Java types heos.js loads: HTTP reads answered by replies(body), and
// HEOS CLI sockets that network says how to answer.
function heosJava(requests, replies, network = {}, sockets = []) {
  return {
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
    'java.net.Socket': class { constructor() { sockets.push(this); this.written = ''; }
      connect(address, ms) { this.address = `${address.host}:${address.port}`; this.connectMs = ms; if (network.refuse) throw Error('Connection refused'); }
      setSoTimeout(ms) { this.timeout = ms; }
      getOutputStream() { const socket = this; return { write(bytes) { socket.written += Buffer.from(bytes).toString('ascii'); }, flush() {} }; }
      getInputStream() { const reply = Buffer.from(network.answer || '', 'utf8'); let offset = 0;
        return { read: () => { if (offset < reply.length) return reply[offset++]; if (network.answer === undefined) throw Error('Read timed out'); return -1; } }; }
      close() { this.closed = true; } },
    'java.net.InetSocketAddress': class { constructor(host, port) { this.host = host; this.port = port; } },
    'java.nio.charset.StandardCharsets': { UTF_8: 'utf8', US_ASCII: 'ascii' },
    'java.lang.String': class { constructor(text, charset) { this.text = Buffer.isBuffer(text) ? text.toString(charset) : text; }
      getBytes(charset = 'utf8') { return Buffer.from(this.text, charset); } toString() { return this.text; } },
    'java.io.ByteArrayOutputStream': class { constructor() { this.parts = []; }
      write(buffer, start, count) { this.parts.push(typeof buffer === 'number' ? Buffer.from([buffer]) : Buffer.from(buffer.subarray(start, start + count))); }
      size() { return Buffer.concat(this.parts).length; } toByteArray() { return Buffer.concat(this.parts); } },
    'java.lang.System': { nanoTime: () => Number(process.hrtime.bigint()) },
    'byte[]': function (length) { return Buffer.alloc(length); },
    'java.io.ByteArrayInputStream': class { constructor(bytes) { this.bytes = bytes; } close() {} },
    'javax.xml.parsers.DocumentBuilderFactory': { newInstance: () => ({ setFeature() {}, setAttribute() {}, setXIncludeAware() {},
      setExpandEntityReferences() {}, newDocumentBuilder: () => ({ setErrorHandler() {}, parse: input => parseXml(Buffer.from(input.bytes).toString('utf8')) }) }) },
    'org.xml.sax.helpers.DefaultHandler': class {},
    'javax.xml.XMLConstants': { FEATURE_SECURE_PROCESSING: 'secure', ACCESS_EXTERNAL_DTD: 'dtd', ACCESS_EXTERNAL_SCHEMA: 'schema' },
  };
}

const HEOS_DEVICES = [{ id: 'avr', thing: 'heos:player:1', receiver: 'denon:avr:1', host: '192.0.2.10',
  readURL: 'http://192.0.2.10:8080/goform/AppCommand.xml' }];

function heos(replies) {
  const requests = [];
  const things = { 'heos:player:1': thing('ONLINE'), 'denon:avr:1': thing('ONLINE') };
  const items = { avr_main_power: 'ON', avr_main_input: 'NET', avr_heos_playback: 'NULL', avr_problem: 'NULL', avr_heos_commands: 'NULL' };
  const loaded = run(heosSource, { DEVICES: HEOS_DEVICES }, { things, items, java: heosJava(requests, replies), clock: { now: 0 } });
  loaded.rules.heos_avr_transport.execute({ itemName: 'avr_key', receivedCommand: 'play' });
  return { requests, handled: things['heos:player:1'].handled, logs: loaded.logs, recorded: loaded.recorded() };
}

// A command to the receiver's reboot Item, and the HEOS CLI sockets it opened.
function heosReboot(command, network, send = true) {
  const requests = [], sockets = [];
  const things = { 'heos:player:1': thing('ONLINE'), 'denon:avr:1': thing('ONLINE') };
  const source = send ? heosSource : heosSource.replace('const SEND = true;', 'const SEND = false;');
  const loaded = run(source, { DEVICES: HEOS_DEVICES }, { things, items: { avr_problem: 'NULL', avr_heos_commands: 'NULL' },
    java: heosJava(requests, () => '', network, sockets), clock: { now: 0 } });
  loaded.rules.heos_avr_reboot.execute({ itemName: 'avr_reboot', receivedCommand: command });
  return { sockets, requests, logs: loaded.said().map(([level, message]) => `${level}: ${message}`),
    recorded: loaded.recorded(), problems: loaded.problems('avr_problem'), tally: loaded.tally('avr_heos_commands') };
}

test('a HEOS reboot goes to the receiver\'s own CLI, once, and logs its answer', () => {
  const answered = heosReboot('reboot', { answer: '{"heos": {"command": "system/reboot", "result": "success", "message": ""}}\r\n' });
  assert.deepEqual(answered.sockets.map(socket => [socket.address, socket.written, socket.closed]),
    [['192.0.2.10:1255', 'heos://system/reboot\r\n', true]]);
  assert.deepEqual(answered.requests, []);
  assert.deepEqual(answered.logs, ['warn: avr: sent heos://system/reboot; it answered success']);
  assert.deepEqual(heosReboot('reboot', {}).logs, ['warn: avr: sent heos://system/reboot; no answer: Read timed out']);
  const refused = heosReboot('reboot', { refuse: true });
  assert.deepEqual(refused.logs, ['warn: avr: could not send heos://system/reboot: Connection refused']);
  assert.equal(refused.sockets[0].written, '');
  const dropped = heosReboot('ON', {});
  assert.deepEqual([dropped.sockets, dropped.logs], [[], ["warn: avr_reboot: 'ON' is not reboot, dropped"]]);
  const readOnly = heosReboot('reboot', {}, false);
  assert.deepEqual([readOnly.sockets, readOnly.logs], [[], ['info: avr: SEND is false, not sending heos://system/reboot']]);
});

test('every HEOS reboot ends in one record: what the receiver answered, or why nothing was sent', () => {
  const said = result => `{"heos": {"command": "system/reboot", "result": "${result}", "message": ""}}\r\n`;
  for (const [command, network, send, record, problem, tally] of [
    ['reboot', { answer: said('success') }, true, ['info', 'avr_reboot', 'reboot', 'applied', '-'], [], { applied: 1 }],
    ['reboot', { answer: said('fail') }, true, ['warn', 'avr_reboot', 'reboot', 'refused', 'refused'], ['refused'], { refused: 1 }],
    ['reboot', { answer: 'not HEOS\r\n' }, true, ['warn', 'avr_reboot', 'reboot', 'unknown', 'protocol'], ['protocol'], { unknown: 1 }],
    ['reboot', {}, true, ['warn', 'avr_reboot', 'reboot', 'unknown', 'no_answer'], ['no_answer'], { unknown: 1 }],
    ['reboot', { refuse: true }, true, ['warn', 'avr_reboot', 'reboot', 'unsent', 'unreachable'], ['unreachable'], { unsent: 1 }],
    // What the rule drops itself says nothing of the receiver, and a value
    // the Item does not take is not printed.
    ['ON', {}, true, ['warn', 'avr_reboot', '?', 'dropped', 'invalid'], [], { dropped: 1 }],
    ['reboot', {}, false, ['warn', 'avr_reboot', 'reboot', 'dropped', 'read_only'], [], { dropped: 1 }]]) {
    const r = heosReboot(command, network, send);
    assert.deepEqual(r.recorded, [record], JSON.stringify(record));
    assert.deepEqual(r.problems, ['UNDEF', ...problem]); assert.deepEqual(r.tally, tally);
  }
});

test('HEOS silently ignores non-HEOS keys on the shared receiver key Item', () => {
  const requests = [];
  const things = { 'heos:player:1': thing('ONLINE'), 'denon:avr:1': thing('ONLINE') };
  const items = { avr_main_power: 'ON', avr_main_input: 'NET', avr_heos_playback: 'NULL', avr_problem: 'NULL', avr_heos_commands: 'NULL' };
  const loaded = run(heosSource, { DEVICES: HEOS_DEVICES }, { things, items, java: heosJava(requests, () => ''), clock: { now: 0 } });
  for (const key of ['up', 'down', 'sound_movie', 'menu']) {
    loaded.rules.heos_avr_transport.execute({ itemName: 'avr_key', receivedCommand: key });
  }
  assert.deepEqual(requests, []);
  assert.deepEqual(things['heos:player:1'].handled, []);
  assert.deepEqual(loaded.recorded(), []);
  assert.deepEqual(loaded.said(), []);
  assert.equal(items.avr_problem, 'UNDEF');
  assert.deepEqual(loaded.tally('avr_heos_commands'), {});
});

test('HEOS reads the receiver one verified AppCommand at a time before a transport press', () => {
  // Like the firmware: anything but the verified serialization gets <rx> empty.
  const firmware = (power, source) => body => body === VERIFIED('GetAllZonePowerStatus') ? `<rx><cmd><zone1>${power}</zone1><zone2>ON</zone2></cmd></rx>` :
    body === VERIFIED('GetSourceStatus') ? `<rx><cmd><source>${source}</source></cmd></rx>` : '<rx>\n</rx>';
  const pressed = heos(firmware('ON', 'NET'));
  assert.deepEqual(pressed.requests, [VERIFIED('GetAllZonePowerStatus'), VERIFIED('GetSourceStatus')]);
  assert.deepEqual(pressed.handled, [['heos:player:1:Control', 'PLAY']]);
  // The binding acknowledges nothing: handed over is all the record can say.
  assert.deepEqual(pressed.recorded, [['info', 'avr_key', 'play', 'sent', '-']]);
  for (const [power, source] of [['OFF', 'NET'], ['ON', 'TUNER']]) {
    const refused = heos(firmware(power, source));
    assert.deepEqual(refused.handled, []);
    assert(refused.logs.some(([level, message]) => level === 'warn' && /fresh main-zone ON/.test(message)));
    assert.deepEqual(refused.recorded, [['warn', 'avr_key', 'play', 'unsent', 'not_ready']]);
  }
});

function keys(source, table, uid, tally, clock, status = 'ONLINE') {
  const things = { [uid]: thing(status) };
  const loaded = run(source, table, { things, items: { [`${table.DEVICES[0].id}_problem`]: 'NULL', [tally]: 'NULL' }, clock });
  const rule = Object.values(loaded.rules)[0];
  const item = `${table.DEVICES[0].id}_key`;
  return { ...loaded, send: value => rule.execute({ itemName: item, receivedCommand: value }), thing: things[uid] };
}

test('IR and CEC keys that cannot be sent warn once a minute per device and reason, without the key', () => {
  for (const [source, table, uid, good, tally] of [
    [irSource, { DEVICES: [{ id: 'bluray', thing: 'globalcache:itachIR:1', channel: 'ir-m1#c1', keys: ['PLAY'] }] }, 'globalcache:itachIR:1', 'PLAY',
      'bluray_ir_commands'],
    [cecSource, { DEVICES: [{ id: 'player', thing: 'bk808:cec:1', requests: { PLAY: 'cec play' } }] }, 'bk808:cec:1', 'PLAY',
      'player_bk808_cec_commands']]) {
    const clock = { now: 1000 }, r = keys(source, table, uid, tally, clock);
    r.send('SECRETKEY'); r.send('SECRETKEY');
    r.thing.status = 'OFFLINE'; r.send(good); r.send(good);
    let warnings = r.said().filter(([level]) => level === 'warn');
    assert.equal(warnings.length, 2, source.slice(0, 40));
    assert(/not a/.test(warnings[0][1])); assert(new RegExp(`${uid} is OFFLINE`).test(warnings[1][1]));
    assert(!r.logs.some(([, message]) => message.includes('SECRETKEY')));
    clock.now += 60000; r.send('SECRETKEY');
    warnings = r.said().filter(([level]) => level === 'warn'); assert.equal(warnings.length, 3);
    r.thing.status = 'ONLINE'; r.send(good); assert.equal(r.thing.handled.length, 1);
    // Each key has its record all the same: the reason is said once a
    // minute, that a key did not go out every time.
    const id = table.DEVICES[0].id, item = `${id}_key`;
    assert.deepEqual(r.recorded(), [...Array(2).fill(['warn', item, '?', 'dropped', 'invalid']),
      ...Array(2).fill(['warn', item, good, 'unsent', 'unreachable']), ['warn', item, '?', 'dropped', 'invalid'],
      ['info', item, good, 'sent', '-']]);
    assert.deepEqual(r.problems(`${id}_problem`), ['UNDEF', 'unreachable', 'UNDEF']);
    // A transport that throws leaves the key's effect unknown.
    r.thing.handler.handleCommand = () => { throw Error('binding failed'); };
    r.send(good);
    assert.deepEqual(r.recorded().at(-1), ['warn', item, good, 'unknown', 'other']);
    assert.equal(r.problems(`${id}_problem`).at(-1), 'other');
    assert.deepEqual(r.tally(tally), { sent: 1, dropped: 3, unsent: 2, unknown: 1 });
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
  const source = options.send === false ? androidSource.replace('const SEND = true;', 'const SEND = false;') : androidSource;
  const loaded = run(source, { DEVICES: [{ id: 'tv', thing: 'androidtv:googletv:tv', remote: 'android', volume: options.volume || null,
    keystore: 'androidtv/googletv.tv.keystore' }], PRESSES: { android: { home: ['keypress', 'KEY_HOME'] } } },
  { things: { 'androidtv:googletv:tv': tv }, items: { tv_pairing_status: 'NULL', tv_key: 'NULL', tv_pairing: 'NULL', tv_problem: 'NULL',
    tv_androidtv_googletv_commands: 'NULL', ...options.items }, java, clock });
  const status = () => { const rows = loaded.posted.filter(([name]) => name === 'tv_pairing_status'); return rows.length ? rows[rows.length - 1][1] : null; };
  return { ...loaded, clock, files, moves, tv, status,
    key: command => loaded.rules.androidtv_googletv_tv_command.execute({ itemName: 'tv_key', receivedCommand: command }),
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

test('an Android TV press is sent while its Thing is online, unknown when it is not, and unsent with no handler or volume to step from', () => {
  const scale = { min: 0, max: 100, step: 1, unit: '%' };
  const r = android({ status: 'ONLINE', description: '', volume: scale, items: { tv_android_volume: 'NULL' } });
  r.key('home'); r.volume('30');
  r.tv.status = 'OFFLINE'; r.tv.description = 'Connection refused'; r.key('home');
  r.tv.description = 'PIN Process Incomplete'; r.key('home');
  r.tv.handler = null; r.key('home');
  r.key('nokey'); r.volume('101'); r.pair('not a code');
  assert.deepEqual(r.recorded(), [['info', 'tv_key', 'home', 'sent', '-'], ['warn', 'tv_android_volume', '30', 'unsent', 'not_ready'],
    ['warn', 'tv_key', 'home', 'unknown', 'unreachable'], ['warn', 'tv_key', 'home', 'unknown', 'auth'],
    ['warn', 'tv_key', 'home', 'unsent', 'unreachable'], ['warn', 'tv_key', '?', 'dropped', 'invalid'],
    ['warn', 'tv_android_volume', '?', 'dropped', 'invalid'], ['warn', 'tv_pairing', '<private>', 'dropped', 'invalid']]);
  assert.deepEqual(r.problems('tv_problem'), ['UNDEF', 'not_ready', 'unreachable', 'auth', 'unreachable']);
  // A volume it already has needs no press, and a read-only copy presses nothing.
  const there = android({ status: 'ONLINE', description: '', volume: scale, items: { tv_android_volume: '30' } });
  there.volume('30');
  const copy = android({ status: 'ONLINE', description: '', volume: scale, items: { tv_android_volume: '30' }, send: false });
  copy.key('home'); copy.volume('31');
  assert.deepEqual([there.recorded(), there.tv.handled], [[['info', 'tv_android_volume', '30', 'applied', '-']], []]);
  assert.deepEqual([copy.recorded(), copy.tv.handled], [[['warn', 'tv_key', 'home', 'dropped', 'read_only'],
    ['warn', 'tv_android_volume', '31', 'dropped', 'read_only']], []]);
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
// takes a SET without answering it, and `down` takes no connection. close()
// closes every connection while idle. `format` shapes each answer, and may
// give a SET more than one line. SET RBT is answered RBT, then its listing
// again on the same connection, and no GET or SET is answered for
// restartMs; `rbt` 'refuse' answers it CMD ERR and 'silent' not at all. Its web server records each request (posts) and
// answers webStatus, or, while `webDown`, takes no connection.
function avpro({ send = true } = {}) {
  const clock = { now: 0 }, hooks = [];
  const matrix = { refuse: false, mute: false, hangup: null, silent: false, error: false, down: false, routes: [1, 1, 1, 1], opened: [],
    requests: [], links: [], rbt: 'answer', restartMs: 4000, downUntil: -1, restarts: 0, posts: [], webStatus: 200, webDown: false,
    format: line => line, close() { for (const link of matrix.links) link.peerClosed = true; } };
  const listing = () => [...matrix.routes.map((input, i) => `OUT${i + 1} VS IN${input}`),
    ...matrix.routes.map((_, i) => `OUT${i + 1} EXAUD LEV100`)];
  const thermiteIO = {
    deadline: ms => clock.now + ms,
    remaining: end => { if (end <= clock.now) throw Error('deadline'); return end - clock.now; },
    http(url, method, body, headers) {
      if (matrix.webDown) throw Error('Connection refused');
      matrix.posts.push([url, method, body, headers['Content-Type']]);
      return { getStatus: () => matrix.webStatus };
    },
    open() {
      if (matrix.down) throw Error('refused');
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
      if (request === 'SET RBT') {
        if (matrix.rbt === 'refuse') link.lines.push('CMD ERR');
        else if (matrix.rbt === 'answer') {
          matrix.restarts += 1; matrix.downUntil = clock.now + matrix.restartMs; link.lines.push('RBT', ...listing());
        }
        return;
      }
      if (clock.now < matrix.downUntil) return;
      const set = /^SET OUT(\d+) VS IN(\d+)$/.exec(request);
      if (set && matrix.error) link.lines.push('CMD ERR');
      else if (set && matrix.silent) matrix.routes[set[1] - 1] = Number(set[2]);
      else if (set) { matrix.routes[set[1] - 1] = Number(set[2]); link.lines.push(...[].concat(matrix.format(`SET OUT${set[1]} VS IN${set[2]}`))); }
      else if (request === 'GET OUT0 VS') link.lines.push(...matrix.routes.map((input, i) => matrix.format(`OUT${i + 1} VS IN${input}`)));
    },
    close(link) { link.closed = true; },
  };
  const items = Object.fromEntries([1, 2, 3, 4].flatMap(output => [[`matrix_output${output}_input`, 'NULL'],
    [`matrix_output${output}_hotplug`, 'NULL']]));
  Object.assign(items, { matrix_problem: 'NULL', matrix_avpro_telnet_commands: 'NULL' });
  const source = send ? avproSource : avproSource.replace('const SEND = true;', 'const SEND = false;');
  const loaded = run(source.replace('// THERMITE_BOUNDED_IO', ''), { DEVICES: [{ id: 'matrix', host: '192.0.2.30', port: 23,
    webPort: 80, inputs: 4, outputs: 4 }] }, { items, clock, globals: { thermiteIO }, strict: true,
    runtime: { lifecycleTracker: { addDisposeHook: hook => hooks.push(hook) } } });
  const warnings = () => loaded.said().filter(([level]) => level === 'warn').map(([, message]) => message);
  return { ...loaded, clock, matrix, items, warnings, unload: () => hooks.forEach(hook => hook()),
    poll: () => { loaded.rules.avpro_telnet_matrix_poll.execute(); clock.now += 10000; },
    command: (output, input) => loaded.rules.avpro_telnet_matrix_command.execute({
      itemName: `matrix_output${output}_input`, receivedCommand: String(input) }),
    reboot: (value = 'reboot') => loaded.rules.avpro_telnet_matrix_command.execute({
      itemName: 'matrix_reboot', receivedCommand: value }) };
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
  // Nor is a display known to be on any output: only a diagnostic read says.
  for (let output = 1; output <= 4; output++) assert.deepEqual(posts(`matrix_output${output}_hotplug`), ['UNDEF', 'UNDEF']);
});

test('the AVPro routing provider is unsent before SET, refused on CMD ERR, unknown without its answer, applied once answered', () => {
  const r = avpro(), provider = r.context.routingProvider();
  r.poll();
  assert.deepEqual(provider.execute('matrix_output4_input', '2'),
    { outcome: 'applied', sample: { value: '2', fresh: true, basis: 'owned_native_read', observedAt: 10000 } });
  // CMD ERR is the matrix declining: terminal, never re-read as a slow switch.
  r.matrix.error = true;
  assert.deepEqual(provider.execute('matrix_output4_input', '3'), { outcome: 'refused', code: 'refused' });
  assert.equal(r.items.matrix_output4_input, '2');
  r.matrix.error = false;
  // A SET with no answer, whether the matrix stays connected or hangs up, may
  // still take effect: unknown, even when the read after it succeeds.
  r.matrix.silent = true;
  assert.deepEqual(provider.execute('matrix_output4_input', '3'), { outcome: 'unknown', code: 'no_answer' });
  r.matrix.silent = false; r.matrix.hangup = 'SET OUT4 VS IN4';
  assert.deepEqual(provider.execute('matrix_output4_input', '4'), { outcome: 'unknown', code: 'no_answer' });
  // An answered SET whose read then fails is applied; the executor re-reads.
  r.matrix.hangup = 'GET OUT0 VS';
  assert.deepEqual(provider.execute('matrix_output4_input', '1'), { outcome: 'applied', sample: null });
  assert.equal(r.matrix.routes[3], 1);
  // No connection, so no SET byte: unsent.
  r.matrix.hangup = null; r.matrix.refuse = true; r.matrix.close();
  assert.deepEqual(provider.execute('matrix_output4_input', '2'), { outcome: 'unsent', code: 'no_answer' });
  assert.deepEqual(r.matrix.requests.filter(line => line.startsWith('SET')),
    ['SET OUT4 VS IN2', 'SET OUT4 VS IN3', 'SET OUT4 VS IN3', 'SET OUT4 VS IN4', 'SET OUT4 VS IN1']);
});

test('each way an AVPro switch ends is its record, and what the matrix did not take is its problem until it takes one', () => {
  const r = avpro(), item = 'matrix_output4_input';
  r.poll();
  r.command(4, 2);
  r.matrix.error = true; r.command(4, 3);
  r.matrix.error = false; r.matrix.silent = true; r.command(4, 3);
  r.matrix.silent = false; r.matrix.refuse = true; r.matrix.close(); r.command(4, 1);
  r.matrix.refuse = false; r.matrix.down = true; r.command(4, 1);
  r.command(4, 9); r.command(4, 'IN2');
  r.matrix.down = false; r.command(4, 1);
  assert.deepEqual(r.recorded(), [['info', item, '2', 'applied', '-'], ['warn', item, '3', 'refused', 'refused'],
    ['warn', item, '3', 'unknown', 'no_answer'], ['warn', item, '1', 'unsent', 'no_answer'],
    ['warn', item, '1', 'unsent', 'unreachable'], ['warn', item, '?', 'dropped', 'invalid'], ['warn', item, '?', 'dropped', 'invalid'],
    ['info', item, '1', 'applied', '-']]);
  assert.deepEqual(r.problems('matrix_problem'), ['UNDEF', 'refused', 'no_answer', 'unreachable', 'UNDEF']);
  assert.deepEqual(r.tally('matrix_avpro_telnet_commands'), { applied: 2, dropped: 2, unsent: 2, refused: 1, unknown: 1 });
});

test('only a line naming the input it was sent answers an AVPro SET, not a route from before or another controller\'s', () => {
  const r = avpro(), provider = r.context.routingProvider();
  r.poll();
  // Control4's switch of the same output, which every session hears, comes first.
  r.matrix.format = line => line === 'SET OUT4 VS IN2' ? ['OUT4 VS IN3', line] : line;
  const applied = provider.execute('matrix_output4_input', '2');
  assert.equal(applied.outcome, 'applied'); assert.equal(applied.sample.value, '2');
  // A matrix that names only another input for the output has not said it
  // took this one; its read says what it did.
  r.matrix.format = line => line === 'SET OUT4 VS IN3' ? 'OUT4 VS IN2' : line;
  assert.deepEqual(provider.execute('matrix_output4_input', '3'), { outcome: 'unknown', code: 'no_answer' });
  assert.equal(r.items.matrix_output4_input, '3');
  assert.deepEqual(r.warnings(), ['matrix: did not answer SET OUT4 VS IN3']);
});

test('a read-only copy connects to no AVPro matrix and only logs what it would send', () => {
  const r = avpro({ send: false });
  r.poll(); r.poll(); r.command(4, 2); r.reboot();
  assert.deepEqual(r.matrix.opened, []); assert.deepEqual(r.posted.filter(([item]) => item.includes('_input')), []);
  assert(r.logs.some(([level, message]) => level === 'info' && message === 'matrix: SEND is false, not sending SET OUT4 VS IN2'));
  assert(r.logs.some(([level, message]) => level === 'info' && message === 'matrix: SEND is false, not sending SET RBT'));
  // Its own record says the command went nowhere, and blames no matrix.
  assert.deepEqual(r.recorded(), [['warn', 'matrix_output4_input', '2', 'dropped', 'read_only'],
    ['warn', 'matrix_reboot', 'reboot', 'dropped', 'read_only']]);
  assert.deepEqual(r.problems('matrix_problem'), ['UNDEF']);
});

test('an AVPro reboot is SET RBT, then its network module\'s restart; nothing is sent until it has restarted', () => {
  const r = avpro(), item = 'matrix_reboot';
  r.poll();
  r.reboot();
  assert.deepEqual(r.matrix.requests, ['GET OUT0 VS', 'SET RBT']); assert.equal(r.matrix.restarts, 1);
  assert.deepEqual(r.matrix.posts, [['http://192.0.2.30:80/do', 'POST', '{"cmd":"c s reboot"}', 'application/json;charset=utf-8']]);
  assert(r.matrix.links[0].closed);
  for (let output = 1; output <= 4; output++) {
    assert.equal(r.items[`matrix_output${output}_input`], 'UNDEF'); assert.equal(r.items[`matrix_output${output}_hotplug`], 'UNDEF');
  }
  // For 30 s a switch is not sent, and a poll reads nothing.
  r.command(4, 2); r.poll(); r.poll(); r.poll();
  assert.deepEqual(r.matrix.requests, ['GET OUT0 VS', 'SET RBT']); assert.equal(r.items.matrix_output4_input, 'UNDEF');
  // Then a new connection reads it again.
  r.poll();
  assert.deepEqual(r.matrix.opened, [0, 4]); assert.deepEqual(r.matrix.requests.slice(2), ['GET OUT0 VS']);
  assert.equal(r.items.matrix_output4_input, '1');
  r.command(4, 2);
  assert.equal(r.items.matrix_output4_input, '2');
  assert.deepEqual(r.recorded(), [['info', item, 'reboot', 'applied', '-'], ['warn', 'matrix_output4_input', '2', 'unsent', 'not_ready'],
    ['info', 'matrix_output4_input', '2', 'applied', '-']]);
  assert.deepEqual(r.warnings(), ['matrix: restarting, so SET OUT4 VS IN2 is not sent']);
  assert(r.logs.some(([level, message]) => level === 'info' &&
    message === 'matrix: answered SET RBT and restarts its network module; it is read again in 30 s'));
  // The routing executor queues a reboot in order, as an action, and takes only `reboot`.
  const provider = r.context.routingProvider();
  assert.equal(provider.oneShot(item), true); assert.equal(provider.oneShot('matrix_output4_input'), false);
  assert.equal(provider.has(item, 'reboot'), true); assert.equal(provider.has(item, 'restart'), false);
  r.clock.now += 30000;
  assert.deepEqual(provider.execute(item, 'reboot'), { outcome: 'applied' });
  assert.equal(r.matrix.restarts, 2);
});

test('an AVPro that refuses SET RBT keeps its routes; one that does not answer it is UNDEF and connected again', () => {
  const r = avpro(), item = 'matrix_reboot';
  r.poll();
  r.matrix.rbt = 'refuse'; r.reboot();
  assert.equal(r.items.matrix_output4_input, '1'); assert.deepEqual(r.warnings(), ['matrix: refused SET RBT']);
  r.matrix.rbt = 'silent'; r.reboot();
  assert.equal(r.items.matrix_output4_input, 'UNDEF'); assert(r.matrix.links[0].closed);
  assert.deepEqual(r.warnings().slice(1), ['matrix: did not answer SET RBT']);
  r.poll();
  assert.deepEqual(r.matrix.opened, [0, 1]); assert.equal(r.items.matrix_output4_input, '1');
  r.reboot('restart');
  r.matrix.down = true; r.matrix.close(); r.reboot();
  assert.deepEqual(r.matrix.posts, []);
  // A web server that does not take the network module's restart leaves the
  // controller's under way; a second reboot meanwhile is not sent.
  r.matrix.down = false; r.matrix.rbt = 'answer'; r.matrix.webDown = true; r.reboot(); r.reboot();
  assert.deepEqual(r.warnings().slice(-1), ['matrix: answered SET RBT, but its network module was not restarted: Connection refused']);
  r.matrix.webDown = false; r.matrix.webStatus = 500; r.clock.now += 30000; r.reboot();
  assert.deepEqual(r.warnings().slice(-1), ['matrix: answered SET RBT, but its network module was not restarted: its web server answered HTTP 500']);
  assert.deepEqual(r.recorded(), [['warn', item, 'reboot', 'refused', 'refused'], ['warn', item, 'reboot', 'unknown', 'no_answer'],
    ['warn', item, '?', 'dropped', 'invalid'], ['warn', item, 'reboot', 'unsent', 'unreachable'], ['info', item, 'reboot', 'applied', '-'],
    ['warn', item, 'reboot', 'unsent', 'not_ready'], ['info', item, 'reboot', 'applied', '-']]);
  assert.equal(r.matrix.restarts, 2);
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
  const items = Object.fromEntries(['power', 'power_state', 'input', 'picture_mode', 'key', 'problem', 'sony_adcp_commands']
    .map(point => [`projector_${point}`, 'NULL']));
  const source = send ? adcpSource : adcpSource.replace('const SEND = true;', 'const SEND = false;');
  const loaded = run(source.replace('// THERMITE_BOUNDED_IO', ''), { DEVICES: [{ id: 'projector', host: '192.0.2.31',
    port: 53595, control: true }], KEYS: { menu: 'menu' }, INPUTS: ['hdmi1', 'hdmi2'] }, { items, clock, globals: { thermiteIO }, strict: true,
    runtime: { lifecycleTracker: { addDisposeHook: hook => hooks.push(hook) } } });
  const warnings = () => loaded.said().filter(([level]) => level === 'warn').map(([, message]) => message);
  return { ...loaded, clock, projector, items, warnings, unload: () => hooks.forEach(hook => hook()),
    // Whether any reading was ever posted UNDEF; its problem Item is UNDEF while nothing is wrong.
    unread: () => loaded.posted.some(([item, state]) => state === 'UNDEF' && item !== 'projector_problem'),
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

test('every read posts each projector Item, changed or not, so a reading that holds stays fresh', () => {
  const r = adcp();
  r.poll(); r.poll(); r.poll();
  const posts = name => r.posted.filter(([item]) => item === `projector_${name}`).map(([, state]) => state);
  assert.deepEqual(posts('power'), ['ON', 'ON', 'ON']); assert.deepEqual(posts('power_state'), ['on', 'on', 'on']);
  assert.deepEqual(posts('input'), ['hdmi1', 'hdmi1', 'hdmi1']); assert.deepEqual(posts('picture_mode'), Array(3).fill('reference'));
  // A command's reads post as well.
  r.command('input', 'hdmi1');
  assert.deepEqual(posts('input'), Array(4).fill('hdmi1'));
});

test('a connection the projector closed while idle, or lost to a power cycle, is replaced within the poll', () => {
  const r = adcp();
  r.poll(); r.poll();
  r.projector.close(); r.poll(); r.poll();
  r.projector.cycle(); r.poll(); r.poll();
  assert.deepEqual(r.projector.opened, [0, 2, 4]); assert.deepEqual(r.projector.requests, Array(6).fill(READS).flat());
  assert(!r.unread()); assert.deepEqual(r.warnings(), []);
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
  assert(!r.unread()); assert.deepEqual(r.warnings(), []);
});

test('a projector answering err_cmd twice on power_status fails the read instead of reporting healthy UNDEF, and SESSION_MS is 30000', () => {
  const r = adcp();
  assert.equal(vm.runInContext('SESSION_MS', r.context), 30000);
  r.poll();
  r.projector.errors = 2;
  r.poll();
  assert.equal(r.items.projector_power, 'UNDEF');
  assert.equal(r.items.projector_problem, 'protocol');
  assert(r.warnings().some(msg => msg.includes('answered power_status ? with err_cmd')), JSON.stringify(r.warnings()));
  assert(r.projector.links[0].closed);
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
  assert.deepEqual(provider.execute('projector_input', 'hdmi1'), { outcome: 'refused', code: 'refused' });
  assert.deepEqual(r.projector.opened, [0]);
  // The kept connection lost and no new one: nothing was sent.
  r.projector.refuse = null; r.projector.cycle(); r.projector.down = true;
  assert.deepEqual(provider.execute('projector_power', 'OFF'), { outcome: 'unsent', code: 'unreachable' });
  r.projector.down = false; r.projector.mute = true;
  assert.deepEqual(provider.execute('projector_power', 'OFF'), { outcome: 'unsent', code: 'no_answer' });
  r.projector.mute = false;
  const opened = r.projector.opened.length;
  assert.equal(provider.probe('projector_power').value, 'ON'); assert.equal(r.projector.opened.length, opened + 1);
  r.projector.hangup = 'input "hdmi1"';
  assert.deepEqual(provider.execute('projector_input', 'hdmi1'), { outcome: 'unknown', code: 'no_answer' });
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

test('the routing provider answers busy to a command declined or ignored while the projector moves the other way', () => {
  // The power field's declared settle (sony_adcp.fields()), as the actor's
  // configuration carries it: busy for a tenth of the transition's.
  const r = adcp(), provider = r.context.routingProvider({ projector_power: { settle: { ON: 60000, OFF: 30000 } } });
  r.poll();
  // On while it cools: err_inactive, as the VW915ES and the stand-in answer.
  r.projector.power = 'cooling1'; r.projector.refuse = 'err_inactive';
  assert.deepEqual(provider.execute('projector_power', 'ON'), { outcome: 'busy', code: 'busy', retryAfterMs: 3000 });
  // Off while it starts up, declined or taken and ignored.
  r.projector.power = 'startup';
  assert.deepEqual(provider.execute('projector_power', 'OFF'), { outcome: 'busy', code: 'busy', retryAfterMs: 6000 });
  r.projector.refuse = null; r.projector.after = { on: 'startup', off: 'startup' };
  assert.deepEqual(provider.execute('projector_power', 'OFF'), { outcome: 'busy', code: 'busy', retryAfterMs: 6000 });
  // An input while it starts up waits for it to be on.
  r.projector.refuse = 'err_inactive';
  assert.deepEqual(provider.execute('projector_input', 'hdmi2'), { outcome: 'busy', code: 'busy', retryAfterMs: 6000 });
  // Taken and under way: applied, its transition the readback.
  r.projector.refuse = null; r.projector.power = 'on'; r.projector.after = { on: 'startup', off: 'cooling1' };
  assert.equal(provider.execute('projector_power', 'OFF').outcome, 'applied');
  // A command toward where it is already going, or one declined while it
  // is still, means no.
  r.projector.refuse = 'err_inactive';
  for (const [status, resource, value] of [['cooling1', 'projector_power', 'OFF'], ['standby', 'projector_input', 'hdmi1'],
    ['cooling2', 'projector_input', 'hdmi1'], ['on', 'projector_power', 'OFF'], ['standby', 'projector_power', 'ON']]) {
    r.projector.power = status;
    assert.deepEqual(provider.execute(resource, value), { outcome: 'refused', code: 'refused' }, `${status} ${resource}=${value}`);
  }
  // Without a declared settle it is busy with no wait of its own.
  const bare = adcp(), plain = bare.context.routingProvider();
  bare.projector.power = 'cooling2'; bare.projector.refuse = 'err_inactive';
  assert.deepEqual(plain.execute('projector_power', 'ON'), { outcome: 'busy', code: 'busy' });
});

test('each way a Sony projector command ends is its record: applied, refused, busy while it moves, unsent or unknown', () => {
  const r = adcp();
  r.poll();
  r.command('input', 'hdmi2');
  r.projector.refuse = 'err_inactive'; r.command('input', 'hdmi1');
  r.projector.power = 'cooling1'; r.command('power', 'ON');
  r.projector.power = 'on'; r.projector.refuse = null; r.projector.hangup = 'key "menu"'; r.command('key', 'menu');
  r.projector.hangup = null; r.projector.cycle(); r.projector.down = true; r.command('power', 'OFF');
  r.projector.down = false; r.projector.greeting = '9f86d081'; r.command('power', 'OFF');
  r.command('key', 'nokey'); r.command('input', 'hdmi9');
  r.projector.greeting = 'NOKEY'; r.command('key', 'menu');
  assert.deepEqual(r.recorded(), [['info', 'projector_input', 'hdmi2', 'applied', '-'],
    ['warn', 'projector_input', 'hdmi1', 'refused', 'refused'], ['warn', 'projector_power', 'ON', 'busy', 'busy'],
    ['warn', 'projector_key', 'menu', 'unknown', 'no_answer'], ['warn', 'projector_power', 'OFF', 'unsent', 'unreachable'],
    ['warn', 'projector_power', 'OFF', 'unsent', 'auth'], ['warn', 'projector_key', '?', 'dropped', 'invalid'],
    ['warn', 'projector_input', '?', 'dropped', 'invalid'], ['info', 'projector_key', 'menu', 'applied', '-']]);
  assert.deepEqual(r.problems('projector_problem'), ['UNDEF', 'refused', 'busy', 'no_answer', 'unreachable', 'auth', 'UNDEF']);
});

test('a read-only copy connects to no Sony projector and only logs what it would send', () => {
  const r = adcp({ send: false });
  r.poll(); r.poll(); r.command('power', 'OFF'); r.command('key', 'menu'); r.unload();
  assert.deepEqual(r.projector.opened, []);
  assert.deepEqual(r.posted.filter(([item]) => !item.endsWith('_problem') && !item.endsWith('_commands')), []);
  assert.deepEqual(r.said(), [['info', 'projector: SEND is false, not sending power "off"'],
    ['info', 'projector: SEND is false, not sending key "menu"']]);
  assert.deepEqual(r.recorded(), [['warn', 'projector_power', 'OFF', 'dropped', 'read_only'],
    ['warn', 'projector_key', 'menu', 'dropped', 'read_only']]);
  assert.deepEqual(r.problems('projector_problem'), ['UNDEF']);
});

test('a projector or matrix read while a reload has an Item missing posts the rest and stays connected', () => {
  // The rules can load before the Items. Such a read used to throw, drop
  // the connection, back the device off and warn that it does not answer.
  const projector = adcp(), matrix = avpro();
  delete projector.items.projector_picture_mode; delete matrix.items.matrix_output4_input;
  projector.poll(); projector.poll(); matrix.poll(); matrix.poll();
  assert.deepEqual(projector.warnings(), []); assert.deepEqual(matrix.warnings(), []);
  assert.deepEqual(projector.projector.opened, [0]); assert.deepEqual(matrix.matrix.opened, [0]);
  assert.equal(projector.items.projector_input, 'hdmi1'); assert.equal(matrix.items.matrix_output3_input, '1');
  assert(!('projector_picture_mode' in projector.items)); assert(!('matrix_output4_input' in matrix.items));
});

// A Sony BRAVIA TV on IP control, paired: its registration is in userdata.
// It answers getPowerStatus with `power` (active or standby), and while it is
// active getPlayingContentInfo with HDMI `input` and getVolumeInformation
// with `volume` and `mute`; in standby Sony's error 40005. setPowerStatus
// turns it on or off; while `boot` is above 0 its content API (avContent)
// answers `bootError` instead, one request less each time. Its power Item is
// sony_rest's, so a test sets it. `decline` names a method it answers with a
// Sony error, `status` is the HTTP status of every answer, `fault` answers a
// key with a SOAP fault, and `down` loses every request: `unreached` before
// it connects, `lost` after. `paired: false` leaves it no registration.
function sonyTv({ paired = true, send = true } = {}) {
  const clock = { now: 0 }, day = 24 * 60 * 60 * 1000;
  const tv = { power: 'active', input: 2, volume: 20, mute: false, boot: 0, bootError: [40005, 'Display Is Turned off'], requests: [],
    decline: null, status: 200, fault: false, down: null };
  const answer = body => ({ getStatus: () => tv.status, getContentAsString: () => JSON.stringify(Object.assign({ id: 1 }, body)),
    getHeaders: () => ({ getValuesList: () => ({ size: () => 0, get: () => null }) }) });
  const thermiteIO = {
    unreached: error => error.unreached === true,
    http(url, method, body) {
      if (tv.down) throw Object.assign(Error('no answer'), { unreached: tv.down === 'unreached' });
      if (url.endsWith('/sony/IRCC')) {
        tv.requests.push('IRCC');
        return { getStatus: () => tv.status, getContentAsString: () => tv.fault ? '<s:Envelope><s:Body><s:Fault></s:Fault></s:Body></s:Envelope>' : '',
          getHeaders: () => ({ getValuesList: () => ({ size: () => 0, get: () => null }) }) };
      }
      const request = JSON.parse(body).method;
      tv.requests.push(request);
      if (request === tv.decline) return answer({ error: [3, 'Illegal Argument'] });
      if (request === 'getPowerStatus') return answer({ result: [{ status: tv.power }] });
      if (request === 'setPowerStatus') { tv.power = JSON.parse(body).params[0].status ? 'active' : 'standby'; return answer({ result: [] }); }
      if (tv.power !== 'active') return answer({ error: [40005, 'Display Is Turned off'] });
      if (tv.boot > 0 && url.endsWith('/avContent')) { tv.boot -= 1; return answer({ error: tv.bootError }); }
      if (request === 'setPlayContent') { tv.input = Number(/port=(\d+)/.exec(JSON.parse(body).params[0].uri)[1]); return answer({ result: [] }); }
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
    'java.nio.file.Files': { exists: () => paired, readString: () => registration },
    'java.nio.file.Paths': { get: () => path },
    'java.nio.file.StandardCopyOption': {},
    'java.nio.file.attribute.PosixFilePermissions': {},
    'java.util.UUID': { randomUUID: () => '00000000-0000-4000-8000-000000000001' },
    'org.openhab.core.OpenHAB': { getUserDataFolder: () => 'userdata' },
  };
  const items = Object.fromEntries(['input', 'volume', 'mute', 'key', 'pairing', 'pairing_status', 'pairing_result', 'problem',
    'sony_ip_control_commands'].map(point => [`tv_${point}`, 'NULL']));
  items.tv_power = 'ON';
  const source = send ? sonyTvSource : sonyTvSource.replace('const SEND = true;', 'const SEND = false;');
  const loaded = run(source.replace('// THERMITE_BOUNDED_IO', ''), {
    DEVICES: [{ id: 'tv', address: '192.0.2.40', inputs: [1, 2, 3, 4], volume: { min: 0, max: 100, step: 1 }, speakerAudio: true }],
    PRESSES: { home: 'AAAAAQAAAAEAAABgAw==' } }, { items, clock, java, globals: { thermiteIO } });
  return { ...loaded, clock, tv, items, poll: () => { loaded.rules.sony_ip_control_tv_poll.execute(); clock.now += 15000; },
    command: (point, value) => loaded.rules.sony_ip_control_tv_command.execute({ itemName: `tv_${point}`, receivedCommand: value }) };
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

test('a Sony TV\'s tally says what its reads meet only while it is read: nothing in standby, its problem while one stands', () => {
  const reads = r => JSON.parse(r.items.tv_sony_ip_control_commands).reads;
  const r = sonyTv();
  // Not read yet, then on and answering.
  assert.equal(reads(r), undefined);
  r.poll(); assert.equal(reads(r), 'ok');
  // In standby the rule asks it nothing, so it says nothing: an `ok` kept
  // from the evening before would outlive a TV unplugged since.
  r.tv.power = 'standby'; r.items.tv_power = 'OFF'; r.poll(); r.poll();
  assert.equal(reads(r), undefined); assert.equal(r.items.tv_problem, 'UNDEF');
  r.tv.power = 'active'; r.items.tv_power = 'ON'; r.poll(); assert.equal(reads(r), 'ok');
  // A read it does not answer is its problem, also once its power is no
  // longer known and it is asked nothing; standby, which it reports, ends it.
  r.tv.down = 'unreached'; r.poll(); assert.equal(reads(r), 'unreachable');
  r.items.tv_power = 'UNDEF'; r.poll(); assert.equal(reads(r), 'unreachable');
  assert.equal(r.items.tv_problem, 'unreachable');
  r.tv.down = null; r.tv.power = 'standby'; r.items.tv_power = 'OFF'; r.poll();
  assert.equal(reads(r), undefined); assert.equal(r.items.tv_problem, 'UNDEF');
  // A read the TV declines is an answer; the code is kept as it came.
  r.tv.power = 'active'; r.items.tv_power = 'ON'; r.tv.decline = 'getVolumeInformation'; r.poll();
  assert.equal(reads(r), 'refused'); assert.equal(r.items.tv_problem, 'refused');
  // A TV that is not paired is, on or in standby: the rule knows that itself.
  const unpaired = sonyTv({ paired: false });
  unpaired.poll(); assert.equal(reads(unpaired), 'auth');
  unpaired.tv.power = 'standby'; unpaired.items.tv_power = 'OFF'; unpaired.poll(); unpaired.poll();
  assert.equal(reads(unpaired), 'auth'); assert.equal(unpaired.items.tv_problem, 'auth');
});

test('a volume the Sony TV refuses is explained once and recorded each time, and stays its problem until it takes a command', () => {
  const r = sonyTv();
  r.poll();
  r.tv.decline = 'setAudioVolume';
  r.command('volume', '30'); r.command('volume', '31');
  // What an operator reads: why, once while it stays the same, then one
  // record for each command that was not applied.
  assert.deepEqual(r.logs.filter(([level]) => level === 'warn'), [
    ['warn', 'tv: answered setAudioVolume with a Sony API error'],
    ['warn', 'command device=tv item=tv_volume command=30 outcome=refused code=refused ms=0'],
    ['warn', 'command device=tv item=tv_volume command=31 outcome=refused code=refused ms=0']]);
  assert.equal(r.items.tv_problem, 'refused');
  // A poll that reads the TV says nothing of whether it would now take it.
  r.poll(); assert.equal(r.items.tv_problem, 'refused');
  r.tv.decline = null; r.command('volume', '30');
  assert.deepEqual(r.logs.slice(-2), [['info', 'tv: sent setAudioVolume [{"target":"speaker","volume":"30"}]'],
    ['info', 'command device=tv item=tv_volume command=30 outcome=applied code=- ms=0']]);
  assert.deepEqual(r.problems('tv_problem'), ['UNDEF', 'refused', 'UNDEF']);
  assert.deepEqual(r.tally('tv_sony_ip_control_commands'), { applied: 1, refused: 2 });
});

test('each way a Sony TV command fails ends in its own outcome and code', () => {
  for (const [how, point, value, outcome, code] of [
    [r => { r.tv.power = 'standby'; }, 'volume', '30', 'refused', 'not_ready'],
    [r => { r.tv.down = 'unreached'; }, 'volume', '30', 'unsent', 'unreachable'],
    [r => { r.tv.down = 'lost'; }, 'mute', 'ON', 'unknown', 'no_answer'],
    [r => { r.tv.status = 500; }, 'input', 'hdmi3', 'unknown', 'protocol'],
    [r => { r.tv.status = 404; }, 'input', 'hdmi3', 'refused', 'refused'],
    [r => { r.tv.status = 403; }, 'power', 'OFF', 'refused', 'auth'],
    [r => { r.tv.fault = true; }, 'key', 'home', 'refused', 'refused'],
    [r => { r.tv.status = 503; }, 'key', 'home', 'unknown', 'protocol'],
    [r => { r.tv.down = 'unreached'; }, 'pairing', 'REQUEST', 'unsent', 'unreachable'],
    [r => { r.tv.down = 'lost'; }, 'pairing', 'REQUEST', 'unknown', 'no_answer']]) {
    const r = sonyTv();
    r.poll(); how(r); r.command(point, value);
    const command = point === 'pairing' ? '<private>' : value;
    assert.deepEqual(r.recorded(), [['warn', `tv_${point}`, command, outcome, code]], `${point} ${value}`);
    assert.equal(r.items.tv_problem, code, `${point} ${value}`);
  }
  // Not paired: nothing is sent, and it was the TV's problem before any command.
  const unpaired = sonyTv({ paired: false });
  assert.equal(unpaired.items.tv_problem, 'auth');
  unpaired.command('volume', '30');
  assert.deepEqual([unpaired.recorded(), unpaired.tv.requests], [[['warn', 'tv_volume', '30', 'unsent', 'auth']], []]);
  // What the rule drops itself is no problem of the TV.
  const r = sonyTv(), copy = sonyTv({ send: false });
  r.command('key', 'unknownkey'); r.command('volume', '300'); r.command('input', 'hdmi9'); r.command('pairing', 'not a pin');
  copy.command('volume', '30'); copy.command('key', 'home'); copy.command('pairing', '1234');
  assert.deepEqual(r.recorded(), [['warn', 'tv_key', '?', 'dropped', 'invalid'], ['warn', 'tv_volume', '?', 'dropped', 'invalid'],
    ['warn', 'tv_input', '?', 'dropped', 'invalid'], ['warn', 'tv_pairing', '<private>', 'dropped', 'invalid']]);
  assert.deepEqual(copy.recorded(), [['warn', 'tv_volume', '30', 'dropped', 'read_only'], ['warn', 'tv_key', 'home', 'dropped', 'read_only'],
    ['warn', 'tv_pairing', '<private>', 'dropped', 'read_only']]);
  assert.deepEqual([r.problems('tv_problem'), copy.problems('tv_problem')], [['UNDEF'], ['UNDEF']]);
  // A key and a power command it takes are applied.
  r.command('key', 'home'); r.command('power', 'OFF');
  assert.deepEqual(r.recorded().slice(-2), [['info', 'tv_key', 'home', 'applied', '-'], ['info', 'tv_power', 'OFF', 'applied', '-']]);
});

// An Apple TV behind its helper. The helper answers a read with the Apple
// TV's `state`, and a command with HTTP `status`: that state on 200, else
// what it declined. `down` loses every request: `unreached` before it
// connects, `lost` after.
function appleTv({ send = true } = {}) {
  const clock = { now: 0 };
  const helper = { status: 200, down: null, requests: [],
    state: { id: 'player', paired: true, connected: true, pairing: null, error: null, power: 'on', power_reported_at: 1700000000000 } };
  const thermiteIO = {
    unreached: error => error.unreached === true,
    http(url, method) {
      helper.requests.push(`${method} ${url.replace('http://helper.example', '').replace(/\?.*$/, '')}`);
      if (helper.down) throw Object.assign(Error('no answer'), { unreached: helper.down === 'unreached' });
      const status = method === 'POST' ? helper.status : 200;
      return { getStatus: () => status, getContentAsString: () => JSON.stringify(status === 200 ? helper.state : { error: 'declined' }) };
    },
  };
  const items = Object.fromEntries(['power', 'power_reported_at', 'key', 'pairing', 'pairing_status', 'problem', 'pyatv_http_commands']
    .map(point => [`player_${point}`, 'NULL']));
  const source = send ? pyatvSource : pyatvSource.replace('const SEND = true;', 'const SEND = false;');
  const loaded = run(source.replace('// THERMITE_BOUNDED_IO', ''), { DEVICES: [{ id: 'player', host: '192.0.2.50', helper: 'http://helper.example' }],
    PRESSES: { menu: 'menu' }, APPS: { films: 'com.example.films' } }, { items, clock, strict: true,
    // The rule turns the helper's power timestamp into a date.
    globals: { thermiteIO, Date: class extends Date { static now() { return clock.now; } } } });
  return { ...loaded, clock, helper, items, poll: () => { loaded.rules.pyatv_http_player_poll.execute(); clock.now += 5000; },
    command: (point, value) => loaded.rules.pyatv_http_player_command.execute({ itemName: `player_${point}`, receivedCommand: value }) };
}

test('each way the Apple TV helper answers a command is its record, and the Apple TV\'s own state its problem', () => {
  for (const [how, point, value, outcome, code, problem] of [
    [r => { r.helper.status = 409; }, 'key', 'menu', 'unsent', 'auth', 'auth'],
    [r => { r.helper.status = 422; }, 'pairing', '1234', 'refused', 'auth', 'auth'],
    [r => { r.helper.status = 404; }, 'key', 'films', 'unsent', 'protocol', 'protocol'],
    [r => { r.helper.status = 502; }, 'power', 'OFF', 'unknown', 'no_answer', 'no_answer'],
    [r => { r.helper.status = 500; }, 'power', 'ON', 'unknown', 'other', 'other'],
    [r => { r.helper.down = 'unreached'; }, 'key', 'menu', 'unsent', 'unreachable', 'unreachable'],
    [r => { r.helper.down = 'lost'; }, 'key', 'menu', 'unknown', 'no_answer', 'no_answer']]) {
    const r = appleTv();
    r.poll(); how(r); r.command(point, value);
    assert.deepEqual(r.recorded(), [['warn', `player_${point}`, point === 'pairing' ? '<private>' : value, outcome, code]], `${point} ${value}`);
    assert.equal(r.items.player_problem, problem, `${point} ${value}`);
    // It answers again and takes a key: nothing is wrong any more.
    r.helper.status = 200; r.helper.down = null; r.command('key', 'menu');
    assert.deepEqual(r.recorded()[1], ['info', 'player_key', 'menu', 'applied', '-']);
    assert.equal(r.items.player_problem, 'UNDEF');
  }
  // What each poll reads of the Apple TV is its problem without any command.
  const r = appleTv();
  r.helper.state.paired = false; r.helper.state.connected = false; r.poll();
  assert.equal(r.items.player_problem, 'auth');
  r.helper.state.paired = true; r.helper.state.error = 'cannot connect'; r.poll();
  assert.equal(r.items.player_problem, 'unreachable');
  r.helper.down = 'unreached'; r.poll(); assert.equal(r.items.player_problem, 'unreachable');
  r.helper.down = 'lost'; r.poll(); assert.equal(r.items.player_problem, 'no_answer');
  r.helper.down = null; r.helper.state.connected = true; r.helper.state.error = null; r.poll();
  assert.deepEqual(r.problems('player_problem'), ['UNDEF', 'auth', 'unreachable', 'no_answer', 'UNDEF']);
  // What the rule drops itself reaches no helper.
  const copy = appleTv({ send: false });
  r.helper.requests.length = 0;
  r.command('key', 'nokey'); r.command('power', 'TOGGLE'); r.command('pairing', 'not a code');
  copy.command('key', 'menu'); copy.command('power', 'ON'); copy.command('pairing', '1234');
  assert.deepEqual(r.recorded(), [['warn', 'player_key', '?', 'dropped', 'invalid'], ['warn', 'player_power', '?', 'dropped', 'invalid'],
    ['warn', 'player_pairing', '<private>', 'dropped', 'invalid']]);
  assert.deepEqual(copy.recorded(), [['warn', 'player_key', 'menu', 'dropped', 'read_only'],
    ['warn', 'player_power', 'ON', 'dropped', 'read_only'], ['warn', 'player_pairing', '<private>', 'dropped', 'read_only']]);
  assert.deepEqual([r.helper.requests, copy.helper.requests], [[], []]);
  assert(![...r.logs, ...copy.logs].some(([, message]) => message.includes('1234') || message.includes('not a code')));
});

// _device.js alone, as the renderer embeds it after thermiteIO in a script
// whose SEND it reads.
test('a Sony TV whose content API still boots after power-on reads as on its way, and an input it declines then is busy', () => {
  // Its power field's declared settle (sony_ip_control.fields()) bounds the boot.
  const r = sonyTv(), provider = r.context.routingProvider({ tv_power: { settle: { ON: 30000 } } });
  const sample = (value, extra = {}) => ({ value, fresh: true, basis: 'owned_native_read', observedAt: r.clock.now, ...extra });
  r.tv.power = 'standby';
  assert.deepEqual(provider.probe('tv_power'), sample('OFF'));
  r.tv.boot = 3;
  assert.deepEqual(provider.execute('tv_power', 'ON'), { outcome: 'applied', sample: sample('ON', { transition: true }) });
  r.clock.now += 1000;
  assert.deepEqual(provider.execute('tv_input', 'hdmi3'), { outcome: 'busy', code: 'busy', retryAfterMs: 3000 });
  assert.equal(r.tv.input, 2);
  r.clock.now += 3000; r.tv.boot = 0;
  assert.deepEqual(provider.probe('tv_power'), sample('ON'));
  assert.deepEqual(provider.execute('tv_input', 'hdmi3'), { outcome: 'applied', sample: sample('hdmi3') });
  // Past the boot window, or a TV in standby, a declined input means no.
  r.clock.now += 30000; r.tv.boot = 5;
  assert.deepEqual(provider.execute('tv_input', 'hdmi1'), { outcome: 'refused', code: 'not_ready' });
  assert.deepEqual(provider.probe('tv_power'), sample('ON'));
  r.tv.boot = 0; r.tv.power = 'standby';
  assert.equal(provider.probe('tv_power').value, 'OFF');
  r.tv.power = 'active'; r.tv.boot = 5;
  assert.deepEqual(provider.probe('tv_power'), sample('ON', { transition: true }));
  r.tv.power = 'standby';
  assert.deepEqual(provider.execute('tv_input', 'hdmi1'), { outcome: 'refused', code: 'not_ready' });
  // An answer naming no HDMI input (Sony's error 7) is a booted TV showing
  // something else; without a declared settle nothing is a boot.
  r.tv.power = 'active'; r.tv.boot = 5; r.tv.bootError = [7, 'Illegal State'];
  assert.deepEqual(provider.probe('tv_power'), sample('ON'));
  const bare = sonyTv(), plain = bare.context.routingProvider();
  bare.tv.power = 'standby'; plain.probe('tv_power'); bare.tv.boot = 5;
  assert.deepEqual(plain.execute('tv_power', 'ON').sample.transition, undefined);
  assert.deepEqual(plain.execute('tv_input', 'hdmi1'), { outcome: 'refused', code: 'not_ready' });
});

function device({ send = true, items = {}, thermiteIO = {} } = {}) {
  const rules = {}, logs = [], hooks = [], clock = { now: 5000 };
  const openhab = openhabStub({ items, rules });
  const context = vm.createContext({
    require: name => name === '@runtime' ? { lifecycleTracker: { addDisposeHook: hook => hooks.push(hook) } }
      : (assert.equal(name, 'openhab'), openhab.module),
    console: { info: m => logs.push(['info', m]), warn: m => logs.push(['warn', m]) },
    Date: { now: () => clock.now }, thermiteIO, Map, Set, Object, String, Math,
  });
  const helper = vm.runInContext(`const SEND = ${send};\n${deviceSource}\nthermiteDevice;`, context);
  ran.push({ logs, posted: openhab.posted });
  return { helper, rules, logs, hooks, clock, items, posted: openhab.posted, ...records(logs, openhab.posted) };
}

// The problem and tally Items of the device `tv` in a script of the
// integration `test`, not yet posted to.
const recordItems = () => ({ tv_problem: 'NULL', tv_test_commands: 'NULL' });

test('the shared helper posts a reading at every read, the rule\'s own state only on change, and no missing Item', () => {
  const r = device({ items: { tv_power: 'ON', tv_pairing_status: 'paired' } });
  r.helper.reading('tv_power', 'ON'); r.helper.reading('tv_power', 'ON');
  r.helper.status('tv_pairing_status', 'paired'); r.helper.status('tv_pairing_status', 'failed');
  r.helper.status('tv_pairing_status', 'failed');
  // A reload can load the rules before the Items: nothing to post to yet.
  r.helper.reading('tv_input', 'hdmi1'); r.helper.status('tv_problem', 'UNDEF');
  assert.deepEqual(r.posted, [['tv_power', 'ON'], ['tv_power', 'ON'], ['tv_pairing_status', 'failed']]);
});

test('the shared helper logs a device going down and coming back once each, and backs a failing one off', () => {
  const r = device(), health = r.helper.health(4);
  const tried = [];
  for (let poll = 0; poll < 12; poll++) {
    if (health.due('tv')) { tried.push(poll); health.failed('tv', 'does not answer'); }
  }
  // The next poll, then after 2 and 4, then every 4.
  assert.deepEqual(tried, [0, 1, 3, 7, 11]);
  health.answered('tv', 'answers again'); health.answered('tv', 'answers again');
  assert.deepEqual(r.logs, [['warn', 'tv: does not answer'], ['info', 'tv: answers again']]);
  assert(health.due('tv') && health.due('tv'));
  // Without retryPolls a failing device is never left alone.
  const steady = r.helper.health();
  steady.failed('tv', 'down'); steady.failed('tv', 'down');
  assert(steady.due('tv') && steady.due('tv'));
});

test('with SEND false the shared helper holds a command back and says so', () => {
  const live = device(), copy = device({ send: false });
  assert.equal(live.helper.withheld({ id: 'tv' }, 'sending power ON'), false);
  assert.equal(copy.helper.withheld({ id: 'tv' }, 'sending power ON'), true);
  assert.deepEqual(live.logs, []); assert.deepEqual(copy.logs, [['info', 'tv: SEND is false, not sending power ON']]);
});

test('the shared command rule hands a command to the routing actor once there is one, else sends and records it', () => {
  const r = device({ items: recordItems() }), direct = [], legacy = [];
  let actor = null;
  r.helper.commandRule({ id: 'x_tv_command', name: 'tv', device: 'tv', items: ['tv_power', 'tv_pairing'], actor: () => actor,
    direct: event => { direct.push(event.itemName); return { outcome: 'applied' }; } });
  const rule = r.rules.x_tv_command;
  assert.deepEqual(rule.triggers, [{ name: 'tv_power' }, { name: 'tv_pairing' }]);
  rule.execute({ itemName: 'tv_power', receivedCommand: 'ON' });
  assert.deepEqual(r.recorded(), [['info', 'tv_power', 'ON', 'applied', '-']]);
  actor = { legacy: (...args) => legacy.push(args) };
  rule.execute({ itemName: 'tv_power', receivedCommand: { toString: () => 'OFF' }, eventSource: 'remote' });
  rule.execute({ itemName: 'tv_pairing', receivedCommand: '1234' });
  assert.deepEqual(direct, ['tv_power']);
  assert.deepEqual(legacy, [['tv_power', 'OFF', false, 'remote'], ['tv_pairing', '1234', true, undefined]]);
  // A command handed on is the actor's to record when it concludes it
  // (lifecycle_io_harness.js): the rule adds no record of its own.
  assert.equal(r.recorded().length, 1); assert.deepEqual(r.tally('tv_test_commands'), { applied: 1 });
});

test('a command its sender could not apply leaves a WARN record, its code as the problem and a count; the next one taken ends the problem', () => {
  const r = device({ items: recordItems() });
  let answer = { outcome: 'refused', code: 'refused' };
  r.helper.commandRule({ id: 'x_tv_command', name: 'tv', device: 'tv', items: ['tv_volume'],
    direct: () => { r.clock.now += 120; return answer; } });
  // From the start: a count of zero for every outcome, and no problem.
  const zeros = Object.fromEntries(VOCABULARY.outcomes.map(outcome => [outcome, { count: 0, ms: 0 }]));
  assert.deepEqual(r.posted, [['tv_test_commands', JSON.stringify({ version: 1, outcomes: zeros })], ['tv_problem', 'UNDEF']]);
  const send = value => r.rules.x_tv_command.execute({ itemName: 'tv_volume', receivedCommand: value });
  send('30');
  assert.deepEqual(r.logs, [['warn', 'command device=tv item=tv_volume command=30 outcome=refused code=refused ms=120']]);
  assert.equal(r.items.tv_problem, 'refused');
  assert.deepEqual(JSON.parse(r.items.tv_test_commands), { version: 1, outcomes: { ...zeros, refused: { count: 1, ms: 120 } } });
  // Another that fails the same way counts again and posts no problem twice.
  send('30');
  assert.deepEqual(r.problems('tv_problem'), ['UNDEF', 'refused']);
  answer = { outcome: 'applied' }; send('31');
  assert.deepEqual(r.logs[2], ['info', 'command device=tv item=tv_volume command=31 outcome=applied code=- ms=120']);
  assert.equal(r.items.tv_problem, 'UNDEF');
  assert.deepEqual(JSON.parse(r.items.tv_test_commands).outcomes, { ...zeros, refused: { count: 2, ms: 240 }, applied: { count: 1, ms: 120 } });
});

test('a sender that throws, or does not say how its command ended, is recorded unknown, and what the rule dropped blames no device', () => {
  const r = device({ items: recordItems() });
  let direct = () => { throw Error('the sender failed'); };
  r.helper.commandRule({ id: 'x_tv_command', name: 'tv', device: 'tv', items: ['tv_volume'], direct: event => direct(event) });
  const send = () => r.rules.x_tv_command.execute({ itemName: 'tv_volume', receivedCommand: '30' });
  // The record is made, and openHAB still logs the rule's failure.
  assert.throws(send, /the sender failed/);
  for (const said of [undefined, null, 'applied', true, {}, { outcome: 'done' }]) { direct = () => said; send(); }
  assert.deepEqual(r.recorded(), Array(7).fill(['warn', 'tv_volume', '30', 'unknown', 'other']));
  assert.deepEqual(r.problems('tv_problem'), ['UNDEF', 'other']);
  for (const code of VOCABULARY.drops) { direct = () => ({ outcome: 'dropped', code }); send(); }
  assert.deepEqual(r.recorded().slice(7), [['warn', 'tv_volume', '?', 'dropped', 'invalid'],
    ['warn', 'tv_volume', '30', 'dropped', 'read_only'], ['warn', 'tv_volume', '30', 'dropped', 'held']]);
  assert.equal(r.items.tv_problem, 'other');
  assert.deepEqual(r.tally('tv_test_commands'), { unknown: 7, dropped: 3 });
});

test('the tally says what the reads meet apart from a command\'s problem, on change, and nothing in a script that reads nothing', () => {
  // The latest `reads` the tally was posted with; undefined while it has none.
  const reads = r => JSON.parse(r.items.tv_test_commands).reads;
  const tallied = r => r.posted.filter(([item]) => item === 'tv_test_commands').length;
  const sent = device({ items: recordItems() }), record = sent.helper.recorder('tv');
  // A script that only sends: a command's problem stays in the Item, and
  // the tally never claims a reading of the device.
  record({ item: 'tv_key', command: 'play', outcome: 'unsent', code: 'unreachable', ms: 1 });
  assert.equal(sent.items.tv_problem, 'unreachable'); assert.equal(reads(sent), undefined);
  assert(!sent.items.tv_test_commands.includes('reads'));

  const r = device({ items: recordItems() }), health = r.helper.health();
  const refuse = r.helper.recorder('tv');
  assert.equal(reads(r), undefined);
  health.answered('tv', 'answers again'); assert.equal(reads(r), 'ok');
  // A command the device refused is its problem until it takes one, while
  // its reads go on saying that it answers.
  refuse({ item: 'tv_volume', command: '300', outcome: 'refused', code: 'refused', ms: 1 });
  const posts = tallied(r);
  for (let poll = 0; poll < 5; poll++) health.answered('tv', 'answers again');
  assert.equal(r.items.tv_problem, 'refused'); assert.equal(reads(r), 'ok');
  // Posted when it changes, not at every read.
  assert.equal(tallied(r), posts);
  health.failed('tv', 'does not answer', 'no_answer'); health.failed('tv', 'does not answer', 'no_answer');
  assert.equal(reads(r), 'no_answer'); assert.equal(tallied(r), posts + 1);
  // A device that answers and is not paired cannot be read either.
  health.answered('tv', 'answers again', 'auth'); assert.equal(reads(r), 'auth');
  health.answered('tv', 'answers again'); assert.equal(reads(r), 'ok');
  assert.equal(r.items.tv_problem, 'refused');
  // A script that asks the device nothing now takes back its `ok`, once: no
  // reading outlives its read. A problem its reads met stands until a read
  // ends it.
  r.helper.problem.unread('tv'); r.helper.problem.unread('tv');
  assert.equal(reads(r), undefined); assert.equal(tallied(r), posts + 4);
  assert.equal(r.items.tv_problem, 'refused');
  health.failed('tv', 'does not answer', 'unreachable'); r.helper.problem.unread('tv');
  assert.equal(reads(r), 'unreachable'); assert.equal(r.items.tv_problem, 'unreachable');
  health.answered('tv', 'answers again'); assert.equal(reads(r), 'ok');
  // A read concluded before the script records for the device is not lost.
  const late = device({ items: recordItems() });
  late.helper.problem.read('tv', 'unreachable'); late.helper.recorder('tv');
  assert.equal(reads(late), 'unreachable');
});

test('only a code of the table reaches a problem Item or a record, and no PIN or free text a record', () => {
  const r = device({ items: recordItems() }), record = r.helper.recorder('tv'), health = r.helper.health();
  assert.equal(r.helper.recorder('tv'), record);
  for (const code of ['The TV said: Illegal State', 'native_avpro_status_unavailable', 'invalid', 'UNDEF', '', 7, {}]) {
    record({ item: 'tv_volume', command: '30', outcome: 'refused', code, ms: 1 });
    assert.equal(r.items.tv_problem, 'other', String(code));
    record({ item: 'tv_volume', command: '30', outcome: 'applied', code, ms: 1 });
    assert.equal(r.items.tv_problem, 'UNDEF');
    r.helper.problem.read('tv', code);
    assert.equal(r.items.tv_problem, 'other', String(code));
    r.helper.problem.read('tv', null);
  }
  health.failed('tv', 'does not answer'); assert.equal(r.items.tv_problem, 'other');
  health.answered('tv', 'answers again', 'not a code'); assert.equal(r.items.tv_problem, 'other');
  health.answered('tv', 'answers again'); assert.equal(r.items.tv_problem, 'UNDEF');
  // A record never prints a pairing PIN, a value with anything but plain
  // characters, a duration that is no number or an Item name that is none.
  record({ item: 'tv_pairing', command: '1234', secret: true, outcome: 'applied', ms: 2 });
  record({ item: 'tv_app', command: 'a title: with words', outcome: 'sent', ms: -5 });
  record({ item: 'tv key', command: 'x'.repeat(65), outcome: 'dropped', code: 'not a drop', ms: 'soon' });
  assert.deepEqual(r.logs.slice(-3), [['info', 'command device=tv item=tv_pairing command=<private> outcome=applied code=- ms=2'],
    ['info', 'command device=tv item=tv_app command=? outcome=sent code=- ms=0'],
    ['warn', 'command device=tv item=? command=? outcome=dropped code=- ms=0']]);
});

test('a read that fails shows before a failed command, and one that recovers ends only the same problem of a command', () => {
  const r = device({ items: recordItems() }), record = r.helper.recorder('tv'), health = r.helper.health();
  const command = (outcome, code) => record({ item: 'tv_volume', command: '30', outcome, code, ms: 1 });
  command('refused', 'refused');
  health.failed('tv', 'does not answer', 'no_answer'); assert.equal(r.items.tv_problem, 'no_answer');
  // That it answers a read again does not say it would now take the command.
  health.answered('tv', 'answers again'); assert.equal(r.items.tv_problem, 'refused');
  command('unsent', 'unreachable'); health.failed('tv', 'cannot connect', 'unreachable');
  health.answered('tv', 'answers again'); assert.equal(r.items.tv_problem, 'UNDEF');
  // What a device answers can itself be the problem: not paired, say.
  health.answered('tv', 'answers again', 'auth'); assert.equal(r.items.tv_problem, 'auth');
  command('applied'); assert.equal(r.items.tv_problem, 'auth');
  health.answered('tv', 'answers again'); assert.equal(r.items.tv_problem, 'UNDEF');
  assert.deepEqual(r.problems('tv_problem'), ['UNDEF', 'refused', 'no_answer', 'refused', 'unreachable', 'UNDEF', 'auth', 'UNDEF']);
});

test('the shared connections keep one per device, replace one closed while idle and close all on unload', () => {
  const opened = [];
  const thermiteIO = {
    renew(link, end) { link.end = end; },
    // Each kept link has one unasked line waiting, then is quiet.
    line(link) { if (link.unasked.length) return link.unasked.shift(); if (link.peerClosed) { link.closed = true; return null; } throw Error('quiet'); },
    quietTimeout: () => true,
    close(link) { link.closed = true; },
  };
  const r = device({ thermiteIO });
  const links = r.helper.links((dev, end) => { const link = { id: dev.id, end, closed: false, unasked: [] }; opened.push(link); return link; });
  const tv = { id: 'tv' };
  const first = links.connection(tv, 1);
  first.unasked.push('late answer');
  assert.equal(links.connection(tv, 2), first); assert.deepEqual(first.unasked, []); assert.equal(first.end, 2);
  first.peerClosed = true;
  const second = links.connection(tv, 3);
  assert.notEqual(second, first); assert.equal(opened.length, 2);
  links.drop(tv); assert(second.closed);
  const third = links.connection(tv, 4);
  r.hooks.forEach(hook => hook());
  assert(third.closed); assert.equal(opened.length, 3);
});

test('the shared samples are fresh owned reads, and a command applied without one leaves the actor to read again', () => {
  const r = device();
  assert.deepEqual(r.helper.sample('ON'), { value: 'ON', fresh: true, basis: 'owned_native_read', observedAt: 5000 });
  assert.deepEqual(r.helper.applied(() => r.helper.sample('hdmi1')).sample.value, 'hdmi1');
  assert.deepEqual(r.helper.applied(() => { throw Error('unread'); }), { outcome: 'applied', sample: null });
});

test('in every scenario above, a problem Item held only a code of the table or UNDEF, and every record was of the table', () => {
  const states = [VOCABULARY.clear, ...VOCABULARY.problems];
  let problems = 0, recorded = 0;
  for (const { logs, posted } of ran) {
    for (const [item, state] of posted) if (item.endsWith('_problem')) { problems += 1; assert(states.includes(state), `${item}=${state}`); }
    for (const [level, message] of logs) if (message.startsWith('command ')) {
      const match = RECORD.exec(message);
      assert(match, message);
      const outcome = match[4], code = match[5], taken = outcome === 'applied' || outcome === 'sent';
      assert(VOCABULARY.outcomes.includes(outcome), message); assert.equal(level, taken ? 'info' : 'warn', message);
      assert(taken ? code === '-' : outcome === 'dropped' ? code === '-' || VOCABULARY.drops.includes(code)
        : VOCABULARY.problems.includes(code), message);
      recorded += 1;
    }
  }
  // Not vacuous: the scenarios above post problems and record commands.
  assert(problems >= 60 && recorded >= 60, `${problems} problem posts, ${recorded} records`);
});

process.stdout.write(JSON.stringify({ passed: tests.length, tests }));
