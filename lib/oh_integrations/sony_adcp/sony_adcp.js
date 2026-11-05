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
// values with no ADCP line, are dropped.
//
// The projector answers one request at a time, across controllers too, and
// Control4 may drive it as well. So a read-only copy (SEND false,
// oh_render.read_only), which runs beside the live runtime, connects to
// nothing and only logs what it would send, and nothing probes the port
// (sony_adcp.PROBED).
//
// ADCP is one CRLF-terminated ASCII line per request and one line per
// answer, strictly in turn. The projector greets with NOKEY when its
// authentication is off; one that asks for a password is reported, not
// answered. A VW series projector waking from standby may leave its first
// request unanswered, so every connection starts with `power_status ?`,
// sent once more on a new connection if it gets no answer. openHAB runs one
// rule of a script file at a time, which serializes the connections.
// Each rendered device gets its own context, including its ordered command
// and poll rules. A slow peer cannot hold another device's context.
const DEVICES = null;
const KEYS = null;
const INPUTS = null;
const SEND = true;

const openhab = require('openhab');
let routingLifecycleActor = null;

// THERMITE_BOUNDED_IO

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
  try { thermiteIO.close(link); } catch (_) { /* no resource remains usable */ }
}

function open(device, end) {
  let link;
  try {
    try { link = thermiteIO.open(device.host, device.port, CONNECT_MS, end); }
    catch (_) { throw new Error(`cannot connect to ${device.host}:${device.port}`); }
    const greeting = thermiteIO.line(link, ANSWER_MS);
    if (greeting === 'NOKEY') return link;
    throw new Error(greeting === null ? 'sent no greeting' : 'asks for unsupported ADCP authentication');
  } catch (error) { close(link); throw error; }
}

// One request and its answer; null when none came. A socket that missed an
// answer may still deliver it late, so it answers nothing more.
function ask(link, line, ms) {
  if (link.dead) return null;
  let answer = null;
  try {
    thermiteIO.write(link, `${line}\r\n`);
    answer = thermiteIO.line(link, ms);
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
  const answer = ask(link, line, ANSWER_MS);
  if (answer === 'ok') console.info(`${device.id}: sent ${line}`);
  else console.warn(`${device.id}: ${line} answered ${answer === null ? 'nothing' : answer}`);
  return answer;
}

// The reads after `lines`; on failure {failed, reached}: whether any command
// line was written before it, so a routing executor knows nothing was sent.
// `answers` holds each line's answer (null: none came).
function session(device, lines) {
  if (!SEND) {
    for (const line of lines) console.info(`${device.id}: SEND is false, not sending ${line}`);
    return {failed:true, reached:false, answers:[]};
  }
  let link = null;
  const answers = [];
  const end = thermiteIO.deadline(20000);
  try {
    link = open(device, end);
    let power = ask(link, 'power_status ?', FIRST_MS);
    if (power === null || power === 'err_cmd') {
      close(link);
      link = open(device, end);
      power = ask(link, 'power_status ?', FIRST_MS);
    }
    if (power === null) throw new Error('does not answer');
    for (const line of lines) answers.push(send(device, link, line));
    if (lines.length > 0) power = ask(link, 'power_status ?', ANSWER_MS);
    const status = value(power);
    const input = value(ask(link, 'input ?', ANSWER_MS));
    const mode = status === 'on' ? value(ask(link, 'picture_mode ?', ANSWER_MS)) : 'UNDEF';
    if (down.delete(device.id)) console.info(`${device.id}: answering over ADCP again`);
    show(device, status, input, mode);
    return {power: has(POWER,status) ? POWER[status] : 'UNDEF', input, picture_mode:mode, answers};
  } catch (e) {
    if (!down.has(device.id)) {
      down.add(device.id);
      console.warn(`${device.id}: ${e && e.message ? e.message : e}; its Items are UNDEF until it answers`);
    }
    show(device, 'UNDEF', 'UNDEF', 'UNDEF');
    return {failed:true, reached:answers.length > 0, answers};
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
  id: `sony_adcp_${DEVICES[0].id}_poll`,
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
      if (routingLifecycleActor) {
        routingLifecycleActor.legacy(event.itemName, String(event.receivedCommand), event.itemName.endsWith('_pairing'),
          event.eventSource);
        return;
      }
      return directRoutingCommand(device, event);
    },
  });
}

function directRoutingCommand(device, event) {
  const point = event.itemName.slice(device.id.length + 1);
  const command = String(event.receivedCommand);
  const line = lineFor(point, command);
  if (line === null) {
    console.warn(`${event.itemName}: no ADCP command for '${command}', dropped`);
    return;
  }
  return session(device, [line]);
}

function routingProvider() {
  const device=DEVICES[0];
  function point(resource) { return resource.slice(device.id.length+1); }
  function sample(resource, values) {
    const value = values && values[point(resource)];
    if (!value || value === 'UNDEF') throw Error('native_adcp_status_unavailable');
    return {value, fresh:true, basis:'owned_native_read', observedAt:Date.now()};
  }
  return {
    // Menu navigation can eventually select another input. Treat it as an
    // unknown manual input effect rather than reasserting the old route.
    effects:resource=>point(resource)==='key' ? [`${device.id}_input`] : [],
    oneShot:resource=>point(resource)==='key',
    has:(resource,value)=>resource.startsWith(device.id+'_') && lineFor(point(resource),String(value)) !== null,
    // Connect, greeting and the first power_status precede the command
    // line: their failure sent nothing. err_* is the projector declining it;
    // no answer after the line is the only unknown outcome.
    execute:(resource,value)=>{
      const values=directRoutingCommand(device,{itemName:resource,receivedCommand:value});
      const answer=values && values.answers ? values.answers[0] : undefined;
      if (!values || answer === undefined) return {outcome:'unsent'};
      if (typeof answer === 'string' && answer.startsWith('err_')) return {outcome:'refused'};
      if (answer !== 'ok') return {outcome:'unknown'};
      if (point(resource)==='key') return {outcome:'applied'};
      let observed=null;
      try { observed=sample(resource,values); } catch (_) { /* the actor reads again */ }
      return {outcome:'applied',sample:observed};
    },
    probe:resource=>sample(resource,session(device,[])),
  };
}

// Set only by the routing lifecycle renderer. Legacy configuration keeps
// this device's existing command rule and protocol ownership.
const ROUTING_EXECUTOR = null;
if (ROUTING_EXECUTOR) routingLifecycleActor = require('thermite-routing-io').actor(ROUTING_EXECUTOR, routingProvider(), {readonly: !SEND});
