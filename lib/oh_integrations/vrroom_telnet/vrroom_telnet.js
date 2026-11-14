// HDFury VRROOM HDMI switches over telnet (lib/oh_integrations/vrroom_telnet).
//
// DEVICES lists each switch: its id (its Items are <id>_input and
// <id>_reboot), the host and port of its telnet server and its number of
// inputs. The rule keeps one connection open to each switch and every 10 s
// asks it which input TX0 shows (`get insel`, answered `insel <tx0> <tx1>`)
// and posts it, changed or not: each read is an Item update that keeps the
// reading fresh for the room policy worker, which counts one older than
// 30 s as unknown (thermiteDevice.reading). Between polls it listens: with
// its ipinterrupt setting on, the switch says `int` on the connection
// whenever a stream changes, and the rule reads it again at once on that
// connection. The listening never connects: a switch that closed or stopped
// answering is left to the poll and its backoff. A new connection asks `get
// ipinterrupt` and turns the setting on when the switch answers that it is
// off (it is off from the factory, and a reset turns it off); no answer
// changes nothing.
//
// A command to <id>_input, an input number, is sent as `set insel <n> 4`
// (TX1 a copy of TX0, as it always is in splitter mode) and waited for: the
// switch answers it with the new insel line, or refuses it with `insel
// err`, then the rule reads it back. A command naming no
// input of the switch is dropped. A command `reboot` to <id>_reboot sends
// `set reboot`, logged: the switch restarts without losing its settings and
// does not answer for about 15 s. That is the cure when its web server hangs
// (every HTTP request reset) while its telnet still answers; only the
// nightly reboots (reboots.js) send it unasked, to a switch the house file
// gives `reboot: nightly`.
//
// A switch that does not answer within ANSWER_MS, or closes the connection
// while answering, gets UNDEF and a new connection: at the next poll, then
// after 2, 4 and at most RETRY_POLLS polls while it keeps refusing
// (thermiteDevice.health). A command or a route probe connects at once. A
// connection the switch closed while idle is replaced at the next poll. The
// script closes its connections when it unloads (thermiteDevice.links).
//
// The switch takes lowercase ASCII commands ending in a newline and answers
// each it knows with a line, a get with the setting's name and value; a
// command it does not know gets no answer at all. The wording of the
// answers (`insel %d %d`, `insel err`, `ipinterrupt on`, `ipinterrupt err`,
// `reboot`) is taken from strings in the switch's firmware, not from a
// recorded session, so each answer is matched loosely and any other line
// is skipped while waiting. Its command list names
// one "current open IP connection" for its notices, so a read-only copy
// (SEND false, oh_render.read_only), which runs beside the live runtime,
// connects to nothing and only logs what it would send, and nothing probes
// the port (vrroom_telnet.PROBED). openHAB runs one rule of a script file at
// a time, which serializes each connection's use. Each rendered device gets
// its own context, including its ordered command, poll and listen rules.
const DEVICES = null;
const SEND = true;

const openhab = require('openhab');
let routingLifecycleActor = null;

// THERMITE_BOUNDED_IO
// THERMITE_DEVICE
const { reading, withheld, close } = thermiteDevice;

const POLL = '0/10 * * * * ?';
// How often the kept connections are checked for a change notice.
const LISTEN = '* * * * * ?';
// A refused switch is tried at most every this many polls.
const RETRY_POLLS = 6;
const CONNECT_MS = 2000;
// Time for the answer to one command.
const ANSWER_MS = 2000;
// The deadline of each use of a connection: at most a connect, the two
// ipinterrupt commands, a switch and a read.
const SESSION_MS = 12000;
// The deadline of a check for notices, which waits for nothing.
const LISTEN_MS = 1000;
// The insel value that makes TX1 copy TX0.
const COPY = 4;
// The answer to get insel (and to set insel): TX0's input, then TX1's. The
// switch has been seen dropping the first letter of an answer (`get status
// tx0`), so a missing `i` is taken too. An echo of the command is not one.
const INSEL = /^i?nsel\s+(\d+)\s+(\d+)$/i;
// The answer to get ipinterrupt and set ipinterrupt, and its value when off.
const INTERRUPTS = /interrupt\s+(on|off|0|1)$/i;
const OFF = /^(?:off|0)$/i;
// The answer to a command the switch refuses (`insel err`, `ipinterrupt err`).
const ERR = /\berr\b/i;
// Switches that stopped answering, logged once, and their backoff.
const health = thermiteDevice.health(RETRY_POLLS);
// Switches that sent a change notice the rule has not yet read after.
const noticed = new Set();

// The next line within `ms`, or null: none came, or the switch closed the
// connection (link.closed).
function next(link, ms) {
  if (link.closed) return null;
  try { return thermiteIO.line(link, ms); }
  catch (error) {
    // A quiet switch is expected; an incomplete line cannot answer a later
    // command and must not be retained as though it were a fresh reply.
    if (!thermiteIO.quietTimeout(link, error)) close(link);
    return null;
  }
}

// Send `line`, and return the first line within ANSWER_MS that answers it:
// one matching `wanted` or ERR. Every other line (a change notice, an echo,
// an answer left over from an earlier command) is skipped: a notice needs
// no read of its own, as every use of the connection ends with get insel.
// null: no answer came.
function ask(device, link, line, wanted) {
  thermiteIO.write(link, `${line}\n`);
  const end = Date.now() + ANSWER_MS;
  while (Date.now() < end) {
    const said = next(link, end - Date.now());
    if (said === null) return null;
    const answer = said.trim();
    if (wanted.test(answer) || ERR.test(answer)) return answer;
  }
  return null;
}

function open(device, end) {
  let link;
  try { link = thermiteIO.open(device.host, device.port, CONNECT_MS, end); }
  catch (_) { throw new Error(`cannot connect to ${device.host}:${device.port}`); }
  try {
    const state = ask(device, link, 'get ipinterrupt', INTERRUPTS);
    const value = state && INTERRUPTS.exec(state);
    if (!link.closed && value && OFF.test(value[1])) {
      const answer = ask(device, link, 'set ipinterrupt on', INTERRUPTS);
      console.info(`${device.id}: sent set ipinterrupt on, so that it says when a stream changes` +
        (answer === null ? '; it did not answer' : ERR.test(answer) ? `; it answered ${answer}` : ''));
    }
    if (link.closed) throw new Error('closed the connection');
    return link;
  } catch (error) { close(link); throw error; }
}

// Each switch's kept connection. A line it says unasked between commands
// is a change notice: any, so that a notice worded otherwise is read too.
const links = thermiteDevice.links(open, (device) => noticed.add(device.id));

// TX0's input, from the answer to get insel; null when none came.
function shows(device, link) {
  const answer = ask(device, link, 'get insel', INSEL);
  const named = answer === null ? null : INSEL.exec(answer);
  return named ? Number(named[1]) : null;
}

// set insel, then its answer, waited for so that the read after it sees the
// switch: 'acknowledged' when the switch answers with an insel line naming
// that input for TX0, 'refused' when it answers `insel err` or names
// another input, and 'unanswered' when no answer came in time or the
// switch closed the connection. Only the last may have switched without
// saying so.
function send(device, link, input) {
  const line = `set insel ${input} ${COPY}`;
  const answer = ask(device, link, line, INSEL);
  console.info(`${device.id}: sent ${line}`);
  if (answer === null) {
    console.warn(`${device.id}: did not answer ${line}`);
    return 'unanswered';
  }
  const named = INSEL.exec(answer);
  if (!named || Number(named[1]) !== input) {
    console.warn(`${device.id}: answered ${line} with ${answer}`);
    return 'refused';
  }
  return 'acknowledged';
}

// One use of the switch's connection: set insel to `input` if it is not
// null, then the read. {shown, answer}: TX0's input, or null when the read
// failed; and set insel's answer (see send()), or null when no byte of it
// was written, so that a routing executor knows what the switch did with
// it even when the read after it failed. `kept`: the open connection to
// use, which a session from the listen rule passes so that it never
// connects; else the kept one, or a new one.
function session(device, input = null, kept = null) {
  if (!SEND) {
    if (input !== null) withheld(device, `sending set insel ${input} ${COPY}`);
    return {shown:null, answer:null};
  }
  let answer = null;
  try {
    const end = thermiteIO.deadline(SESSION_MS);
    const link = kept || links.connection(device, end);
    if (kept) thermiteIO.renew(link, end);
    if (input !== null) {
      // A write that fails part way may have sent some of the line.
      answer = 'unanswered';
      answer = send(device, link, input);
    }
    const shown = shows(device, link);
    if (shown === null) throw new Error(link.closed ? 'closed the connection' : 'does not answer get insel');
    health.answered(device.id, 'answering over telnet again');
    if (input !== null && answer !== 'refused' && shown !== input) {
      console.warn(`${device.id}: TX0 shows input ${shown} after set insel ${input} ${COPY}`);
    }
    reading(`${device.id}_input`, String(shown));
    return {shown, answer};
  } catch (e) {
    links.drop(device);
    health.failed(device.id, `${e && e.message ? e.message : e}; its Items are UNDEF until it answers`);
    reading(`${device.id}_input`, 'UNDEF');
    return {shown:null, answer};
  } finally {
    // Read, or failed: either way no notice is left to act on.
    noticed.delete(device.id);
  }
}

// set reboot on the switch's connection. {answer}: null when no byte of it
// was written, 'sent' once it was, and 'unanswered' when the write failed
// part way. Its input is UNDEF until the switch answers again.
function reboot(device, source) {
  if (withheld(device, 'sending set reboot')) return {answer:null};
  let answer = null;
  try {
    const link = links.connection(device, thermiteIO.deadline(SESSION_MS));
    answer = 'unanswered';
    thermiteIO.write(link, 'set reboot\n');
    answer = 'sent';
    console.warn(`${device.id}: sent set reboot` + (source ? ` (command from ${source})` : '') +
      '; it restarts and does not answer for about 15 s');
    // What it says before it goes, if anything, answers nothing later.
    next(link, ANSWER_MS);
  } catch (e) {
    console.warn(`${device.id}: could not send set reboot: ${e && e.message ? e.message : e}`);
  }
  links.drop(device);
  if (answer !== null) reading(`${device.id}_input`, 'UNDEF');
  return {answer};
}

// "2" (or "2.0") -> 2; anything else -> null.
function inputOf(command) {
  const match = /^(\d+)(\.0+)?$/.exec(command);
  return match ? Number(match[1]) : null;
}

openhab.rules.JSRule({
  id: `vrroom_telnet_${DEVICES[0].id}_poll`,
  name: 'HDMI switches: read their input over telnet',
  triggers: [openhab.triggers.GenericCronTrigger(POLL)],
  execute: () => {
    for (const device of DEVICES) if (health.due(device.id)) session(device);
  },
});

openhab.rules.JSRule({
  id: `vrroom_telnet_${DEVICES[0].id}_listen`,
  name: 'HDMI switches: read their input again when they say it changed',
  triggers: [openhab.triggers.GenericCronTrigger(LISTEN)],
  execute: () => {
    if (!SEND) return;
    for (const device of DEVICES) {
      noticed.delete(device.id);
      const link = links.listen(device, thermiteIO.deadline(LISTEN_MS));
      if (link && noticed.has(device.id)) session(device, null, link);
    }
  },
});

// Who asked for each reboot the routing actor queued, which the actor does
// not carry to execute(): {source, from, to}, `to` the time legacy() queued
// it by. The actor stamps each operation with its queuedAt, so execute()
// finds its own; any queued before it was sent or dropped.
const rebootSources = [];
// At most this many waiting (the actor's own queue holds 16).
const REBOOT_SOURCES = 16;

// The actor, its legacy() noting the source of each reboot it queues.
function sourcedActor(device) {
  if (!routingLifecycleActor) return null;
  return Object.assign(Object.create(routingLifecycleActor), {
    legacy(resource, value, sensitive, source) {
      const from = Date.now();
      const queued = routingLifecycleActor.legacy(resource, value, sensitive, source);
      if (queued && resource === `${device.id}_reboot`) {
        rebootSources.push({ source: source ? String(source) : null, from, to: Date.now() });
        if (rebootSources.length > REBOOT_SOURCES) rebootSources.shift();
      }
      return queued;
    },
  });
}

// The source of the reboot the actor queued at `queuedAt`, or null.
function rebootSource(queuedAt) {
  const at = Number(queuedAt);
  while (rebootSources.length && rebootSources[0].to < at) rebootSources.shift();
  if (rebootSources.length && rebootSources[0].from <= at) return rebootSources.shift().source;
  return null;
}

for (const device of DEVICES) {
  thermiteDevice.commandRule({
    id: `vrroom_telnet_${device.id}_command`,
    name: `${device.id}: switch its input, or reboot it, over telnet`,
    items: [`${device.id}_input`, `${device.id}_reboot`],
    actor: () => sourcedActor(device),
    direct: (event) => directRoutingCommand(device, event),
  });
}

function directRoutingCommand(device, event) {
  const command = String(event.receivedCommand);
  if (event.itemName === `${device.id}_reboot`) {
    if (command !== 'reboot') {
      console.warn(`${event.itemName}: '${command}' is not reboot, dropped`);
      return;
    }
    return reboot(device, event.eventSource ? String(event.eventSource) : null);
  }
  const input = inputOf(command);
  if (input === null || input >= device.inputs) {
    console.warn(`${event.itemName}: ${device.id} has no input '${command}', dropped`);
    return;
  }
  return session(device, input);
}

function routingProvider() {
  const device=DEVICES[0];
  const input=`${device.id}_input`, restart=`${device.id}_reboot`;
  function sample(values) {
    if (!values || values.shown===null || values.shown===undefined) throw Error('native_vrroom_status_unavailable');
    return thermiteDevice.sample(String(values.shown));
  }
  return {
    // A reboot is an action, not a value to hold; it leaves the input as it was.
    oneShot:resource=>resource===restart,
    has:(resource,value)=>resource===restart ? String(value)==='reboot' :
      resource===input && inputOf(String(value))!==null && inputOf(String(value))<device.inputs,
    // The answer to set insel decides (send()). A connection opened for it,
    // and its ipinterrupt commands, precede it: their failure sent nothing
    // (unsent). An answer naming another input is the switch declining
    // (refused). A set insel it acknowledged is applied even when the read
    // after it fails: the actor reads again. One it did not answer may still
    // have switched, so it alone is unknown. A set reboot written whole is
    // applied: the switch restarts, with nothing to read back.
    execute:(resource,value,operation)=>{
      const eventSource=resource===restart && operation ? rebootSource(operation.queuedAt) : null;
      const result=directRoutingCommand(device,{itemName:resource,receivedCommand:value,eventSource});
      if (!result || result.answer===null) return {outcome:'unsent'};
      if (resource===restart) return {outcome:result.answer==='sent' ? 'applied' : 'unknown'};
      if (result.answer==='refused') return {outcome:'refused'};
      if (result.answer!=='acknowledged') return {outcome:'unknown'};
      return thermiteDevice.applied(()=>sample(result));
    },
    probe:()=>sample(session(device)),
  };
}

// Set only by the routing lifecycle renderer. Legacy configuration keeps
// this device's existing command rule and protocol ownership.
const ROUTING_EXECUTOR = null;
if (ROUTING_EXECUTOR) routingLifecycleActor = require('thermite-routing-io').actor(ROUTING_EXECUTOR, routingProvider(), {readonly: !SEND});
