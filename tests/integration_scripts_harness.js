'use strict';
// Runs production integration scripts against stub openHAB and Java types.
// argv: heos.js globalcache_ir.js bk808_cec.js androidtv_googletv.js
// avpro_telnet.js. No device or network access: HTTP and sockets are
// fixtures.
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const [heosSource, irSource, cecSource, androidSource, avproSource] =
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

function run(source, table, { things, items, java = {}, clock, globals = {} }) {
  const rules = {}, logs = [];
  const openhab = openhabStub({ things, items, rules });
  const types = Object.assign({
    'org.openhab.core.thing.ChannelUID': class { constructor(uid) { this.uid = uid; } },
    'org.openhab.core.library.types.StringType': class { constructor(value) { this.value = value; } },
  }, java);
  const context = vm.createContext({
    require: name => { assert.equal(name, 'openhab'); return openhab.module; },
    Java: { type: name => types[name] || assert.fail('unexpected Java type ' + name) },
    console: { info: m => logs.push(['info', m]), warn: m => logs.push(['warn', m]), error: m => logs.push(['error', m]) },
    Date: { now: () => clock.now }, JSON, Map, Set, Object, String, Number, Boolean, Array, Error, Math, RegExp,
    ...globals,
  });
  let text = source;
  for (const [mark, value] of Object.entries(table)) text = text.replace(`const ${mark} = null;`, `const ${mark} = ${JSON.stringify(value)};`);
  vm.runInContext(text, context);
  return { rules, logs, posted: openhab.posted };
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

// An AC-MX-44X on telnet: it lists nothing on connect, answers GET OUT0 VS
// with every route and SET with the new one, or refuses every connection.
function avpro() {
  const clock = { now: 0 }, matrix = { refuse: false, routes: [1, 1, 1, 1], opened: [] };
  const thermiteIO = {
    deadline: ms => clock.now + ms,
    remaining: end => { if (end <= clock.now) throw Error('deadline'); return end - clock.now; },
    open() { matrix.opened.push(clock.now / 60000); if (matrix.refuse) throw Error('refused'); return { closed: false, lines: [] }; },
    line(link) { if (link.lines.length) return link.lines.shift(); throw Error('quiet'); },
    quietTimeout: () => true,
    write(link, text) {
      const set = /^SET OUT(\d+) VS IN(\d+)\r\n$/.exec(text);
      if (set) { matrix.routes[set[1] - 1] = Number(set[2]); link.lines.push(`OUT${set[1]} VS IN${set[2]}`); }
      else if (text === 'GET OUT0 VS\r\n') link.lines.push(...matrix.routes.map((input, i) => `OUT${i + 1} VS IN${input}`));
    },
    close(link) { link.closed = true; },
  };
  const items = {};
  const loaded = run(avproSource.replace('// THERMITE_BOUNDED_IO', ''), { DEVICES: [{ id: 'matrix', host: '192.0.2.30', port: 23,
    inputs: 4, outputs: 4 }] }, { items, clock, globals: { thermiteIO } });
  return { ...loaded, clock, matrix, items,
    poll: () => { loaded.rules.avpro_telnet_matrix_poll.execute(); clock.now += 60000; },
    command: (output, input) => loaded.rules.avpro_telnet_matrix_command.execute({
      itemName: `matrix_output${output}_input`, receivedCommand: String(input) }) };
}

test('an AVPro matrix that refuses is polled after 1, 2, 4 and then every 5 minutes; a switch still tries at once', () => {
  const r = avpro();
  r.matrix.refuse = true;
  for (let minute = 0; minute < 18; minute++) r.poll();
  assert.deepEqual(r.matrix.opened, [0, 1, 3, 7, 12, 17]);
  assert.equal(r.items.matrix_output4_input, 'UNDEF');
  assert.equal(r.logs.filter(([level]) => level === 'warn').length, 1);
  r.matrix.refuse = false; r.command(4, 2);
  assert.deepEqual(r.matrix.opened.slice(6), [18]); assert.equal(r.items.matrix_output4_input, '2');
  assert(r.logs.some(([level, message]) => level === 'info' && message.includes('answering over telnet again')));
  r.poll(); r.poll(); r.matrix.refuse = true; r.poll(); r.poll();
  assert.deepEqual(r.matrix.opened.slice(7), [18, 19, 20, 21]);
});

process.stdout.write(JSON.stringify({ passed: tests.length, tests }));
