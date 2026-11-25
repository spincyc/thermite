// Sony BRAVIA TVs over Sony's IP control, with its "Normal" authentication
// (lib/oh_integrations/sony_ip_control).
//
// DEVICES lists each TV openHAB controls: its id (its Items are <id>_power,
// which sony_rest's HTTP Thing reads as well, <id>_input, <id>_volume,
// <id>_mute, <id>_key, <id>_pairing, <id>_pairing_status and <id>_problem),
// the address of its web server, the numbers of its HDMI inputs and its
// speaker's own volume scale ({min, max, step}). speakerAudio false omits
// the TV's volume/mute Items, commands and reads; it does not affect the
// room's separate audio device.
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
//   or an unusable registration clears all three readings to UNDEF. Every
//   reading is posted, changed or not: each is an Item update that keeps it
//   fresh for the room policy worker, which counts one older than 30 s as
//   unknown (thermiteDevice.reading). <id>_problem and <id>_pairing_status,
//   the rule's own state, are posted only when they change
//   (thermiteDevice.status).
// - A command to a TV's power, input, volume or mute is sent (setPowerStatus,
//   setPlayContent, setAudioVolume on the TV's own scale, setAudioMute), then
//   read back at once; a remote button id sent to <id>_key is sent as its
//   IRCC code (X_SendIRCC). Anything else is dropped.
// - Under the routing lifecycle (routingProvider) a TV that has just come
//   on may still be booting its content API (avContent): for its power
//   field's declared settle (sony_ip_control.fields()) after a power read
//   showed it come on, a power read of ON while avContent answers a Sony
//   error is a transition toward ON, so the planner waits for it before it
//   reads or sets the input, and an input setPlayContent declines with a
//   Sony error then is busy: the planner sends it again a tenth of that
//   settle later.
//
// A TV that is not paired, refuses its registration or does not answer a
// read is logged once, until that changes, and <id>_problem holds the code
// of that problem (oh_integrations/_commands.py; UNDEF while there is
// none), whose words the remotes show. Each command ends with one line of
// the command record (thermiteDevice.commandRule), which sets that Item to
// the code of a command the TV did not take until it takes one; why it was
// not taken is logged before it, once while it stays the same. What the
// reads meet is in the rule's tally as well, for the metrics
// (thermiteDevice.problem): only while the TV is on and read, or has a
// problem, since a TV in standby is asked nothing. With SEND false
// (oh_render.read_only) the rule still reads, pairs and renews nothing, and
// logs what it would send.
// Each rendered device gets its own context, including its ordered command
// and poll rules. A slow peer cannot hold another device's context.
const DEVICES = null;
const PRESSES = null;
const SEND = true;

const openhab = require('openhab');
let routingLifecycleActor = null;
// THERMITE_BOUNDED_IO
// THERMITE_DEVICE
const { has, reading, status, withheld } = thermiteDevice;

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

// getPowerStatus -> the power Switch.
const POWER = { active: 'ON', standby: 'OFF' };
const NOT_PAIRED = 'not paired: pair it on the Pairing page (on the TV: Settings, Network, Home network, '
  + 'IP control: Authentication Normal, Remote Start on)';
const REFUSED = 'refused its registration: pair it again on the Pairing page';
const IRCC_ACTION = '"urn:schemas-sony-com:service:IRCC:1#X_SendIRCC"';
const BASE64 = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/';

// TV id -> the registration or read problem last logged for it.
const problems = new Map();
// TV id -> unresolved input/sound read failures {message, detail, code}. A
// successful command or partial read does not establish that the other
// reading has recovered.
const readProblems = new Map();
// TV id -> why its last command failed, as last logged: a held key against
// a TV that stays away says why once. A later poll answers a different
// question from whether a command worked, so this, like the command's code
// in <id>_problem, lasts until a command is accepted.
const commandFailures = new Map();
// TV id -> its registration {clientid, nickname, pin, cookie, expires,
// refused}, or null.
const auths = new Map();
// TV id -> {asked, clientid} while its PIN is on screen, or {until, state}
// while a refused PIN or no answer shows.
const pairings = new Map();
// TV id -> when its cookie was last renewed or tried.
const renewed = new Map();
// TV id -> its last power read, and when a power read showed it come on.
const powers = new Map();
const woke = new Map();

// A String or Switch Item's state; null for NULL, UNDEF or missing Item.
function reported(name) {
  const item = openhab.items.getItem(name, true);
  const state = item === null ? 'NULL' : String(item.state);
  return state === 'NULL' || state === 'UNDEF' ? null : state;
}

// An Error whose message says a TV's problem in the log, with a detail.
// Its outcome, for a command: `unsent` when no request byte reached the TV,
// `refused` when the TV answered declining, else `unknown`. Its code is the
// problem's in <id>_problem and the command record
// (oh_integrations/_commands.py): an answer the rule cannot use is
// `protocol` unless said otherwise.
function failure(problem, detail, outcome = 'unknown', code = 'protocol') {
  const error = new Error(problem);
  error.detail = detail || '';
  error.outcome = outcome;
  error.code = code;
  return error;
}

// Logs a TV's registration problem when it is new, or with none (problem
// null) its first unresolved read's, and its end (said as ended), and keeps
// its code as what the TV's reads meet (thermiteDevice.problem).
function note(device, problem, detail, ended) {
  let code = problem === null ? null : 'auth';
  if (problem === null) {
    const unresolved = readProblems.get(device.id);
    if (unresolved && unresolved.size) {
      const reading = unresolved.values().next().value;
      problem = reading.message;
      detail = reading.detail;
      code = reading.code;
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
  thermiteDevice.problem.read(device.id, code);
}

// Logs why a command failed when that is new; an accepted one (error null)
// ends it.
function commandNote(device, error) {
  const said = error ? error.message : null;
  if (said === (commandFailures.has(device.id) ? commandFailures.get(device.id) : null)) return;
  if (said === null) commandFailures.delete(device.id);
  else {
    commandFailures.set(device.id, said);
    console.warn(`${device.id}: ${said}${error.detail ? ` (${error.detail})` : ''}`);
  }
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
  let response;
  try {
    response = thermiteIO.http(`http://${device.address}${path}`, 'POST', body, headers, ANSWER_MS, CONNECT_MS);
  } catch (e) {
    // No connection sent nothing; one lost after it opened may have.
    if (thermiteIO.unreached(e)) throw failure('does not answer', 'no connection', 'unsent', 'unreachable');
    throw failure('does not answer', 'connected, then no complete answer in time', 'unknown', 'no_answer');
  }
  const list = response.getHeaders().getValuesList('set-cookie');
  const cookies = [];
  for (let i = 0; i < list.size(); i += 1) cookies.push(String(list.get(i)));
  return { status: Number(response.getStatus()), body: String(response.getContentAsString()), cookies };
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
  if (!auth || !auth.cookie) throw failure(NOT_PAIRED, null, 'unsent', 'auth');
  if (auth.refused) throw failure(REFUSED, null, 'unsent', 'auth');
  const last = renewed.get(device.id) || 0;
  if (Date.now() > auth.expires - RENEW_MS && Date.now() - last >= RETRY_MS) {
    renew(device, auth.expires > Date.now() ? 'it runs out soon' : 'it ran out');
  }
  const now = auths.get(device.id);
  if (now.refused) throw failure(REFUSED, null, 'unsent', 'auth');
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
        `HTTP ${answer.status}`, 'refused', 'auth');
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
  if (answer.status !== 200) {
    const declined = answer.status >= 400 && answer.status < 500;
    throw failure(`answered ${method} with HTTP ${answer.status}`, null, declined ? 'refused' : 'unknown', declined ? 'refused' : 'protocol');
  }
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
  if (last === NOT_PAIRED || last === REFUSED) note(device, null, '', 'is paired, so it is sent commands again');
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
    // A JSON-RPC error is the TV declining the request; in standby, it is
    // not ready for it.
    const error = failure(`answered ${method} with ${code === 1 ? 'Sony error 1' : 'a Sony API error'}`, null, 'refused',
      code === DISPLAY_OFF ? 'not_ready' : 'refused');
    error.displayOff = code === DISPLAY_OFF;
    error.api = true;
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
function soundOf(device, answer) {
  const result = readResult(answer, 'getVolumeInformation');
  const targets = (Array.isArray(result) && Array.isArray(result[0]) && result[0]) || [];
  const speaker = targets.find((t) => t && t.target === 'speaker') || targets[0];
  const volume = speaker && speaker.volume;
  const numeric = typeof volume === 'number' || typeof volume === 'string' && /^\d+(\.\d+)?$/.test(volume);
  if (!speaker || typeof speaker.target !== 'string' || !speaker.target || !numeric
    || !Number.isFinite(Number(volume)) || Number(volume) < device.volume.min || Number(volume) > device.volume.max
    || typeof speaker.mute !== 'boolean') {
    throw failure('answered getVolumeInformation without valid readback');
  }
  return [String(Number(speaker.volume)), speaker.mute === true ? 'ON' : 'OFF'];
}

function off(device) {
  const points = device.speakerAudio ? ['input', 'volume', 'mute'] : ['input'];
  for (const point of points) reading(`${device.id}_${point}`, 'UNDEF');
}

// Track power transitions from both direct reads and sony_rest Item changes
// so the content API boot guard fires after normal polls and external wakes.
function notePower(device, power, oldState) {
  const prev = powers.has(device.id) ? powers.get(device.id) : oldState;
  if (power === 'ON' && prev === 'OFF') woke.set(device.id, Date.now());
  if (power === 'ON' || power === 'OFF') powers.set(device.id, power);
}

// Reads what the TV shows; a TV that is off, or not paired, is asked nothing.
function read(device, what, freshPower, observed) {
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
    // Asked nothing: the tally must not go on saying that its reads go well
    // (a problem they met, or its pairing's, stands).
    thermiteDevice.problem.unread(device.id);
    return;
  }
  if (!checkAuth(device)) {
    readProblems.delete(device.id);
    off(device);
    return;
  }
  const failures = readProblems.get(device.id) || new Map();
  if (what !== 'sound') {
    if (observed) observed.inputRead = true;
    try {
      const value = inputOf(call(device, 'avContent', 'getPlayingContentInfo', []));
      reading(`${device.id}_input`, value);
      if (observed) { observed.input = value; observed.contentBooting = false; }
      failures.delete('input');
    } catch (e) {
      // Content and power are polled independently. During shutdown the
      // content API can report standby before the power Item catches up.
      // Confirm it directly before invalidating the selected input; a
      // standby error alone never manufactures an Off state.
      if (e.displayOff && freshPower === undefined) {
        const current = readPower(device);
        if (observed) observed.power = current;
        if (current === 'OFF') {
          read(device, what, 'OFF', observed);
          return;
        }
      }
      if (observed && e.api === true) observed.contentBooting = true;
      failures.set('input', { message: e.message, detail: e.detail, code: e.code });
      reading(`${device.id}_input`, 'UNDEF');
    }
  }
  if (device.speakerAudio && what !== 'input') {
    try {
      const [volume, mute] = soundOf(device, call(device, 'audio', 'getVolumeInformation', []));
      reading(`${device.id}_volume`, volume);
      reading(`${device.id}_mute`, mute);
      failures.delete('sound');
    } catch (e) {
      if (e.displayOff && freshPower === undefined) {
        const current = readPower(device);
        if (observed) observed.power = current;
        if (current === 'OFF') {
          read(device, what, 'OFF', observed);
          return;
        }
      }
      failures.set('sound', { message: e.message, detail: e.detail, code: e.code });
      reading(`${device.id}_volume`, 'UNDEF');
      reading(`${device.id}_mute`, 'UNDEF');
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
function normalizePower(answered, field) {
  if (field && field.reports) {
    return has(field.reports, answered) ? thermiteDevice.normalizeReport(field, answered).value : null;
  }
  return has(POWER, answered) ? POWER[answered] : null;
}

function readPower(device, field) {
  let power = null;
  try {
    const answer = post(device, '/sony/system', json('getPowerStatus', []), { 'Content-Type': 'application/json' });
    const parsed = answer.status === 200 ? JSON.parse(answer.body) : {};
    const answered = Array.isArray(parsed.result) && parsed.result[0] ? String(parsed.result[0].status) : '';
    power = normalizePower(answered, field);
  } catch (e) { /* Leave an unanswered or malformed power report unknown. */ }
  reading(`${device.id}_power`, power || 'UNDEF');
  notePower(device, power);
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
  status(`${device.id}_pairing_status`, pairingState(device));
}

function shown(device, state, ms, error) {
  pairings.set(device.id, { until: Date.now() + ms, state, error });
}

function ask(device) {
  if (withheld(device, 'asking it for a PIN')) return;
  const auth = auths.get(device.id);
  const clientid = auth ? auth.clientid : `${CLIENT_PREFIX}:${String(UUID.randomUUID())}`;
  let answer;
  try {
    answer = register(device, clientid, null);
  } catch (e) {
    console.warn(`${device.id}: does not answer, so it was not asked for a PIN (${e.detail})`);
    shown(device, 'unreachable', FAILED_MS);
    return e.outcome === 'unsent' ? null : false; // null: nothing reached the TV.
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
  return true; // A complete native registration response, including refusal.
}

function sendPin(device, pin) {
  if (withheld(device, 'sending the PIN')) return;
  const pairing = pairings.get(device.id);
  const auth = auths.get(device.id);
  const clientid = pairing && pairing.clientid ? pairing.clientid
    : auth ? auth.clientid : `${CLIENT_PREFIX}:${String(UUID.randomUUID())}`;
  status(`${device.id}_pairing_status`, 'checking');
  let answer;
  try {
    answer = register(device, clientid, pin);
  } catch (e) {
    console.warn(`${device.id}: does not answer, so the PIN was not taken (${e.detail})`);
    shown(device, 'unreachable', FAILED_MS);
    return e.outcome === 'unsent' ? null : false; // null: nothing reached the TV.
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
  return true;
}

// Whether the TV answered the exchange (true, even refusing the PIN), did
// not (false), or was sent nothing: it was not reached (null), or the
// command was withheld or is none (undefined).
function pair(device, command) {
  let accepted;
  if (command === REQUEST) accepted = ask(device);
  else if (PIN.test(command)) accepted = sendPin(device, command);
  else console.warn(`${device.id}_pairing: a command that is neither ${REQUEST} nor four digits, dropped`);
  showPairing(device);
  // The status can be unchanged, or pass through checking between two REST
  // reads. A unique result acknowledges this command without retaining its PIN.
  const result = { id: String(UUID.randomUUID()), state: pairingState(device) };
  const pairing = pairings.get(device.id);
  if (pairing && pairing.error) result.error = pairing.error;
  status(`${device.id}_pairing_result`, JSON.stringify(result));
  return accepted;
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
  if (device.speakerAudio && point === 'volume' && thermiteDevice.onScale(command, device.volume)) {
    return ['audio', 'setAudioVolume', [{ target: 'speaker', volume: String(Number(command)) }]];
  }
  const match = point === 'input' ? INPUT.exec(command) : null;
  if (match && device.inputs.includes(Number(match[1]))) {
    return ['avContent', 'setPlayContent', [{ uri: `extInput:hdmi?port=${Number(match[1])}` }]];
  }
  return null;
}

// How the command ended, for the command record and the routing executor:
// {outcome, code} (oh_integrations/_commands.py), the outcome applied,
// dropped (by a read-only copy), unsent, refused or unknown. `declined`,
// when given, learns whether a refusal was a Sony API error (api).
function send(device, request, declined) {
  const [service, method, params, version] = request;
  const what = `${method} ${JSON.stringify(params)}`;
  if (withheld(device, `sending ${what}`)) return { outcome: 'dropped', code: 'read_only' };
  try {
    const answer = call(device, service, method, params, version);
    if (Array.isArray(answer.error) && answer.error[0] === DISPLAY_OFF) {
      throw Object.assign(failure(`cannot perform ${method}: the TV is in standby`, null, 'refused', 'not_ready'), { api: true });
    }
    if (!Array.isArray(readResult(answer, method))) throw failure(`answered ${method} without valid acknowledgement`);
    commandNote(device, null);
    console.info(`${device.id}: sent ${what}`);
    return { outcome: 'applied' };
  } catch (e) {
    commandNote(device, e);
    if (declined) declined.api = e.api === true;
    return { outcome: e.outcome || 'unknown', code: e.code || 'other' };
  }
}

function key(device, button) {
  if (!has(PRESSES, button)) {
    console.warn(`${device.id}_key: unsupported remote key, dropped`);
    return { outcome: 'dropped', code: 'invalid' };
  }
  if (withheld(device, `sending key ${button}`)) return { outcome: 'dropped', code: 'read_only' };
  const body = '<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
    + 's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
    + '<u:X_SendIRCC xmlns:u="urn:schemas-sony-com:service:IRCC:1">'
    + `<IRCCCode>${PRESSES[button]}</IRCCCode></u:X_SendIRCC></s:Body></s:Envelope>`;
  try {
    const answer = authed(device, '/sony/IRCC', body,
      { 'Content-Type': 'text/xml; charset=UTF-8', SOAPACTION: IRCC_ACTION });
    if (answer.status !== 200) {
      const declined = answer.status >= 400 && answer.status < 500;
      throw failure(`key ${button} answered HTTP ${answer.status}`, null, declined ? 'refused' : 'unknown', declined ? 'refused' : 'protocol');
    }
    if (/<(?:[A-Za-z_][\w.-]*:)?Fault(?:\s|>)/.test(answer.body || '')) {
      throw failure(`key ${button} answered with a SOAP fault`, null, 'refused', 'refused');
    }
    commandNote(device, null);
    console.info(`${device.id}: sent key ${button}`);
    return { outcome: 'applied' };
  } catch (e) {
    commandNote(device, e);
    return { outcome: e.outcome || 'unknown', code: e.code || 'other' };
  }
}

openhab.rules.JSRule({
  id: `sony_ip_control_${DEVICES[0].id}_poll`,
  name: 'Sony TVs: read their input, volume and mute, and keep their pairing',
  triggers: [openhab.triggers.GenericCronTrigger(POLL)],
  execute: () => {
    for (const device of DEVICES) {
      read(device);
      showPairing(device);
    }
  },
});

// The registrations first: the command rule then shows <id>_problem as it
// stands (thermiteDevice.recorder), a TV that is not paired included.
for (const device of DEVICES) {
  auths.set(device.id, load(device));
  checkAuth(device);
  showPairing(device);
  const initialPower = reported(`${device.id}_power`);
  if (initialPower === 'ON' || initialPower === 'OFF') powers.set(device.id, initialPower);
}

for (const device of DEVICES) {
  const points = ['power', 'input', ...(device.speakerAudio ? ['volume', 'mute'] : []), 'key', 'pairing'];
  thermiteDevice.commandRule({
    id: `sony_ip_control_${device.id}_command`,
    name: `${device.id}: send its ${points.join(', ')} commands over IP control`,
    device: device.id,
    items: points.map((point) => `${device.id}_${point}`),
    actor: () => routingLifecycleActor,
    direct: (event) => directRoutingCommand(device, event),
  });
  if (typeof openhab.triggers.ItemStateChangeTrigger === 'function') {
    openhab.rules.JSRule({
      id: `sony_ip_control_${device.id}_power_change`,
      name: `${device.id}: note power transitions for content API boot guard`,
      triggers: [openhab.triggers.ItemStateChangeTrigger(`${device.id}_power`)],
      execute: (event) => {
        const oldState = event && event.oldState !== undefined && event.oldState !== null ? String(event.oldState) : undefined;
        const newState = event && event.newState !== undefined && event.newState !== null ? String(event.newState) : reported(`${device.id}_power`);
        notePower(device, newState, oldState === 'ON' || oldState === 'OFF' ? oldState : undefined);
      },
    });
  }
}

// How the command ended (see send). A pairing exchange the TV answered,
// even refusing the PIN, is complete; one it never answered is unknown.
function directRoutingCommand(device, event, declined, observed) {
  const point = event.itemName.slice(device.id.length + 1);
  const command = String(event.receivedCommand);
  if (point === 'pairing') {
    const answered = pair(device, command);
    return answered === true ? { outcome: 'applied' }
      : answered === false ? { outcome: 'unknown', code: 'no_answer' }
      : answered === null ? { outcome: 'unsent', code: 'unreachable' }
      : { outcome: 'dropped', code: command === REQUEST || PIN.test(command) ? 'read_only' : 'invalid' };
  }
  if (point === 'key') return key(device, command);
  const request = requestFor(device, point, command);
  if (request === null) {
    console.warn(`${event.itemName}: unsupported command, dropped`);
    return { outcome: 'dropped', code: 'invalid' };
  }
  const done = send(device, request, declined);
  const power = point === 'power' ? readPower(device) : undefined;
  if (observed && point === 'power') observed.power = power;
  const readKind = point === 'volume' || point === 'mute' ? 'sound'
    : point === 'input' || (observed && point === 'power') ? 'input' : 'all';
  read(device, readKind, power, observed);
  return done;
}

// fields: the actor's fields (ROUTING_EXECUTOR.fields), for the power
// field's declared settle.
function routingProvider(fields = {}) {
  const device = DEVICES[0];
  function point(resource) { return resource.slice(device.id.length + 1); }
  // How long after a power read showed the TV come on its content API may
  // still boot: its power field's declared settle toward ON (see the header).
  const power = fields[`${device.id}_power`];
  const bootMs = power && power.settle && Number.isFinite(power.settle.ON) ? power.settle.ON : 0;
  function booting() {
    return woke.has(device.id) && Date.now() - woke.get(device.id) < bootMs;
  }
  // Whether the content API answers, or answers that the screen shows no
  // HDMI input (it has booted); any other Sony error is it still booting.
  function contentBooting() {
    let answer;
    try { answer = call(device, 'avContent', 'getPlayingContentInfo', []); } catch (_) { return false; }
    return !!answer && Array.isArray(answer.error) && answer.error[0] !== ILLEGAL_STATE;
  }
  function probe(resource) {
    const what = point(resource);
    const field = fields[resource];
    const raw = what === 'power' ? readPower(device, field) : what === 'input' ?
      inputOf(call(device, 'avContent', 'getPlayingContentInfo', [])) : null;
    if (raw === null) throw Error('native_sony_status_unavailable');
    const normalized = what === 'input' && field && field.reports
      ? thermiteDevice.normalizeReport(field, raw) : { value: raw, transition: false };
    reading(resource, normalized.value);
    const read = thermiteDevice.sample(normalized.value);
    if (normalized.transition || (what === 'power' && normalized.value === 'ON' && booting() && contentBooting())) {
      read.transition = true;
    }
    return read;
  }
  return {
    // Home/TV/app/input keys and menu navigation may change the selected
    // feed. Preserve manual intent without guessing their eventual value.
    effects: resource => point(resource) === 'key' ? [`${device.id}_power`, `${device.id}_input`] : [],
    oneShot: resource => point(resource) === 'key' || point(resource) === 'pairing',
    has: (resource, value) => resource.startsWith(device.id + '_') &&
      (point(resource) === 'pairing' ? value === REQUEST || PIN.test(value) :
       point(resource) === 'key' ? has(PRESSES,value) : requestFor(device,point(resource),String(value)) !== null),
    // Pairing state, local guards and a refused connection precede any
    // request byte; a JSON-RPC error, standby or 4xx answer is the TV
    // declining. Only a request without a usable answer stays unknown.
    // An input it declines with a Sony error while it boots is busy (see the header).
    execute: (resource, value) => {
      const declined = {};
      const observed = {};
      const what = point(resource);
      const done = directRoutingCommand(device, { itemName: resource, receivedCommand: value }, declined, observed);
      if (done.outcome === 'refused' && what === 'input' && declined.api && booting()
        && (observed.power !== undefined ? observed.power : readPower(device)) === 'ON') {
        return { outcome: 'busy', code: 'busy', retryAfterMs: Math.ceil(bootMs / 10) };
      }
      // A command the rule dropped itself sent nothing.
      if (done.outcome === 'dropped') return { outcome: 'unsent', code: done.code };
      if (done.outcome !== 'applied') return done;
      if (what === 'power') {
        if (observed.power === null || observed.power === undefined) return { outcome: 'applied', sample: null };
        const sample = thermiteDevice.sample(observed.power);
        if (observed.power === 'ON' && booting() && observed.contentBooting === true) sample.transition = true;
        return { outcome: 'applied', sample };
      }
      if (what === 'input') {
        if (observed.inputRead) return { outcome: 'applied', sample: observed.input ? thermiteDevice.sample(observed.input) : null };
        return thermiteDevice.applied(() => probe(resource));
      }
      return { outcome: 'applied' };
    },
    probe,
  };
}

// Set only by the routing lifecycle renderer. Legacy configuration keeps
// this device's existing command rule and protocol ownership.
const ROUTING_EXECUTOR = null;
if (ROUTING_EXECUTOR) routingLifecycleActor = require('thermite-routing-io').actor(ROUTING_EXECUTOR, routingProvider(ROUTING_EXECUTOR.fields),
  {readonly: !SEND, record: thermiteDevice.recorder(DEVICES[0].id)});
