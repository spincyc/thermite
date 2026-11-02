// Sony BRAVIA TVs over Sony's IP control, with its "Normal" authentication
// (lib/oh_integrations/sony_ip_control).
//
// DEVICES lists each TV openHAB controls: its id (its Items are <id>_power,
// which sony_rest's HTTP Thing reads as well, <id>_input, <id>_volume,
// <id>_mute, <id>_key, <id>_pairing, <id>_pairing_status and <id>_problem),
// the address of its web server, the numbers of its HDMI inputs and the
// volume it is held to. speakerAudio false omits the TV's volume/mute Items,
// commands and reads; it does not affect the room's separate audio device.
// PRESSES gives the IRCC code of each remote button.
//
// - Pairing: REQUEST sent to <id>_pairing asks the TV to register openHAB
//   (accessControl actRegister) without a PIN; the TV answers 401 and shows
//   four digits, and <id>_pairing_status is code_shown for CODE_MS. The
//   digits sent back go with the same request as HTTP Basic credentials
//   (an empty user, the PIN as password); the TV answers 200 with an auth
//   cookie, and the client id, the PIN and the cookie with its expiry are
//   kept in openHAB's userdata (sony_ip_control/<id>.json, mode 0600),
//   never in an Item or the log. A PIN the TV refuses shows failed for
//   FAILED_MS; a TV that does not answer, unreachable as long.
// - Every request but getPowerStatus carries the cookie. Within RENEW_MS of
//   its expiry, or when the TV refuses it (HTTP 401 or 403), the rule
//   registers again with the stored PIN for a new cookie, at most once per
//   RETRY_MS. A TV that refuses that is not paired any more: the rule sends
//   it nothing and tries no more until it is paired again, since each
//   refused registration may put a PIN on its screen.
// - Every 15 s it reads the input (getPlayingContentInfo), volume and mute
//   (getVolumeInformation) of each TV whose power is ON, and posts them: an
//   input is hdmi<n> for an HDMI input, tv for anything else on the screen
//   (an app, the home screen, the tuner). A failed input or sound read clears
//   only those readings; its problem stays until that read recovers. Standby
//   or an unusable registration clears all three readings to UNDEF.
// - A command to a TV's power, input, volume or mute is sent (setPowerStatus,
//   setPlayContent, setAudioVolume held to the TV's max, setAudioMute), then
//   read back at once; a remote button id sent to <id>_key is sent as its
//   IRCC code (X_SendIRCC). Anything else is dropped.
//
// A TV that is not paired, refuses its registration or does not answer is
// logged once, until that changes, and that is the state of <id>_problem
// (UNDEF while there is none), which the remote page shows. With SEND false
// (oh_render.read_only) the rule still reads, pairs and renews nothing, and
// logs what it would send.
const DEVICES = null;
const PRESSES = null;
const SEND = true;

const openhab = require('openhab');

const HttpClient = Java.type('java.net.http.HttpClient');
const HttpVersion = Java.type('java.net.http.HttpClient$Version');
const HttpRequest = Java.type('java.net.http.HttpRequest');
const BodyPublishers = Java.type('java.net.http.HttpRequest$BodyPublishers');
const BodyHandlers = Java.type('java.net.http.HttpResponse$BodyHandlers');
const URI = Java.type('java.net.URI');
const Duration = Java.type('java.time.Duration');
const Files = Java.type('java.nio.file.Files');
const Paths = Java.type('java.nio.file.Paths');
const StandardCopyOption = Java.type('java.nio.file.StandardCopyOption');
const PosixFilePermissions = Java.type('java.nio.file.attribute.PosixFilePermissions');
const UUID = Java.type('java.util.UUID');
const OpenHAB = Java.type('org.openhab.core.OpenHAB');

const POLL = '0/15 * * * * ?';
const CONNECT_MS = 2000;
const ANSWER_MS = 4000;
const DAY_MS = 24 * 60 * 60 * 1000;
// How long a TV shows its PIN, how long a refused PIN or no answer shows.
const CODE_MS = 180000;
const FAILED_MS = 60000;
// Renew a cookie this long before it runs out, and try again after a
// failed attempt no sooner than this.
const RENEW_MS = 2 * DAY_MS;
const RETRY_MS = 10 * 60 * 1000;
// A cookie whose Set-Cookie names no lifetime: Sony's is 14 days.
const LIFE_MS = 14 * DAY_MS;
const NICKNAME = 'thermite';
// Identity is independent of the visible label: retain existing registrations.
const CLIENT_PREFIX = 'openHAB';
const DIR = 'sony_ip_control';
const REQUEST = 'REQUEST';
const PIN = /^[0-9]{4}$/;
// Sony's error codes for "the screen shows nothing the API can name" (an
// app, the home screen) and "the TV is in standby".
const ILLEGAL_STATE = 7;
const DISPLAY_OFF = 40005;
const HDMI_URI = /^extInput:hdmi\?port=(\d+)$/;
const INPUT = /^hdmi(\d+)$/;
const NUMBER = /^\d+(\.\d+)?$/;
// getPowerStatus -> the power Switch.
const POWER = { active: 'ON', standby: 'OFF' };
const NOT_PAIRED = 'not paired: pair it on the Pairing page (on the TV: Settings, Network, Home network, '
  + 'IP control: Authentication Normal, Remote Start on)';
const REFUSED = 'refused its registration: pair it again on the Pairing page';
const IRCC_ACTION = '"urn:schemas-sony-com:service:IRCC:1#X_SendIRCC"';
const BASE64 = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/';

// HTTP/1.1: the TVs' web server is not known to take an upgrade to HTTP/2.
const client = HttpClient.newBuilder().version(HttpVersion.HTTP_1_1)
  .connectTimeout(Duration.ofMillis(CONNECT_MS)).build();
// TV id -> the problem last logged for it.
const problems = new Map();
// TV id -> unresolved input/sound read failures. A successful command or
// partial read does not establish that the other reading has recovered.
const readProblems = new Map();
// A later poll answers a different question from whether a command worked.
// Retain the latest failed command until a subsequent command is accepted.
const commandProblems = new Map();
// TV id -> its registration {clientid, nickname, pin, cookie, expires,
// refused}, or null.
const auths = new Map();
// TV id -> {asked, clientid} while its PIN is on screen, or {until, state}
// while a refused PIN or no answer shows.
const pairings = new Map();
// TV id -> when its cookie was last renewed or tried.
const renewed = new Map();

function has(object, key) {
  return Object.prototype.hasOwnProperty.call(object, key);
}

// A String or Switch Item's state; null for NULL or UNDEF.
function reported(name) {
  const state = String(openhab.items.getItem(name).state);
  return state === 'NULL' || state === 'UNDEF' ? null : state;
}

function update(name, state) {
  // Null while a reload has the rules in before the Items: the next poll posts it.
  const item = openhab.items.getItem(name, true);
  if (item && String(item.state) !== state) item.postUpdate(state);
}

// An Error whose message is a TV's problem as the remote page shows it, and
// whose detail only the log adds.
function failure(problem, detail) {
  const error = new Error(problem);
  error.detail = detail || '';
  return error;
}

// Logs a TV's problem when it is new and its end (problem null, said as
// ended), and keeps <id>_problem showing it.
function note(device, problem, detail, ended) {
  if (problem === null) {
    const unresolved = readProblems.get(device.id);
    if (unresolved && unresolved.size) {
      const reading = unresolved.values().next().value;
      problem = reading.message;
      detail = reading.detail;
    }
    if (problem === null && commandProblems.has(device.id)) {
      const command = commandProblems.get(device.id);
      problem = command.message;
      detail = command.detail;
    }
  }
  const last = problems.has(device.id) ? problems.get(device.id) : null;
  if (problem !== last && problem === null) {
    problems.delete(device.id);
    console.info(`${device.id}: ${ended || 'answers again'}`);
  } else if (problem !== last) {
    problems.set(device.id, problem);
    console.warn(`${device.id}: ${problem}${detail ? ` (${detail})` : ''}`);
  }
  update(`${device.id}_problem`, problem === null ? 'UNDEF' : problem);
}

function commandNote(device, error) {
  if (error) commandProblems.set(device.id, error);
  else commandProblems.delete(device.id);
  note(device, error ? error.message : null, error ? error.detail : '');
}

function base64(text) {
  let out = '';
  for (let i = 0; i < text.length; i += 3) {
    const [a, b, c] = [text.charCodeAt(i), text.charCodeAt(i + 1), text.charCodeAt(i + 2)];
    const bits = (a << 16) | ((b || 0) << 8) | (c || 0);
    out += BASE64[(bits >> 18) & 63] + BASE64[(bits >> 12) & 63]
      + (i + 1 < text.length ? BASE64[(bits >> 6) & 63] : '=') + (i + 2 < text.length ? BASE64[bits & 63] : '=');
  }
  return out;
}

// ---- the registrations in userdata ----------------------------------------

function folder() {
  return Paths.get(`${String(OpenHAB.getUserDataFolder())}/${DIR}`);
}

function load(device) {
  const path = folder().resolve(`${device.id}.json`);
  if (!Files.exists(path)) return null;
  try {
    const auth = JSON.parse(String(Files.readString(path)));
    return auth && typeof auth === 'object' && typeof auth.clientid === 'string' ? auth : null;
  } catch (e) {
    console.warn(`${device.id}: cannot read its registration in ${path}, so it is not paired (${e})`);
    return null;
  }
}

// Writes the registration as <id>.json, mode 0600 from the start, in a 0700
// directory, by moving a new file over the old one.
function save(device, auth) {
  auths.set(device.id, auth);
  const dir = folder();
  if (!Files.isDirectory(dir)) {
    Files.createDirectories(dir, PosixFilePermissions.asFileAttribute(PosixFilePermissions.fromString('rwx------')));
  }
  const temp = Files.createTempFile(dir, `.${device.id}.`, '.tmp',
    PosixFilePermissions.asFileAttribute(PosixFilePermissions.fromString('rw-------')));
  Files.writeString(temp, JSON.stringify(auth));
  Files.move(temp, dir.resolve(`${device.id}.json`), StandardCopyOption.REPLACE_EXISTING,
    StandardCopyOption.ATOMIC_MOVE);
}

// ---- HTTP --------------------------------------------------------------------

// {status, body, cookies: [Set-Cookie values]}; throws when there is no answer.
function post(device, path, body, headers) {
  const builder = HttpRequest.newBuilder(URI.create(`http://${device.address}${path}`))
    .timeout(Duration.ofMillis(ANSWER_MS));
  for (const [name, value] of Object.entries(headers)) builder.header(name, value);
  let response;
  try {
    response = client.send(builder.POST(BodyPublishers.ofString(body)).build(), BodyHandlers.ofString());
  } catch (e) {
    throw failure('does not answer', String(e));
  }
  const list = response.headers().allValues('set-cookie');
  const cookies = [];
  for (let i = 0; i < list.size(); i += 1) cookies.push(String(list.get(i)));
  return { status: Number(response.statusCode()), body: String(response.body()), cookies };
}

// {cookie, expires} from an answer's Set-Cookie, or null without an auth cookie.
function cookieOf(answer) {
  for (const header of answer.cookies) {
    const match = /^\s*auth=([^;]+)/.exec(header);
    if (!match) continue;
    const age = /;\s*max-age=(\d+)/i.exec(header);
    const date = /;\s*expires=([^;]+)/i.exec(header);
    let expires = Date.now() + LIFE_MS;
    if (age) expires = Date.now() + Number(age[1]) * 1000;
    else if (date && Number.isFinite(Date.parse(date[1]))) expires = Date.parse(date[1]);
    return { cookie: match[1], expires };
  }
  return null;
}

// actRegister for clientid, with the PIN as Basic credentials unless pin is null.
function register(device, clientid, pin, nickname = NICKNAME) {
  const body = JSON.stringify({
    method: 'actRegister',
    id: 8,
    version: '1.0',
    params: [{ clientid, nickname, level: 'private' }, [{ value: 'yes', function: 'WOL' }]],
  });
  const headers = { 'Content-Type': 'application/json' };
  if (pin !== null) headers.Authorization = `Basic ${base64(`:${pin}`)}`; // lint: allow (the PIN in userdata)
  return post(device, '/sony/accessControl', body, headers);
}

// Sony can return an API error with HTTP 200. Never copy its message: a
// device's response could echo credentials. Only the known code below is
// published; every other API error is reported without its value or text.
function registrationCode(answer) {
  try {
    const parsed = JSON.parse(answer.body);
    if (!Array.isArray(parsed.error)) return null;
    return typeof parsed.error[0] === 'number' ? parsed.error[0] : 'unknown';
  } catch (e) {
    return null;
  }
}

function registrationProblem(answer) {
  const code = registrationCode(answer);
  const problem = { http: answer.status };
  if (code !== null) problem.api = true;
  if (code === 1) problem.code = 1;
  return problem;
}

function registrationMessage(problem) {
  const why = problem.code === 1 ? 'Sony error 1 (Internal Server Error)'
    : problem.api ? 'a Sony API error' : 'no authentication cookie';
  return `registration failed: ${why}, HTTP ${problem.http}`;
}

// A refusal of the cookie: HTTP 401 or 403, or Sony's error 401 or 403.
function refusedAnswer(answer) {
  if (answer.status === 401 || answer.status === 403) return true;
  try {
    const parsed = JSON.parse(answer.body);
    return Array.isArray(parsed.error) && (parsed.error[0] === 401 || parsed.error[0] === 403);
  } catch (e) {
    return false;
  }
}

// Registers again with the stored PIN for a new cookie; whether it got one.
// A refusal unpairs the TV; no answer leaves the old cookie to be tried.
function renew(device, why) {
  const auth = auths.get(device.id);
  if (!SEND || !auth || auth.refused || auth.pin === undefined || auth.pin === null) return false;
  renewed.set(device.id, Date.now());
  let answer;
  try {
    answer = register(device, auth.clientid, auth.pin, auth.nickname || CLIENT_PREFIX);
  } catch (e) {
    console.warn(`${device.id}: could not renew its cookie (${why}): ${e.message}`);
    return false;
  }
  const got = answer.status === 200 && registrationCode(answer) === null ? cookieOf(answer) : null;
  if (got) {
    save(device, Object.assign({}, auth, got));
    console.info(`${device.id}: renewed its cookie (${why})`);
    return true;
  }
  if (answer.status === 401 || answer.status === 403) {
    save(device, Object.assign({}, auth, { refused: true }));
    note(device, REFUSED, `actRegister with its PIN answered HTTP ${answer.status} (${why})`);
  } else {
    console.warn(`${device.id}: could not renew its cookie (${why}): ${registrationMessage(registrationProblem(answer))}`);
  }
  return false;
}

// The TV's registration, renewed first when due; throws its problem when it
// has none it may use.
function usable(device) {
  const auth = auths.get(device.id);
  if (!auth || !auth.cookie) throw failure(NOT_PAIRED);
  if (auth.refused) throw failure(REFUSED);
  const last = renewed.get(device.id) || 0;
  if (Date.now() > auth.expires - RENEW_MS && Date.now() - last >= RETRY_MS) {
    renew(device, auth.expires > Date.now() ? 'it runs out soon' : 'it ran out');
  }
  const now = auths.get(device.id);
  if (now.refused) throw failure(REFUSED);
  return now;
}

// One request with the TV's cookie: {status, body}. A refused cookie is
// renewed once and the request sent again; throws the TV's problem when it
// cannot be sent or is refused.
function authed(device, path, body, headers) {
  const send = () => post(device, path, body, Object.assign({ Cookie: `auth=${usable(device).cookie}` }, headers));
  let answer = send();
  if (refusedAnswer(answer)) {
    const last = renewed.get(device.id) || 0;
    if (Date.now() - last >= RETRY_MS && renew(device, `HTTP ${answer.status} to its cookie`)) answer = send();
    if (refusedAnswer(answer)) {
      const auth = auths.get(device.id);
      throw failure(auth && auth.refused ? REFUSED : `refuses its cookie: pair it again on the Pairing page`,
        `HTTP ${answer.status}`);
    }
  }
  return answer;
}

function json(method, params, version) {
  return JSON.stringify({ method, id: 1, params, version: version || '1.0' });
}

// One JSON request with the cookie: its answer, {result} or {error: [code,
// message]}. Throws an Error naming the problem when there is none.
function call(device, service, method, params, version) {
  const answer = authed(device, `/sony/${service}`, json(method, params, version),
    { 'Content-Type': 'application/json' });
  if (answer.status !== 200) throw failure(`answered ${method} with HTTP ${answer.status}`);
  try {
    return JSON.parse(answer.body);
  } catch (e) {
    throw failure(`answered ${method} with something other than JSON`);
  }
}

// ---- reading -----------------------------------------------------------------

// Whether the TV has a registration it may use; a TV asked nothing has its
// pairing problem shown, and a registration stored since ends it.
function checkAuth(device) {
  const auth = auths.get(device.id);
  const problem = !auth || !auth.cookie ? NOT_PAIRED : auth.refused ? REFUSED : null;
  if (problem !== null) {
    note(device, problem);
    return false;
  }
  const last = problems.get(device.id);
  if (last === NOT_PAIRED || last === REFUSED) {
    const command = commandProblems.get(device.id);
    if (command && (command.message === NOT_PAIRED || command.message === REFUSED)) commandProblems.delete(device.id);
    note(device, null, '', 'is paired, so it is sent commands again');
  }
  return true;
}

// HTTP 200 can still carry a Sony error or no usable reading. Publish only
// fixed classifications; the error text and unrecognised code may be private.
function readResult(answer, method) {
  if (!answer || typeof answer !== 'object' || Array.isArray(answer)) {
    throw failure(`answered ${method} without valid readback`);
  }
  if (has(answer, 'error')) {
    const code = Array.isArray(answer.error) ? answer.error[0] : null;
    const error = failure(`answered ${method} with ${code === 1 ? 'Sony error 1' : 'a Sony API error'}`);
    error.displayOff = code === DISPLAY_OFF;
    throw error;
  }
  return answer.result;
}

function inputOf(answer) {
  if (answer && Array.isArray(answer.error) && answer.error[0] === ILLEGAL_STATE) return 'tv';
  const result = readResult(answer, 'getPlayingContentInfo');
  const content = Array.isArray(result) && result[0];
  if (!content || typeof content.uri !== 'string' || !content.uri.trim()) {
    throw failure('answered getPlayingContentInfo without valid readback');
  }
  const match = HDMI_URI.exec(content.uri);
  return match ? `hdmi${Number(match[1])}` : 'tv';
}

// [volume, mute] of the TV's speaker, as Item states.
function soundOf(answer) {
  const result = readResult(answer, 'getVolumeInformation');
  const targets = (Array.isArray(result) && Array.isArray(result[0]) && result[0]) || [];
  const speaker = targets.find((t) => t && t.target === 'speaker') || targets[0];
  const volume = speaker && speaker.volume;
  const numeric = typeof volume === 'number' || typeof volume === 'string' && /^\d+(\.\d+)?$/.test(volume);
  if (!speaker || typeof speaker.target !== 'string' || !speaker.target || !numeric
    || !Number.isFinite(Number(volume)) || Number(volume) < 0 || Number(volume) > 100 || typeof speaker.mute !== 'boolean') {
    throw failure('answered getVolumeInformation without valid readback');
  }
  return [String(Number(speaker.volume)), speaker.mute === true ? 'ON' : 'OFF'];
}

function off(device) {
  const points = device.speakerAudio ? ['input', 'volume', 'mute'] : ['input'];
  for (const point of points) update(`${device.id}_${point}`, 'UNDEF');
}

// Reads what the TV shows; a TV that is off, or not paired, is asked nothing.
function read(device, what, freshPower) {
  // postUpdate is asynchronous: a command's fresh read must not consult
  // the old Item state again in this same rule execution.
  const power = freshPower === undefined ? reported(`${device.id}_power`) : freshPower;
  if (power !== 'ON') {
    const failures = readProblems.get(device.id);
    const settledRead = power === 'OFF' && failures
      && [...failures.values()].some((reading) => reading.message === problems.get(device.id));
    if (power === 'OFF') readProblems.delete(device.id);
    off(device);
    if (checkAuth(device) && settledRead) note(device, null, '', 'is in standby');
    return;
  }
  if (!checkAuth(device)) {
    readProblems.delete(device.id);
    off(device);
    return;
  }
  const failures = readProblems.get(device.id) || new Map();
  if (what !== 'sound') {
    try {
      update(`${device.id}_input`, inputOf(call(device, 'avContent', 'getPlayingContentInfo', [])));
      failures.delete('input');
    } catch (e) {
      // Content and power are polled independently. During shutdown the
      // content API can report standby before the power Item catches up.
      // Confirm it directly before invalidating the selected input; a
      // standby error alone never manufactures an Off state.
      if (e.displayOff && readPower(device) === 'OFF') {
        read(device, what, 'OFF');
        return;
      }
      failures.set('input', { message: e.message, detail: e.detail });
      update(`${device.id}_input`, 'UNDEF');
    }
  }
  if (device.speakerAudio && what !== 'input') {
    try {
      const [volume, mute] = soundOf(call(device, 'audio', 'getVolumeInformation', []));
      update(`${device.id}_volume`, volume);
      update(`${device.id}_mute`, mute);
      failures.delete('sound');
    } catch (e) {
      if (e.displayOff && readPower(device) === 'OFF') {
        read(device, what, 'OFF');
        return;
      }
      failures.set('sound', { message: e.message, detail: e.detail });
      update(`${device.id}_volume`, 'UNDEF');
      update(`${device.id}_mute`, 'UNDEF');
    }
  }
  // A request may discover that the registration was revoked. No protected
  // reading remains valid once that happens, even if the other request worked.
  if (!checkAuth(device)) {
    readProblems.delete(device.id);
    off(device);
    return;
  }
  if (failures.size) readProblems.set(device.id, failures);
  else readProblems.delete(device.id);
  note(device, null);
}

// getPowerStatus needs no cookie, so a TV that is not paired is read too.
function readPower(device) {
  let power = null;
  try {
    const answer = post(device, '/sony/system', json('getPowerStatus', []), { 'Content-Type': 'application/json' });
    const parsed = answer.status === 200 ? JSON.parse(answer.body) : {};
    const status = Array.isArray(parsed.result) && parsed.result[0] ? String(parsed.result[0].status) : '';
    power = has(POWER, status) ? POWER[status] : null;
  } catch (e) { /* Leave an unanswered or malformed power report unknown. */ }
  update(`${device.id}_power`, power || 'UNDEF');
  return power;
}

// ---- pairing -----------------------------------------------------------------

function pairingState(device) {
  const pairing = pairings.get(device.id);
  const now = Date.now();
  if (pairing && pairing.asked !== undefined && now - pairing.asked < CODE_MS) return 'code_shown';
  if (pairing && pairing.until !== undefined && now < pairing.until) return pairing.state;
  if (pairing) pairings.delete(device.id);
  const auth = auths.get(device.id);
  return auth && auth.cookie && !auth.refused ? 'paired' : 'unpaired';
}

function showPairing(device) {
  update(`${device.id}_pairing_status`, pairingState(device));
}

function shown(device, state, ms, error) {
  pairings.set(device.id, { until: Date.now() + ms, state, error });
}

function ask(device) {
  if (!SEND) {
    console.info(`${device.id}: SEND is false, not asking it for a PIN`);
    return;
  }
  const auth = auths.get(device.id);
  const clientid = auth ? auth.clientid : `${CLIENT_PREFIX}:${String(UUID.randomUUID())}`;
  let answer;
  try {
    answer = register(device, clientid, null);
  } catch (e) {
    console.warn(`${device.id}: does not answer, so it was not asked for a PIN (${e.detail})`);
    shown(device, 'unreachable', FAILED_MS);
    return;
  }
  const code = registrationCode(answer);
  const got = answer.status === 200 && code === null ? cookieOf(answer) : null;
  if (answer.status === 401 && (code === null || code === 401)) {
    pairings.set(device.id, { asked: Date.now(), clientid });
    console.info(`${device.id}: asked to show a PIN`);
  } else if (got) {
    // A TV that still knows the client id may register it without a PIN.
    save(device, Object.assign({ pin: null }, auth, { clientid, nickname: NICKNAME, refused: false }, got));
    pairings.delete(device.id);
    console.info(`${device.id}: registered thermite again without a PIN`);
    checkAuth(device);
  } else {
    const error = registrationProblem(answer);
    console.warn(`${device.id}: ${registrationMessage(error)}`);
    shown(device, 'failed', FAILED_MS, error);
  }
}

function sendPin(device, pin) {
  if (!SEND) {
    console.info(`${device.id}: SEND is false, not sending the PIN`);
    return;
  }
  const pairing = pairings.get(device.id);
  const auth = auths.get(device.id);
  const clientid = pairing && pairing.clientid ? pairing.clientid
    : auth ? auth.clientid : `${CLIENT_PREFIX}:${String(UUID.randomUUID())}`;
  update(`${device.id}_pairing_status`, 'checking');
  let answer;
  try {
    answer = register(device, clientid, pin);
  } catch (e) {
    console.warn(`${device.id}: does not answer, so the PIN was not taken (${e.detail})`);
    shown(device, 'unreachable', FAILED_MS);
    return;
  }
  const got = answer.status === 200 && registrationCode(answer) === null ? cookieOf(answer) : null;
  if (got) {
    save(device, Object.assign({ clientid, nickname: NICKNAME, pin, refused: false }, got));
    pairings.delete(device.id);
    renewed.set(device.id, Date.now());
    console.info(`${device.id}: paired`);
    checkAuth(device);
  } else {
    const error = registrationProblem(answer);
    console.warn(`${device.id}: did not take the PIN (${registrationMessage(error)})`);
    shown(device, 'failed', FAILED_MS, error);
  }
}

function pair(device, command) {
  if (command === REQUEST) ask(device);
  else if (PIN.test(command)) sendPin(device, command);
  else console.warn(`${device.id}_pairing: a command that is neither ${REQUEST} nor four digits, dropped`);
  showPairing(device);
  // The status can be unchanged, or pass through checking between two REST
  // reads. A unique result acknowledges this command without retaining its PIN.
  const result = { id: String(UUID.randomUUID()), state: pairingState(device) };
  const pairing = pairings.get(device.id);
  if (pairing && pairing.error) result.error = pairing.error;
  update(`${device.id}_pairing_result`, JSON.stringify(result));
}

// ---- commands ----------------------------------------------------------------

// [service, method, params, version] for a command to one of a TV's Items,
// or null.
function requestFor(device, point, command) {
  if (point === 'power' && (command === 'ON' || command === 'OFF')) {
    return ['system', 'setPowerStatus', [{ status: command === 'ON' }]];
  }
  if (device.speakerAudio && point === 'mute' && (command === 'ON' || command === 'OFF')) {
    return ['audio', 'setAudioMute', [{ status: command === 'ON' }]];
  }
  if (device.speakerAudio && point === 'volume' && NUMBER.test(command)) {
    const volume = Math.min(Math.round(Number(command)), device.max);
    return ['audio', 'setAudioVolume', [{ target: 'speaker', volume: String(volume) }]];
  }
  const match = point === 'input' ? INPUT.exec(command) : null;
  if (match && device.inputs.includes(Number(match[1]))) {
    return ['avContent', 'setPlayContent', [{ uri: `extInput:hdmi?port=${Number(match[1])}` }]];
  }
  return null;
}

function send(device, request) {
  const [service, method, params, version] = request;
  const what = `${method} ${JSON.stringify(params)}`;
  if (!SEND) {
    console.info(`${device.id}: SEND is false, not sending ${what}`);
    return;
  }
  try {
    const answer = call(device, service, method, params, version);
    if (Array.isArray(answer.error) && answer.error[0] === DISPLAY_OFF) {
      throw failure(`cannot perform ${method}: the TV is in standby`);
    }
    if (!Array.isArray(readResult(answer, method))) throw failure(`answered ${method} without valid acknowledgement`);
    commandNote(device, null);
    console.info(`${device.id}: sent ${what}`);
  } catch (e) {
    commandNote(device, e);
  }
}

function key(device, button) {
  if (!has(PRESSES, button)) {
    console.warn(`${device.id}_key: unsupported remote key, dropped`);
    return;
  }
  if (!SEND) {
    console.info(`${device.id}: SEND is false, not sending key ${button}`);
    return;
  }
  const body = '<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
    + 's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
    + '<u:X_SendIRCC xmlns:u="urn:schemas-sony-com:service:IRCC:1">'
    + `<IRCCCode>${PRESSES[button]}</IRCCCode></u:X_SendIRCC></s:Body></s:Envelope>`;
  try {
    const answer = authed(device, '/sony/IRCC', body,
      { 'Content-Type': 'text/xml; charset=UTF-8', SOAPACTION: IRCC_ACTION });
    if (answer.status !== 200) throw failure(`key ${button} answered HTTP ${answer.status}`);
    if (/<(?:[A-Za-z_][\w.-]*:)?Fault(?:\s|>)/.test(answer.body || '')) {
      throw failure(`key ${button} answered with a SOAP fault`);
    }
    commandNote(device, null);
    console.info(`${device.id}: sent key ${button}`);
  } catch (e) {
    commandNote(device, e);
  }
}

openhab.rules.JSRule({
  id: 'sony_ip_control_poll',
  name: 'Sony TVs: read their input, volume and mute, and keep their pairing',
  triggers: [openhab.triggers.GenericCronTrigger(POLL)],
  execute: () => {
    for (const device of DEVICES) {
      read(device);
      showPairing(device);
    }
  },
});

for (const device of DEVICES) {
  const points = ['power', 'input', ...(device.speakerAudio ? ['volume', 'mute'] : []), 'key', 'pairing'];
  openhab.rules.JSRule({
    id: `sony_ip_control_${device.id}_command`,
    name: `${device.id}: send its ${points.join(', ')} commands over IP control`,
    triggers: points.map((point) => openhab.triggers.ItemCommandTrigger(`${device.id}_${point}`)),
    execute: (event) => {
      const point = event.itemName.slice(device.id.length + 1);
      const command = String(event.receivedCommand);
      if (point === 'pairing') {
        pair(device, command);
        return;
      }
      if (point === 'key') {
        key(device, command);
        return;
      }
      const request = requestFor(device, point, command);
      if (request === null) {
        console.warn(`${event.itemName}: unsupported command, dropped`);
        return;
      }
      send(device, request);
      const power = point === 'power' ? readPower(device) : undefined;
      read(device, point === 'volume' || point === 'mute' ? 'sound' : point === 'input' ? 'input' : 'all', power);
    },
  });
}

for (const device of DEVICES) {
  auths.set(device.id, load(device));
  if (checkAuth(device)) update(`${device.id}_problem`, 'UNDEF');
  showPairing(device);
}
