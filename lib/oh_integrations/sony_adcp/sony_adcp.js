// Sony projectors over ADCP (lib/oh_integrations/sony_adcp).
//
// DEVICES lists each projector: its id (its Items are <id>_power,
// <id>_power_state, <id>_input, <id>_picture_mode and, under control,
// <id>_key), the host and port of its ADCP server, and whether openHAB
// controls it. Every 15 s the rule opens one connection per projector,
// reads power_status, input and, while it is on, picture_mode, and posts
// them; a projector that does not answer gets UNDEF. A command to a
// controlled projector's power, input or key Item is sent on a connection of
// its own, followed by the same reads. Commands to anything else, and
// values with no ADCP line, are dropped. With SEND false
// (oh_render.read_only) the rule still reads, and logs what it would send.
//
// ADCP is one CRLF-terminated ASCII line per request and one line per
// answer, strictly in turn. The projector greets with NOKEY when its
// authentication is off; one that asks for a password is reported, not
// answered. A VW series projector waking from standby may leave its first
// request unanswered, so every connection starts with `power_status ?`,
// sent once more on a new connection if it gets no answer. openHAB runs one
// rule of a script file at a time, which serializes the connections.
const DEVICES = null;
const KEYS = null;
const INPUTS = null;
const SEND = true;

const openhab = require('openhab');

const Socket = Java.type('java.net.Socket');
const InetSocketAddress = Java.type('java.net.InetSocketAddress');
const BufferedReader = Java.type('java.io.BufferedReader');
const InputStreamReader = Java.type('java.io.InputStreamReader');
const OutputStreamWriter = Java.type('java.io.OutputStreamWriter');
const StandardCharsets = Java.type('java.nio.charset.StandardCharsets');

const POLL = '0/15 * * * * ?';
const CONNECT_MS = 2000;
const ANSWER_MS = 3000;
// The first answer on a connection, which may wait for the projector to wake.
const FIRST_MS = 5000;
// power_status -> the power Switch.
const POWER = { standby: 'OFF', startup: 'ON', on: 'ON', cooling1: 'OFF', cooling2: 'OFF' };
// Projector ids that did not answer their last session: logged once.
const down = new Set();

function has(object, key) {
  return Object.prototype.hasOwnProperty.call(object, key);
}

function close(link) {
  try {
    link.socket.close();
  } catch (e) {
    // Closing a broken socket has nothing left to report.
  }
}

function readLine(link) {
  try {
    const line = link.reader.readLine();
    return line === null ? null : String(line);
  } catch (e) {
    return null;
  }
}

function open(device) {
  const socket = new Socket();
  const link = { socket, dead: false };
  try {
    socket.connect(new InetSocketAddress(device.host, device.port), CONNECT_MS);
    socket.setSoTimeout(ANSWER_MS);
    link.reader = new BufferedReader(new InputStreamReader(socket.getInputStream(), StandardCharsets.US_ASCII));
    link.writer = new OutputStreamWriter(socket.getOutputStream(), StandardCharsets.US_ASCII);
  } catch (e) {
    close(link);
    throw new Error(`cannot connect to ${device.host}:${device.port} (${e})`);
  }
  const greeting = readLine(link);
  if (greeting === 'NOKEY') return link;
  close(link);
  throw new Error(greeting === null ? 'sent no greeting'
    : `asks for a password (greeting '${greeting}'); ADCP authentication is not supported`);
}

// One request and its answer; null when none came. A socket that missed an
// answer may still deliver it late, so it answers nothing more.
function ask(link, line, ms) {
  if (link.dead) return null;
  let answer = null;
  try {
    link.socket.setSoTimeout(ms);
    link.writer.write(`${line}\r\n`);
    link.writer.flush();
    answer = readLine(link);
  } catch (e) {
    answer = null;
  }
  if (answer === null) link.dead = true;
  return answer;
}

// A query's answer as an Item state: the quoted value, else UNDEF.
function value(answer) {
  if (answer === null || answer === 'ok' || answer.startsWith('err_')) return 'UNDEF';
  try {
    const parsed = JSON.parse(answer);
    return typeof parsed === 'string' || typeof parsed === 'number' ? String(parsed) : 'UNDEF';
  } catch (e) {
    return 'UNDEF';
  }
}

function update(name, state) {
  const item = openhab.items.getItem(name);
  if (String(item.state) !== state) item.postUpdate(state);
}

function show(device, status, input, mode) {
  update(`${device.id}_power_state`, status);
  update(`${device.id}_power`, has(POWER, status) ? POWER[status] : 'UNDEF');
  update(`${device.id}_input`, input);
  update(`${device.id}_picture_mode`, mode);
}

function send(device, link, line) {
  if (!SEND) {
    console.info(`${device.id}: SEND is false, not sending ${line}`);
    return;
  }
  const answer = ask(link, line, ANSWER_MS);
  if (answer === 'ok') console.info(`${device.id}: sent ${line}`);
  else console.warn(`${device.id}: ${line} answered ${answer === null ? 'nothing' : answer}`);
}

function session(device, lines) {
  let link = null;
  try {
    link = open(device);
    let power = ask(link, 'power_status ?', FIRST_MS);
    if (power === null || power === 'err_cmd') {
      close(link);
      link = open(device);
      power = ask(link, 'power_status ?', FIRST_MS);
    }
    if (power === null) throw new Error('does not answer');
    for (const line of lines) send(device, link, line);
    if (lines.length > 0 && SEND) power = ask(link, 'power_status ?', ANSWER_MS);
    const status = value(power);
    const input = value(ask(link, 'input ?', ANSWER_MS));
    const mode = status === 'on' ? value(ask(link, 'picture_mode ?', ANSWER_MS)) : 'UNDEF';
    if (down.delete(device.id)) console.info(`${device.id}: answering over ADCP again`);
    show(device, status, input, mode);
  } catch (e) {
    if (!down.has(device.id)) {
      down.add(device.id);
      console.warn(`${device.id}: ${e && e.message ? e.message : e}; its Items are UNDEF until it answers`);
    }
    show(device, 'UNDEF', 'UNDEF', 'UNDEF');
  } finally {
    if (link) close(link);
  }
}

// The ADCP line for a command to one of a projector's Items, or null.
function lineFor(point, command) {
  if (point === 'power' && (command === 'ON' || command === 'OFF')) return `power "${command.toLowerCase()}"`;
  if (point === 'input' && INPUTS.includes(command)) return `input "${command}"`;
  if (point === 'key' && has(KEYS, command)) return `key "${KEYS[command]}"`;
  return null;
}

openhab.rules.JSRule({
  id: 'sony_adcp_poll',
  name: 'Projectors: read their state over ADCP',
  triggers: [openhab.triggers.GenericCronTrigger(POLL)],
  execute: () => {
    for (const device of DEVICES) session(device, []);
  },
});

for (const device of DEVICES.filter((d) => d.control)) {
  openhab.rules.JSRule({
    id: `sony_adcp_${device.id}_command`,
    name: `${device.id}: send its power, input and key commands over ADCP`,
    triggers: ['power', 'input', 'key'].map((point) => openhab.triggers.ItemCommandTrigger(`${device.id}_${point}`)),
    execute: (event) => {
      const point = event.itemName.slice(device.id.length + 1);
      const command = String(event.receivedCommand);
      const line = lineFor(point, command);
      if (line === null) {
        console.warn(`${event.itemName}: no ADCP command for '${command}', dropped`);
        return;
      }
      session(device, [line]);
    },
  });
}
