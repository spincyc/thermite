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
// whenever a stream changes, and the rule reads it again at once. A new
// connection asks `get ipinterrupt` and turns the setting on if it is not
// (it is off from the factory, and a reset turns it off).
//
// A command to <id>_input, an input number, is sent as `set insel <n> 4`
// (TX1 a copy of TX0, as it always is in splitter mode) and waited for: the
// switch acknowledges it, then the rule reads it back. A command naming no
// input of the switch is dropped. A command `reboot` to <id>_reboot sends
// `set reboot`, logged: the switch restarts without losing its settings and
// does not answer for about 15 s. That is the cure when its web server hangs
// (every HTTP request reset) while its telnet still answers; nothing sends
// it unasked.
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
// command it does not know gets no answer at all. Its command list names
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
// tx0`), so a missing `i` is taken too.
const INSEL = /^(?:set\s+)?i?nsel\s+(\d+)\s+(\d+)$/i;
// The answer to get ipinterrupt and set ipinterrupt, and its value when on.
const INTERRUPTS = /interrupt\s+(on|off|0|1)$/i;
const ON = /^(?:on|1)$/i;
// The switch's change notice.
const NOTICE = /^int\b/i;
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

// Send `line`, and return the first line that answers it within ANSWER_MS:
// one matching `wanted`, or with wanted null any line but a notice. A
// notice that comes meanwhile is remembered. null: no answer came.
function ask(device, link, line, wanted) {
  thermiteIO.write(link, `${line}\n`);
  const end = Date.now() + ANSWER_MS;
  while (Date.now() < end) {
    const said = next(link, end - Date.now());
    if (said === null) return null;
    const answer = said.trim();
    if (NOTICE.test(answer)) noticed.add(device.id);
    else if (wanted === null || wanted.test(answer)) return answer;
  }
  return null;
}

function open(device, end) {
  let link;
  try { link = thermiteIO.open(device.host, device.port, CONNECT_MS, end); }
  catch (_) { throw new Error(`cannot connect to ${device.host}:${device.port}`); }
  try {
    const state = ask(device, link, 'get ipinterrupt', INTERRUPTS);
    if (!link.closed && !(state && ON.test(INTERRUPTS.exec(state)[1]))) {
      const answer = ask(device, link, 'set ipinterrupt on', INTERRUPTS);
      console.info(`${device.id}: sent set ipinterrupt on, so that it says when a stream changes` +
        (answer === null ? '; it did not answer' : ''));
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
  return answer === null ? null : Number(INSEL.exec(answer)[1]);
}

// set insel, then its answer, waited for so that the read after it sees the
// switch: 'acknowledged' when the switch answers it, 'refused' when its
// answer names another input for TX0, and 'unanswered' when no answer came
// in time or the switch closed the connection. Only the last may have
// switched without saying so.
function send(device, link, input) {
  const line = `set insel ${input} ${COPY}`;
  const answer = ask(device, link, line, null);
  console.info(`${device.id}: sent ${line}`);
  if (answer === null) {
    console.warn(`${device.id}: did not answer ${line}`);
    return 'unanswered';
  }
  const named = INSEL.exec(answer);
  if (named && Number(named[1]) !== input) {
    console.warn(`${device.id}: answered ${line} with ${answer}`);
    return 'refused';
  }
  return 'acknowledged';
}

// One use of the switch's connection: set insel to `input` if it is not
// null, then the read. {shown, answer}: TX0's input, or null when the read
// failed; and set insel's answer (see send()), or null when no byte of it
// was written, so that a routing executor knows what the switch did with
// it even when the read after it failed.
function session(device, input = null) {
  if (!SEND) {
    if (input !== null) withheld(device, `sending set insel ${input} ${COPY}`);
    return {shown:null, answer:null};
  }
  let answer = null;
  try {
    const link = links.connection(device, thermiteIO.deadline(SESSION_MS));
    if (input !== null) {
      // A write that fails part way may have sent some of the line.
      answer = 'unanswered';
      answer = send(device, link, input);
    }
    const shown = shows(device, link);
    if (shown === null) throw new Error(link.closed ? 'closed the connection' : 'does not answer get insel');
    noticed.delete(device.id);
    health.answered(device.id, 'answering over telnet again');
    if (input !== null && shown !== input) {
      console.warn(`${device.id}: TX0 shows input ${shown} after set insel ${input} ${COPY}`);
    }
    reading(`${device.id}_input`, String(shown));
    return {shown, answer};
  } catch (e) {
    links.drop(device);
    health.failed(device.id, `${e && e.message ? e.message : e}; its Items are UNDEF until it answers`);
    reading(`${device.id}_input`, 'UNDEF');
    return {shown:null, answer};
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
      links.listen(device, thermiteIO.deadline(LISTEN_MS));
      if (noticed.has(device.id)) session(device);
    }
  },
});

for (const device of DEVICES) {
  thermiteDevice.commandRule({
    id: `vrroom_telnet_${device.id}_command`,
    name: `${device.id}: switch its input, or reboot it, over telnet`,
    items: [`${device.id}_input`, `${device.id}_reboot`],
    actor: () => routingLifecycleActor,
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
    execute:(resource,value)=>{
      const result=directRoutingCommand(device,{itemName:resource,receivedCommand:value});
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
