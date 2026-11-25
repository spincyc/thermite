// NuHeat thermostats through the localhost climate helper
// (lib/oh_integrations/nuheat_http, lib/oh_climate/daemon.py).
//
// DEVICES lists each thermostat's id and localhost helper URL. Every POLL (60 s)
// the rule polls GET /devices/<id> and posts read-only status/fact Items and
// command readbacks. Commands on <id>_climate_mode and <id>_climate_setpoint
// POST to /devices/<id>/mode (withheld when SEND is false). Indeterminate cloud
// write outcomes are never retried and never reported as confirmed.
const DEVICES = null;
const SEND = true;

const openhab = require('openhab');

// THERMITE_BOUNDED_IO
// THERMITE_DEVICE
const { reading, status, withheld } = thermiteDevice;

const POLL = '0 * * * * ?';
const CONNECT_MS = 1000;
const READ_MS = 5000;
const SEND_MS = 12000;
const MODES = ['Auto', 'Hold', 'Manual'];
const WRITE_STATES = ['idle', 'acknowledged', 'confirmed', 'different', 'indeterminate'];
const INT_PATTERN = /^-?\d+$/;

const health = thermiteDevice.health();
const lastState = {};

function call(device, method, path, body, ms) {
  const headers = { Accept: 'application/json' };
  if (body !== null) headers['Content-Type'] = 'application/json';
  let response;
  try {
    response = thermiteIO.http(device.helper + path, method,
      body === null ? null : JSON.stringify(body), headers, ms, CONNECT_MS);
  } catch (e) {
    return { code: 0, data: null, unreached: thermiteIO.unreached(e) };
  }
  let data = null;
  try {
    data = JSON.parse(String(response.getContentAsString()));
  } catch (_) {
    data = null;
  }
  return { code: Number(response.getStatus()), data };
}

function climateStatus(data) {
  if (!data.commissioned) return 'uncommissioned';
  if (data.has_error === true) return 'error';
  if (data.online === false) return 'offline';
  if (data.stale === true) return 'stale';
  if (data.ok === false) return 'unavailable';
  return 'ok';
}

function readProblemCode(data) {
  if (!data.commissioned) return 'auth';
  if (data.ok === false) {
    if (data.error === 'unauthorized' || data.error === 'forbidden' || data.error === 'authorization_unavailable') {
      return 'auth';
    }
    if (data.error === 'transport_unavailable') return 'no_answer';
    return 'other';
  }
  if (data.online === false) return 'unreachable';
  return null;
}

function show(device, answer) {
  const data = answer.data;
  if (!data || typeof data !== 'object' || data.id !== device.id) {
    health.failed(device.id, `the climate helper at ${device.helper} does not answer; `
      + 'thermostat Items are UNDEF until it does',
      answer.code !== 0 ? 'protocol' : answer.unreached ? 'unreachable' : 'no_answer');
    for (const suffix of ['temperature', 'climate_setpoint', 'climate_mode', 'heating_call',
      'hold_until', 'online', 'has_error', 'stale', 'write_state']) {
      reading(`${device.id}_${suffix}`, 'UNDEF');
    }
    status(`${device.id}_climate_status`, 'unavailable');
    return;
  }
  health.answered(device.id, 'the climate helper answers again', readProblemCode(data));
  const temp = typeof data.temperature_display === 'number' && Number.isFinite(data.temperature_display)
    ? String(data.temperature_display)
    : typeof data.temperature_raw === 'number' && Number.isFinite(data.temperature_raw)
      ? String(data.temperature_raw) : 'UNDEF';
  const setpoint = typeof data.setpoint_raw === 'number' && Number.isSafeInteger(data.setpoint_raw)
    ? String(data.setpoint_raw) : 'UNDEF';
  const mode = typeof data.mode === 'string' && MODES.includes(data.mode) ? data.mode : 'UNDEF';
  const heating = typeof data.heating_call === 'boolean' ? (data.heating_call ? 'ON' : 'OFF') : 'UNDEF';
  const holdUntil = typeof data.hold_until === 'string' && data.hold_until ? data.hold_until : 'UNDEF';
  const online = typeof data.online === 'boolean' ? (data.online ? 'ON' : 'OFF') : 'UNDEF';
  const hasError = typeof data.has_error === 'boolean' ? (data.has_error ? 'ON' : 'OFF') : 'UNDEF';
  const stale = typeof data.stale === 'boolean' ? (data.stale ? 'ON' : 'OFF') : 'UNDEF';
  const writeState = typeof data.last_write_state === 'string' && WRITE_STATES.includes(data.last_write_state)
    ? data.last_write_state : 'UNDEF';

  reading(`${device.id}_temperature`, temp);
  reading(`${device.id}_climate_setpoint`, setpoint);
  reading(`${device.id}_climate_mode`, mode);
  reading(`${device.id}_heating_call`, heating);
  reading(`${device.id}_hold_until`, holdUntil);
  reading(`${device.id}_online`, online);
  reading(`${device.id}_has_error`, hasError);
  reading(`${device.id}_stale`, stale);
  reading(`${device.id}_write_state`, writeState);
  status(`${device.id}_climate_status`, climateStatus(data));

  lastState[device.id] = {
    mode: mode !== 'UNDEF' ? mode : null,
    setpointRaw: typeof data.setpoint_raw === 'number' && Number.isSafeInteger(data.setpoint_raw)
      ? data.setpoint_raw : null,
    holdUntil: holdUntil !== 'UNDEF' ? holdUntil : null,
    writableModes: Array.isArray(data.writable_modes) ? data.writable_modes : [],
  };
}

function read(device) {
  show(device, call(device, 'GET', `/devices/${device.id}`, null, READ_MS));
}

function concluded(answer) {
  if (answer.code === 200) return { outcome: 'applied', answer };
  if (answer.code === 0) {
    return answer.unreached
      ? { outcome: 'unsent', code: 'unreachable', answer }
      : { outcome: 'unknown', code: 'no_answer', answer };
  }
  if (answer.code === 429) return { outcome: 'refused', code: 'busy', answer };
  if (answer.code === 409) return { outcome: 'unsent', code: 'auth', answer };
  if (answer.code >= 400 && answer.code < 500) return { outcome: 'refused', code: 'protocol', answer };
  return { outcome: 'unknown', code: answer.code === 502 ? 'no_answer' : 'other', answer };
}

const INVALID = Object.freeze({ outcome: 'dropped', code: 'invalid' });
const READ_ONLY = Object.freeze({ outcome: 'dropped', code: 'read_only' });

function sendMode(device, payload, what) {
  if (withheld(device, `sending ${what}`)) return READ_ONLY;
  const answer = call(device, 'POST', `/devices/${device.id}/mode`, payload, SEND_MS);
  if (answer.code === 0) {
    console.warn(`${device.id}: climate helper at ${device.helper} did not answer; ${what} outcome unknown`);
  } else if (answer.code !== 200) {
    const why = answer.data && answer.data.error ? answer.data.error : `HTTP ${answer.code}`;
    console.warn(`${device.id}: ${what}: ${why}`);
  } else {
    console.info(`${device.id}: sent ${what}`);
  }
  if (answer.code !== 0 && !(answer.data && answer.data.id === device.id)) read(device);
  else show(device, answer);
  return concluded(answer);
}

function handleCommand(device, event) {
  const point = event.itemName.slice(device.id.length + 1);
  const raw = String(event.receivedCommand).trim();
  const cached = lastState[device.id] || {};
  if (point === 'climate_mode') {
    if (!MODES.includes(raw)) {
      console.warn(`${device.id}_climate_mode: '${raw}' is not Auto, Hold or Manual, dropped`);
      return INVALID;
    }
    const payload = { mode: raw };
    if (raw !== 'Auto' && typeof cached.setpointRaw === 'number') {
      payload.setpoint_raw = cached.setpointRaw;
    }
    return sendMode(device, payload, `climate mode ${raw}`);
  }
  if (point === 'climate_setpoint') {
    if (!INT_PATTERN.test(raw)) {
      console.warn(`${device.id}_climate_setpoint: '${raw}' is not an integer raw setpoint, dropped`);
      return INVALID;
    }
    const target = Number(raw);
    if (!Number.isSafeInteger(target)) {
      console.warn(`${device.id}_climate_setpoint: '${raw}' out of integer range, dropped`);
      return INVALID;
    }
    const writable = Array.isArray(cached.writableModes) ? cached.writableModes : [];
    const holdValid = cached.mode === 'Hold' && typeof cached.holdUntil === 'string'
      && Date.parse(cached.holdUntil) > Date.now();
    let mode = 'Manual';
    const payload = { setpoint_raw: target };
    if (holdValid && (writable.length === 0 || writable.includes('Hold'))) {
      mode = 'Hold';
      payload.hold_until = cached.holdUntil;
    } else if (writable.length > 0 && !writable.includes('Manual') && writable.includes('Hold')) {
      mode = 'Hold';
    }
    payload.mode = mode;
    return sendMode(device, payload, `climate setpoint ${target} (${mode})`);
  }
  return INVALID;
}

openhab.rules.JSRule({
  id: `nuheat_http_${DEVICES[0].id}_poll`,
  name: 'NuHeat thermostats: read state from the climate helper',
  triggers: [openhab.triggers.GenericCronTrigger(POLL)],
  execute: () => {
    for (const device of DEVICES) read(device);
  },
});

for (const device of DEVICES) {
  thermiteDevice.commandRule({
    id: `nuheat_http_${device.id}_command`,
    name: `${device.id}: send mode and setpoint commands through the climate helper`,
    device: device.id,
    items: [`${device.id}_climate_mode`, `${device.id}_climate_setpoint`],
    direct: (event) => handleCommand(device, event),
  });
}
