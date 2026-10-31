// Apple TVs through the Apple TV helper (lib/oh_integrations/pyatv_http,
// lib/oh_atv.py).
//
// DEVICES lists each Apple TV: its id (its Items are <id>_power, <id>_key,
// <id>_pairing and <id>_pairing_status), its address and the URL of the
// helper that drives it. The helper keeps one connection per Apple TV and
// answers every request with the Apple TV's state: whether it is paired and
// connected, its power, and whether it is showing a pairing code.
//
// - Every POLL the rule asks the helper for each Apple TV's state and posts
//   it; one the helper does not answer for gets UNDEF and "unavailable". A
//   paired Apple TV the helper is still connecting to is "checking", and one
//   it cannot reach "unreachable".
// - A button id sent to <id>_key is pressed as PRESSES gives it, or opens the
//   app APPS gives it.
// - ON or OFF sent to <id>_power wakes the Apple TV or puts it to sleep.
// - REQUEST sent to <id>_pairing makes the Apple TV show four digits; the
//   digits sent to it then pair the helper with it. A code it refuses shows
//   "failed" for FAILED_MS, unless a new code is asked for sooner.
//
// Each command is answered with the new state, which is posted at once.
// Anything else is dropped. With SEND false (oh_render.read_only) the rule
// still reads, and logs what it would send. openHAB runs one rule of a script
// file at a time, so a slow request holds the next one back.
const DEVICES = null;
const PRESSES = null;
const APPS = null;
const SEND = true;

const openhab = require('openhab');

const HttpClient = Java.type('java.net.http.HttpClient');
const HttpRequest = Java.type('java.net.http.HttpRequest');
const BodyPublishers = Java.type('java.net.http.HttpRequest$BodyPublishers');
const BodyHandlers = Java.type('java.net.http.HttpResponse$BodyHandlers');
const URI = Java.type('java.net.URI');
const Duration = Java.type('java.time.Duration');

const POLL = '0/5 * * * * ?';
const CONNECT_MS = 1000;
// The helper answers a read at once, from what the Apple TV last pushed.
const READ_MS = 3000;
// A command may wait for the helper to connect to the Apple TV first (up to
// its own 10 s), and a code for the Apple TV to check it.
const SEND_MS = 15000;
// Asking for a code takes the helper up to three such steps: find the Apple
// TV, open a pairing and start it.
const PAIR_MS = 35000;
const FAILED_MS = 60000;
const REQUEST = 'REQUEST';
const CODE = /^[0-9]{4}$/;
const POWER = { on: 'ON', off: 'OFF' };

const client = HttpClient.newBuilder().connectTimeout(Duration.ofMillis(CONNECT_MS)).build();
// device id -> the time until which its pairing shows failed.
const refused = {};
// Ids of the Apple TVs whose helper did not answer the last request: logged once.
const down = new Set();

function has(object, key) {
  return Object.prototype.hasOwnProperty.call(object, key);
}

function update(name, state) {
  // Null while a reload has the rules in before the Items: the next poll posts it.
  const item = openhab.items.getItem(name, true);
  if (item && String(item.state) !== state) item.postUpdate(state);
}

// {code, data} of the helper's answer (data null unless JSON), or null if it
// did not answer.
function call(device, path, body, ms) {
  const builder = HttpRequest.newBuilder(URI.create(device.helper + path))
    .timeout(Duration.ofMillis(ms))
    .header('Accept', 'application/json');
  if (body === null) {
    builder.GET();
  } else {
    builder.header('Content-Type', 'application/json')
      .POST(BodyPublishers.ofString(JSON.stringify(Object.assign({ host: device.host }, body))));
  }
  let response;
  try {
    response = client.send(builder.build(), BodyHandlers.ofString());
  } catch (e) {
    return null;
  }
  let data = null;
  try {
    data = JSON.parse(String(response.body()));
  } catch (e) {
    data = null;
  }
  return { code: Number(response.statusCode()), data };
}

function pairingState(device, data) {
  if (data.paired && data.connected) return 'paired';
  if (data.pairing === 'code_shown') return 'code_shown';
  if (has(refused, device.id) && Date.now() < refused[device.id]) return 'failed';
  if (!data.paired) return 'unpaired';
  return data.error ? 'unreachable' : 'checking';
}

// Posts the state an answer carries; an answer without one means the helper
// is not there.
function show(device, answer) {
  const data = answer ? answer.data : null;
  if (!data || typeof data !== 'object' || data.id !== device.id) {
    if (!down.has(device.id)) {
      down.add(device.id);
      console.warn(`${device.id}: the Apple TV helper at ${device.helper} does not answer; `
        + 'its Items are UNDEF until it does');
    }
    update(`${device.id}_power`, 'UNDEF');
    update(`${device.id}_pairing_status`, 'unavailable');
    return;
  }
  if (down.delete(device.id)) console.info(`${device.id}: the Apple TV helper answers again`);
  update(`${device.id}_power`, data.connected && has(POWER, data.power) ? POWER[data.power] : 'UNDEF');
  update(`${device.id}_pairing_status`, pairingState(device, data));
}

function read(device) {
  show(device, call(device, `/devices/${device.id}?host=${encodeURIComponent(device.host)}`, null, READ_MS));
}

// Sends one command and logs how it went; `what` names it in the log (never
// the code itself). Returns the answer, or null if none came.
function request(device, path, body, what, ms) {
  const answer = call(device, `/devices/${device.id}${path}`, body, ms || SEND_MS);
  if (answer === null) {
    console.warn(`${device.id}: the Apple TV helper at ${device.helper} does not answer; ${what} not sent`);
  } else if (answer.code !== 200) {
    const why = answer.data && answer.data.error ? answer.data.error : `HTTP ${answer.code}`;
    console.warn(`${device.id}: ${what}: ${why}`);
  } else {
    console.info(`${device.id}: sent ${what}`);
  }
  return answer;
}

// Posts the state an answer to a command carries; one without it (a request
// the helper refused before reaching the Apple TV) is followed by a read.
function settle(device, answer) {
  if (answer !== null && !(answer.data && answer.data.id === device.id)) read(device);
  else show(device, answer);
}

function send(device, path, body, what, ms) {
  if (!SEND) {
    console.info(`${device.id}: SEND is false, not sending ${what}`);
    return;
  }
  settle(device, request(device, path, body, what, ms));
}

function key(device, button) {
  if (has(PRESSES, button)) send(device, '/key', { key: PRESSES[button] }, `key ${PRESSES[button]}`);
  else if (has(APPS, button)) send(device, '/app', { app: APPS[button] }, `app ${APPS[button]}`);
  else console.warn(`${device.id}_key: no key for '${button}', dropped`);
}

function power(device, command) {
  if (command !== 'ON' && command !== 'OFF') {
    console.warn(`${device.id}_power: '${command}' is not ON or OFF, dropped`);
    return;
  }
  send(device, '/power', { on: command === 'ON' }, `power ${command}`);
}

function pair(device, command) {
  if (command === REQUEST) {
    delete refused[device.id];
    send(device, '/pair', {}, 'a request for a pairing code', PAIR_MS);
  } else if (CODE.test(command)) {
    if (!SEND) {
      console.info(`${device.id}: SEND is false, not sending the pairing code`);
      return;
    }
    update(`${device.id}_pairing_status`, 'checking');
    const answer = request(device, '/pair', { code: command }, 'the pairing code', PAIR_MS);
    if (answer !== null && answer.code !== 200) refused[device.id] = Date.now() + FAILED_MS;
    settle(device, answer);
  } else {
    console.warn(`${device.id}_pairing: a command that is neither ${REQUEST} nor four digits, dropped`);
  }
}

openhab.rules.JSRule({
  id: 'pyatv_http_poll',
  name: 'Apple TVs: read their state from the Apple TV helper',
  triggers: [openhab.triggers.GenericCronTrigger(POLL)],
  execute: () => {
    for (const device of DEVICES) read(device);
  },
});

for (const device of DEVICES) {
  openhab.rules.JSRule({
    id: `pyatv_http_${device.id}_command`,
    name: `${device.id}: send its power, key and pairing commands through the Apple TV helper`,
    triggers: ['power', 'key', 'pairing'].map((point) => openhab.triggers.ItemCommandTrigger(`${device.id}_${point}`)),
    execute: (event) => {
      const point = event.itemName.slice(device.id.length + 1);
      const command = String(event.receivedCommand);
      if (point === 'key') key(device, command);
      else if (point === 'power') power(device, command);
      else pair(device, command);
    },
  });
}
