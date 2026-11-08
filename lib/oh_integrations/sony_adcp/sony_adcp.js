// Sony projectors over ADCP (lib/oh_integrations/sony_adcp).
//
// DEVICES lists each projector: its id (its Items are <id>_power,
// <id>_power_state, <id>_input, <id>_picture_mode and, under control,
// <id>_key), the host and port of its ADCP server, and whether openHAB
// controls it. The rule keeps one ADCP connection open to each projector,
// and every 15 s reads power_status, input and, while it is on,
// picture_mode on it and posts them, changed or not (thermiteDevice.reading).
// A command to a controlled projector's power, input or key Item is sent on
// that connection, followed by the same reads. Commands to anything else,
// and values with no ADCP line, are dropped.
//
// A connection the projector closed while idle (its connection time-out, a
// standby, a power cycle) is replaced within the same session, as is one
// that leaves the session's first request unanswered (a power cycle resets
// it; see also waking below). A projector that does not answer a request
// on a new connection either, or closes one while answering, gets UNDEF and
// a new connection: at the next poll, then after 2 and at most RETRY_POLLS
// polls while it keeps failing (thermiteDevice.health). A command or a route
// probe connects at once. The script closes its connections when it unloads
// (thermiteDevice.links).
//
// The projector answers one request at a time, across controllers too, and
// Control4 may drive it as well. So a read-only copy (SEND false,
// oh_render.read_only), which runs beside the live runtime, connects to
// nothing and only logs what it would send, and nothing probes the port
// (sony_adcp.PROBED).
//
// ADCP is one CRLF-terminated ASCII line per request and one line per
// answer, strictly in turn. The projector greets each connection once, with
// NOKEY when its authentication is off; one that asks for a password is
// reported, not answered. A projector waking from low-power standby may
// answer its first request err_cmd once, or a VW series one nothing for up
// to 10 s, so every session starts with `power_status ?`, asked again on the
// same connection after err_cmd and on a new one after no answer. A
// connection that missed an answer may still deliver it late, so it is
// closed and never asked again. openHAB runs one rule of a script file at a
// time, which serializes each connection's use.
//
// Under the routing lifecycle (routingProvider) a power read while the
// projector starts up or cools down (MOVING) is a transition sample toward
// ON or OFF: the planner neither trusts it nor writes the input or power
// until it settles, and an accepted power command the projector has not
// begun yet is waited out (its settle in sony_adcp.fields()). A power
// command it declines (err_inactive to an on while it cools) or ignores
// while it moves the other way, or an input it declines while it starts
// up, is busy: the planner sends it again after a tenth of that settle.
// Each rendered device gets its own context, including its ordered command
// and poll rules. A slow peer cannot hold another device's context.
const DEVICES = null;
const KEYS = null;
const INPUTS = null;
const SEND = true;

const openhab = require('openhab');
let routingLifecycleActor = null;

// THERMITE_BOUNDED_IO
// THERMITE_DEVICE
const { has, reading, withheld, close } = thermiteDevice;

// Well inside the projector's connection time-out (60 s by default), so a
// kept connection is not closed for idling.
const POLL = '0/15 * * * * ?';
// A failing projector is tried at most every this many polls.
const RETRY_POLLS = 4;
const CONNECT_MS = 2000;
const ANSWER_MS = 3000;
// The first answer of a session, which may wait for the projector to wake.
const FIRST_MS = 5000;
// The deadline of each use of a connection: its reads and any command.
const SESSION_MS = 20000;
// power_status -> the power Switch.
const POWER = { standby: 'OFF', startup: 'ON', on: 'ON', cooling1: 'OFF', cooling2: 'OFF' };
// The power_status values on the way to their Switch value. An input needs
// the projector on, and power on is refused while it cools (err_inactive).
const MOVING = { startup: true, cooling1: true, cooling2: true };
// Projectors that stopped answering, logged once, and their backoff.
const health = thermiteDevice.health(RETRY_POLLS);

// A new connection, past its greeting.
function open(device, end) {
  let link;
  try {
    try { link = thermiteIO.open(device.host, device.port, CONNECT_MS, end); }
    catch (_) { throw new Error(`cannot connect to ${device.host}:${device.port}`); }
    let greeting = null;
    try { greeting = thermiteIO.line(link, ANSWER_MS); } catch (_) { /* none came */ }
    if (greeting === 'NOKEY') return link;
    throw new Error(greeting === null ? 'sent no greeting' : 'asks for unsupported ADCP authentication');
  } catch (error) { close(link); throw error; }
}

// Each projector's kept connection. ADCP sends nothing unasked, so
// anything that came while it was idle is dropped before a session.
const links = thermiteDevice.links(open);

// One request and its answer; null when none came. The connection is then
// closed, with why in link.why: it may still deliver the answer late.
function ask(link, line, ms) {
  let answer = null;
  try {
    thermiteIO.write(link, `${line}\r\n`);
    answer = thermiteIO.line(link, ms);
  } catch (error) {
    // Only a read that timed out leaves it open: a reset is a close.
    if (!thermiteIO.quietTimeout(link, error)) close(link);
  }
  if (answer === null) {
    link.why = link.closed ? 'closed the connection' : `does not answer ${line}`;
    close(link);
  }
  return answer;
}

// The answer to a request the session cannot do without.
function needed(link, line, ms) {
  const answer = ask(link, line, ms);
  if (answer === null) throw new Error(link.why);
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

function show(device, status, input, mode) {
  reading(`${device.id}_power_state`, status);
  reading(`${device.id}_power`, has(POWER, status) ? POWER[status] : 'UNDEF');
  reading(`${device.id}_input`, input);
  reading(`${device.id}_picture_mode`, mode);
}

function send(device, link, line) {
  const answer = ask(link, line, ANSWER_MS);
  if (answer === 'ok') console.info(`${device.id}: sent ${line}`);
  else console.warn(`${device.id}: ${line} answered ${answer === null ? 'nothing' : answer}`);
  return answer;
}

// The session's first request, power_status, on the kept connection or a
// new one. No answer means the kept one was lost while idle (a power cycle
// resets it) or a waking projector stayed silent: once more on a new
// connection. err_cmd from a waking projector is asked again on the same
// one. Nothing precedes it, so its failure sent nothing.
function first(device, end) {
  let link = links.connection(device, end);
  let power = ask(link, 'power_status ?', FIRST_MS);
  if (power === null) {
    link = links.connection(device, end);
    power = ask(link, 'power_status ?', FIRST_MS);
  }
  if (power === 'err_cmd') power = ask(link, 'power_status ?', FIRST_MS);
  if (power === null) throw new Error(link.why);
  return [link, power];
}

// The reads after `lines`; on failure {failed, reached}: whether any command
// line was written before it, so a routing executor knows nothing was sent.
// `answers` holds each line's answer (null: none came), and `before` the
// power_status read before them.
function session(device, lines) {
  if (!SEND) {
    for (const line of lines) withheld(device, `sending ${line}`);
    return {failed:true, reached:false, answers:[]};
  }
  const answers = [];
  let before;
  try {
    let [link, power] = first(device, thermiteIO.deadline(SESSION_MS));
    before = value(power);
    for (const line of lines) {
      answers.push(send(device, link, line));
      if (answers[answers.length - 1] === null) throw new Error(link.why);
    }
    if (lines.length > 0) power = needed(link, 'power_status ?', ANSWER_MS);
    const status = value(power);
    const input = value(needed(link, 'input ?', ANSWER_MS));
    const mode = status === 'on' ? value(needed(link, 'picture_mode ?', ANSWER_MS)) : 'UNDEF';
    health.answered(device.id, 'answering over ADCP again');
    show(device, status, input, mode);
    return {power: has(POWER,status) ? POWER[status] : 'UNDEF', status, input, picture_mode:mode, answers, before};
  } catch (e) {
    links.drop(device);
    health.failed(device.id, `${e && e.message ? e.message : e}; its Items are UNDEF until it answers`);
    show(device, 'UNDEF', 'UNDEF', 'UNDEF');
    return {failed:true, reached:answers.length > 0, answers, before};
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
    for (const device of DEVICES) if (health.due(device.id)) session(device, []);
  },
});

for (const device of DEVICES.filter((d) => d.control)) {
  thermiteDevice.commandRule({
    id: `sony_adcp_${device.id}_command`,
    name: `${device.id}: send its power, input and key commands over ADCP`,
    items: ['power', 'input', 'key'].map((point) => `${device.id}_${point}`),
    actor: () => routingLifecycleActor,
    direct: (event) => directRoutingCommand(device, event),
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

// fields: the actor's fields (ROUTING_EXECUTOR.fields), for the power
// field's declared settle.
function routingProvider(fields = {}) {
  const device=DEVICES[0];
  function point(resource) { return resource.slice(device.id.length+1); }
  // A power or input command the projector declines (err_*), or takes and
  // ignores, while it starts up or cools down the other way is busy, not a
  // no: it can once the transition ends. Power the other way, and an input
  // while it starts up; a power_status read before the line or after it
  // says so. It is sent again after a tenth of the power field's declared
  // settle toward where it is going (sony_adcp.fields()), about when the
  // planner would read a transition again; null when it is not busy.
  function busy(name, value, values, declined) {
    const toward = status => has(MOVING, status) ? POWER[status] : null;
    const statuses = declined ? [values.before, values.status] : [values.status];
    const moving = statuses.map(toward).find(target => target !== null &&
      (name === 'power' ? target !== value : name === 'input' && target === 'ON'));
    if (moving === undefined) return null;
    const field = fields[`${device.id}_power`], settle = field && field.settle ? field.settle[moving] : undefined;
    return Number.isFinite(settle) ? {outcome:'busy', retryAfterMs:Math.ceil(settle / 10)} : {outcome:'busy'};
  }
  function sample(resource, values) {
    const name = point(resource), value = values && values[name];
    if (!value || value === 'UNDEF') throw Error('native_adcp_status_unavailable');
    const read = thermiteDevice.sample(value);
    if (name === 'power' && has(MOVING, values.status)) read.transition = true;
    return read;
  }
  return {
    // Menu navigation can eventually select another input. Treat it as an
    // unknown manual input effect rather than reasserting the old route.
    effects:resource=>point(resource)==='key' ? [`${device.id}_input`] : [],
    oneShot:resource=>point(resource)==='key',
    has:(resource,value)=>resource.startsWith(device.id+'_') && lineFor(point(resource),String(value)) !== null,
    // The connection (kept, or new and greeted) and the first power_status
    // on it precede the command line: their failure sent nothing. err_* is
    // the projector declining it, busy() while a transition lasts; no answer
    // after the line, or a close before one, is the only unknown outcome. A
    // read after an ok that gets no answer leaves it applied, unconfirmed:
    // the actor reads again.
    execute:(resource,value)=>{
      const values=directRoutingCommand(device,{itemName:resource,receivedCommand:value});
      const answer=values && values.answers ? values.answers[0] : undefined;
      if (!values || answer === undefined) return {outcome:'unsent'};
      if (typeof answer === 'string' && answer.startsWith('err_')) return busy(point(resource),value,values,true) || {outcome:'refused'};
      if (answer !== 'ok') return {outcome:'unknown'};
      if (point(resource)==='key') return {outcome:'applied'};
      return busy(point(resource),value,values,false) || thermiteDevice.applied(()=>sample(resource,values));
    },
    probe:resource=>sample(resource,session(device,[])),
  };
}

// Set only by the routing lifecycle renderer. Legacy configuration keeps
// this device's existing command rule and protocol ownership.
const ROUTING_EXECUTOR = null;
if (ROUTING_EXECUTOR) routingLifecycleActor = require('thermite-routing-io').actor(ROUTING_EXECUTOR, routingProvider(ROUTING_EXECUTOR.fields), {readonly: !SEND});
