// Apple TVs through the Apple TV helper (lib/oh_integrations/pyatv_http,
// lib/oh_atv.py).
//
// DEVICES lists each Apple TV: its id (its Items are <id>_power, <id>_key,
// <id>_power_reported_at, <id>_pairing and <id>_pairing_status), its address and the URL of the
// helper that drives it. The helper keeps one connection per Apple TV and
// answers every request with the Apple TV's state: whether it is paired and
// connected, its power, and whether it is showing a pairing code.
//
// - Every POLL the rule asks the helper for each Apple TV's state and posts
//   it: its power changed or not (thermiteDevice.reading), its pairing
//   status when it changes. One the helper does not answer for gets UNDEF
//   and "unavailable". A paired Apple TV the helper is still connecting to
//   is "checking", and one it cannot reach "unreachable".
// - A button id sent to <id>_key is pressed as PRESSES gives it, or opens the
//   app APPS gives it.
// - ON or OFF sent to <id>_power wakes the Apple TV or puts it to sleep.
// - REQUEST sent to <id>_pairing makes the Apple TV show four digits; the
//   digits sent to it then pair the helper with it. A code it refuses shows
//   "failed" for FAILED_MS, unless a new code is asked for sooner.
//
// Each command returns the current native state, which may still precede the
// command; it is posted with its original native acquisition time.
// Anything else is dropped. With SEND false (oh_render.read_only) the rule
// still reads, and logs what it would send. openHAB runs one rule of a script
// file at a time, so a slow request holds only this device's next request back.
// Each rendered device gets its own context, including its ordered command
// and poll rules. A slow peer cannot hold another device's context.
const DEVICES = null;
const PRESSES = null;
const APPS = null;
const SEND = true;

const openhab = require('openhab');
let routingLifecycleActor = null;

// THERMITE_BOUNDED_IO
// THERMITE_DEVICE
const { has, reading, status, withheld } = thermiteDevice;

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

// device id -> the time until which its pairing shows failed.
const refused = {};
// The Apple TVs whose helper did not answer the last request: logged once.
const health = thermiteDevice.health();

function powerObservation(device, data) {
  const power = data && data.connected === true && typeof data.power === 'string' && has(POWER, data.power)
    ? POWER[data.power] : 'UNDEF';
  const at = data && data.power_reported_at;
  // The helper supplies UTC epoch milliseconds. Never substitute the poll or
  // acknowledgement time, coerce a string, or give unknown power a timestamp.
  const stamp = power !== 'UNDEF' && typeof at === 'number' && Number.isSafeInteger(at)
    && at > 0 && at <= 253402300799999 ? new Date(at).toISOString() : 'UNDEF';
  // Consumers pair one timestamp event with the following power event. Emit
  // both even when unchanged: a repeated poll retains the original acquisition
  // time, while a repeated native same-value report can advance it honestly.
  reading(`${device.id}_power_reported_at`, stamp);
  reading(`${device.id}_power`, power);
}

// {code, data} of the helper's answer (data null unless JSON). A helper that
// did not answer has code 0, and `unreached` when no request byte got to it.
function call(device, path, body, ms) {
  const headers = { Accept: 'application/json' };
  if (body !== null) headers['Content-Type'] = 'application/json';
  let response;
  try {
    response = thermiteIO.http(device.helper + path, body === null ? 'GET' : 'POST',
      body === null ? null : JSON.stringify(Object.assign({ host: device.host }, body)), headers, ms, CONNECT_MS);
  } catch (e) { return { code: 0, data: null, unreached: thermiteIO.unreached(e) }; }
  let data = null;
  try {
    data = JSON.parse(String(response.getContentAsString()));
  } catch (e) {
    data = null;
  }
  return { code: Number(response.getStatus()), data };
}

function pairingState(device, data) {
  if (data.paired && data.connected) return 'paired';
  if (data.pairing === 'code_shown') return 'code_shown';
  if (has(refused, device.id) && Date.now() < refused[device.id]) return 'failed';
  if (!data.paired) return 'unpaired';
  return data.error ? 'unreachable' : 'checking';
}

// Posts the state an answer carries; an answer without one means the helper
// is not there. A helper that answers may still say the Apple TV is not
// paired, or does not answer it: that stays the device's problem.
function show(device, answer) {
  const data = answer.data;
  if (!data || typeof data !== 'object' || data.id !== device.id) {
    health.failed(device.id, `the Apple TV helper at ${device.helper} does not answer; `
      + 'its Items are UNDEF until it does', answer.code !== 0 ? 'protocol' : answer.unreached ? 'unreachable' : 'no_answer');
    powerObservation(device, null);
    status(`${device.id}_pairing_status`, 'unavailable');
    return;
  }
  health.answered(device.id, 'the Apple TV helper answers again',
    !data.paired ? 'auth' : !data.connected && data.error ? 'unreachable' : null);
  powerObservation(device, data);
  status(`${device.id}_pairing_status`, pairingState(device, data));
}

function read(device) {
  show(device, call(device, `/devices/${device.id}?host=${encodeURIComponent(device.host)}`, null, READ_MS));
}

// Sends one command and logs how it went; `what` names it in the log (never
// the code itself). Returns the answer (see call()).
function request(device, path, body, what, ms, operation) {
  const payload = operation ? {...body, operation} : body;
  const answer = call(device, `/devices/${device.id}${path}`, payload, ms || SEND_MS);
  if (answer.code === 0) {
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
  if (answer.code !== 0 && !(answer.data && answer.data.id === device.id)) read(device);
  else show(device, answer);
}

// How a command ended, from the helper's answer, for the command record
// (thermiteDevice.commandRule), with the answer: {outcome, code, answer}.
// The helper declines with a 4xx before it asks the Apple TV anything (409:
// it is not paired, or shows no code to check), but for 422, the Apple TV
// refusing the pairing code; 502 is the Apple TV not answering the helper,
// which may have sent the command.
function concluded(answer) {
  if (answer.code === 200) return { outcome: 'applied', answer };
  if (answer.code === 0) {
    return answer.unreached ? { outcome: 'unsent', code: 'unreachable', answer } : { outcome: 'unknown', code: 'no_answer', answer };
  }
  if (answer.code === 422) return { outcome: 'refused', code: 'auth', answer };
  if (answer.code >= 400 && answer.code < 500) return { outcome: 'unsent', code: answer.code === 409 ? 'auth' : 'protocol', answer };
  return { outcome: 'unknown', code: answer.code === 502 ? 'no_answer' : 'other', answer };
}
const INVALID = Object.freeze({ outcome: 'dropped', code: 'invalid' });
const READ_ONLY = Object.freeze({ outcome: 'dropped', code: 'read_only' });

function send(device, path, body, what, ms, operation) {
  if (withheld(device, `sending ${what}`)) return READ_ONLY;
  const answer = request(device, path, body, what, ms, operation);
  settle(device, answer);
  return concluded(answer);
}

function key(device, button, operation) {
  if (has(PRESSES, button)) return send(device, '/key', { key: PRESSES[button] }, `key ${PRESSES[button]}`, undefined, operation);
  if (has(APPS, button)) return send(device, '/app', { app: APPS[button] }, `app ${APPS[button]}`, undefined, operation);
  console.warn(`${device.id}_key: no key for '${button}', dropped`);
  return INVALID;
}

function power(device, command) {
  if (command !== 'ON' && command !== 'OFF') {
    console.warn(`${device.id}_power: '${command}' is not ON or OFF, dropped`);
    return INVALID;
  }
  return send(device, '/power', { on: command === 'ON' }, `power ${command}`);
}

function pair(device, command, operation) {
  if (command === REQUEST) {
    delete refused[device.id];
    return send(device, '/pair', {}, 'a request for a pairing code', PAIR_MS, operation);
  }
  if (CODE.test(command)) {
    if (withheld(device, 'sending the pairing code')) return READ_ONLY;
    status(`${device.id}_pairing_status`, 'checking');
    const answer = request(device, '/pair', { code: command }, 'the pairing code', PAIR_MS, operation);
    if (answer.code !== 0 && answer.code !== 200) refused[device.id] = Date.now() + FAILED_MS;
    settle(device, answer);
    return concluded(answer);
  }
  console.warn(`${device.id}_pairing: a command that is neither ${REQUEST} nor four digits, dropped`);
  return INVALID;
}

openhab.rules.JSRule({
  id: `pyatv_http_${DEVICES[0].id}_poll`,
  name: 'Apple TVs: read their state from the Apple TV helper',
  triggers: [openhab.triggers.GenericCronTrigger(POLL)],
  execute: () => {
    for (const device of DEVICES) read(device);
  },
});

for (const device of DEVICES) {
  thermiteDevice.commandRule({
    id: `pyatv_http_${device.id}_command`,
    name: `${device.id}: send its power, key and pairing commands through the Apple TV helper`,
    device: device.id,
    items: ['power', 'key', 'pairing'].map((point) => `${device.id}_${point}`),
    actor: () => routingLifecycleActor,
    direct: (event) => directRoutingCommand(device, event),
  });
}

function directRoutingCommand(device, event, operation) {
  const point = event.itemName.slice(device.id.length + 1);
  const command = String(event.receivedCommand);
  if (point === 'key') return key(device, command, operation);
  if (point === 'power') return power(device, command);
  return pair(device, command, operation);
}

function routingCorrelatedPower(operation, kind) {
  const device = DEVICES[0];
  const body = { operation: operation.token, kind };
  if (kind === 'power') body.on = operation.value === 'ON';
  const answer = call(device, `/devices/${device.id}/lifecycle`, body, SEND_MS);
  if (!answer.data || answer.data.operation !== operation.token || answer.data.id !== device.id
    || typeof answer.data.terminal !== 'boolean' || typeof answer.data.submitted !== 'boolean') return null;
  show(device, answer);
  return {...answer.data, _httpStatus: answer.code};
}
function routingSample(data) {
  if (!data || data._httpStatus !== 200 || data.terminal !== true || data.connected !== true
    || !Number.isSafeInteger(data.native_sequence) || data.native_sequence < 1
    || !Number.isSafeInteger(data.power_reported_at) || data.power_reported_at <= 0 || !has(POWER, data.power)) throw Error('native_power_not_confirmed');
  return {value: POWER[data.power], fresh: true, basis: 'native_power', observedAt: data.power_reported_at,
    nativeSequence: data.native_sequence};
}
function routingProvider() {
  const device = DEVICES[0];
  return {
    // HID keys and launching an app may wake the source. They cannot be
    // treated as neutral while an automatic standby is queued.
    effects: resource => resource === `${device.id}_key` ? [`${device.id}_power`] : [],
    oneShot: resource => resource === `${device.id}_key` || resource === `${device.id}_pairing`,
    has: (resource, value) => resource === `${device.id}_power` ? ['ON','OFF'].includes(String(value)) :
      resource === `${device.id}_key` ? has(PRESSES,value) || has(APPS,value) :
      resource === `${device.id}_pairing` && (value === REQUEST || CODE.test(value)),
    execute: (resource, value, operation) => {
      if (resource === `${device.id}_power`) {
        const data = routingCorrelatedPower({...operation,value}, 'power');
        // A non-200 response can retire only a proved unsubmitted operation.
        // A submitted power command needs a genuine correlated native report.
        if (!data || data.terminal !== true) return {outcome:'unknown', code:'no_answer'};
        if (data._httpStatus !== 200) {
          return {outcome: data.submitted === false ? 'unsent' : 'unknown', code: concluded({code:data._httpStatus}).code};
        }
        if (data.ok !== true) return {outcome:'unknown', code:'other'};
        return {outcome:'applied', sample:routingSample(data)};
      }
      const sent = directRoutingCommand(device, {itemName:resource, receivedCommand:value}, operation.token);
      const answer = sent.answer;
      // Status alone cannot distinguish a refused PIN from a timed-out
      // submission. Only the helper's correlated completion fact can retire
      // this one-shot attempt and permit a new explicit request. A retired
      // attempt is the executor's `applied` even when the helper declined
      // it: `record` says how the command itself ended.
      const done = !!answer && !!answer.data && answer.data.id === device.id
        && answer.data.operation === operation.token && answer.data.terminal === true;
      return done ? {outcome:'applied', record:{outcome:sent.outcome, code:sent.code}}
        : {outcome:'unknown', code:sent.outcome === 'applied' ? 'protocol' : sent.code};
    },
    probe: (_resource, operation) => routingSample(routingCorrelatedPower(operation, 'probe')),
  };
}

// Set only by the routing lifecycle renderer. Legacy configuration keeps
// this device's existing command rule and protocol ownership.
const ROUTING_EXECUTOR = null;
if (ROUTING_EXECUTOR) routingLifecycleActor = require('thermite-routing-io').actor(ROUTING_EXECUTOR, routingProvider(),
  {readonly: !SEND, record: thermiteDevice.recorder(DEVICES[0].id)});
