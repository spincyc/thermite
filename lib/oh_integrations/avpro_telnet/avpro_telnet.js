// AVPro Edge HDMI matrices over telnet (lib/oh_integrations/avpro_telnet).
//
// DEVICES lists each matrix: its id (its Items are <id>_output<N>_input),
// the host and port of its telnet server, and its numbers of inputs and
// outputs. The rule keeps one connection open to each matrix, as AVPro's
// own control drivers do, and every 10 s asks it for the input each output
// shows (GET OUT0 VS) and posts them, changed or not: each read is an Item
// update that keeps the reading fresh for the room policy worker, which
// counts one older than 30 s as unknown (thermiteDevice.reading). A command
// to an output's Item, an input number, is sent as SET OUT<n> VS IN<m> on
// that connection, followed by the same read. A command that names no input
// of the matrix is dropped.
//
// A matrix that does not answer within ANSWER_MS, or closes the connection
// while answering, gets UNDEF and a new connection: at the next poll, then
// after 2, 4 and at most RETRY_POLLS polls while it keeps refusing
// (thermiteDevice.health). A command or a route probe connects at once. A
// connection the matrix closed while idle is replaced within the same poll.
// The script closes its connections when it unloads (thermiteDevice.links).
//
// The matrix serves few telnet sessions, and Control4 keeps one. Each
// connection costs it a session and a listing of its settings, and the
// house's has been seen accepting every new connection and closing it at
// once. So a read-only copy (SEND false, oh_render.read_only), which runs
// beside the live runtime, connects to nothing and only logs what it would
// send, and nothing probes the port (avpro_telnet.PROBED).
//
// The matrix takes CRLF-terminated ASCII lines and answers in lines, one
// `OUT<n> VS IN<m>` per output a request names, and CMD ERR (or NO
// SUPPORT) to a request it refuses; a routing executor is told which answer
// a SET got (send(), routingProvider()). On connect it lists every
// output's input and other settings unasked, so a connection first reads
// until the matrix goes quiet, and lines that came between requests are
// dropped before the next (thermiteDevice.links): only lines after a
// request answer it. A line it sends of its own accord, such as for a switch
// Control4 made at the same time, names a current route just the same.
// openHAB runs one rule of a script file at a time, which serializes each
// connection's use.
// Each rendered device gets its own context, including its ordered command
// and poll rules. A slow peer cannot hold another device's context.
const DEVICES = null;
const SEND = true;

const openhab = require('openhab');
let routingLifecycleActor = null;

// THERMITE_BOUNDED_IO
// THERMITE_DEVICE
const { reading, withheld, close } = thermiteDevice;

const POLL = '0/10 * * * * ?';
// A refused matrix is tried at most every this many polls.
const RETRY_POLLS = 6;
const CONNECT_MS = 2000;
// The listing on connect is over once the matrix is quiet this long.
const QUIET_MS = 300;
// The longest the listing is waited out, should the matrix keep talking.
const LISTING_MS = 3000;
// Time for the whole answer to a request.
const ANSWER_MS = 2000;
// The deadline of each use of a connection: a listing, a SET and a read.
const SESSION_MS = 10000;
// A route line. The matrix's web page prefixes its own requests with an
// address (A00), which an answer may echo; an answer to SET may repeat
// SET, and a number may be zero-padded.
const ROUTE = /^(?:A\d+\s*)?(?:SET\s+)?OUT0*(\d+)\s+VS\s+IN0*(\d+)$/i;
// Its answer to a request it refuses.
const REFUSED = /^(?:A\d+\s*)?(?:CMD ERR|NO SUPPORT)\b/i;
// Matrices that stopped answering, logged once, and their backoff.
const health = thermiteDevice.health(RETRY_POLLS);

// The next line within `ms`, or null: none came, or the matrix closed the
// connection (link.closed).
function next(link, ms) {
  if (link.closed) return null;
  try { return thermiteIO.line(link, ms); }
  catch (error) {
    // A quiet listing is expected; an incomplete line cannot answer a later
    // command and must not be retained as though it were a fresh reply.
    if (!thermiteIO.quietTimeout(link, error)) close(link);
    return null;
  }
}

function write(link, line) { thermiteIO.write(link, `${line}\r\n`); }

function open(device, end) {
  let link;
  try { link = thermiteIO.open(device.host, device.port, CONNECT_MS, end); }
  catch (_) { throw new Error(`cannot connect to ${device.host}:${device.port}`); }
  try {
    const listingEnd = Math.min(end, thermiteIO.deadline(LISTING_MS));
    while (!link.closed) {
      let left;
      try { left = thermiteIO.remaining(listingEnd); } catch (_) { break; }
      if (next(link, Math.min(QUIET_MS, left)) === null) break;
    }
    if (link.closed) throw new Error('closed the connection');
    return link;
  } catch (error) { close(link); throw error; }
}

// Each matrix's kept connection. The lines that came since the last
// request are dropped before the next: none answers it.
const links = thermiteDevice.links(open);

// [output, input] of a route line naming one of the matrix's ports, or null.
function route(device, line) {
  const match = ROUTE.exec(line.trim());
  if (!match) return null;
  const output = Number(match[1]);
  const input = Number(match[2]);
  return output >= 1 && output <= device.outputs && input >= 1 && input <= device.inputs ? [output, input] : null;
}

// Each output's input, from the lines that answer GET OUT0 VS; null for an
// output none of them named in time.
function routes(device, link) {
  const shown = new Array(device.outputs).fill(null);
  write(link, 'GET OUT0 VS');
  let missing = device.outputs;
  const end = Date.now() + ANSWER_MS;
  while (missing > 0 && Date.now() < end) {
    const line = next(link, end - Date.now());
    if (line === null) break;
    const found = route(device, line);
    if (found === null) continue;
    if (shown[found[0] - 1] === null) missing -= 1;
    shown[found[0] - 1] = found[1];
  }
  return shown;
}

// SET, then its answer, waited for so that the read after it sees the
// switch: 'acknowledged' when a route line names the output (the matrix
// answers a SET with the output's route), 'refused' on CMD ERR or NO
// SUPPORT, and 'unanswered' when neither came in time or the matrix closed
// the connection. Only the last may have switched without saying so.
function send(device, link, output, input) {
  const line = `SET OUT${output} VS IN${input}`;
  write(link, line);
  console.info(`${device.id}: sent ${line}`);
  const end = Date.now() + ANSWER_MS;
  while (Date.now() < end) {
    const answer = next(link, end - Date.now());
    if (answer === null) break;
    if (REFUSED.test(answer.trim())) {
      console.warn(`${device.id}: answered ${line} with ${answer.trim()}`);
      return 'refused';
    }
    const found = route(device, answer);
    if (found !== null && found[0] === output) return 'acknowledged';
  }
  console.warn(`${device.id}: did not answer ${line}`);
  return 'unanswered';
}

function show(device, shown) {
  shown.forEach((input, index) => {
    reading(`${device.id}_output${index + 1}_input`, input === null ? 'UNDEF' : String(input));
  });
}

// One use of the matrix's connection: the SET of `command` ([output,
// input]) if there is one, then the read. {shown, answer}: each output's
// input, or null when the read failed; and the SET's answer (see send()),
// or null when no SET byte was written, so that a routing executor knows
// what the matrix did with it even when the read after it failed.
function session(device, command = null) {
  if (!SEND) {
    if (command) withheld(device, `sending SET OUT${command[0]} VS IN${command[1]}`);
    return {shown:null, answer:null};
  }
  let answer = null;
  try {
    const link = links.connection(device, thermiteIO.deadline(SESSION_MS));
    if (command) {
      // A write that fails part way may have sent some of the line.
      answer = 'unanswered';
      answer = send(device, link, command[0], command[1]);
    }
    const shown = routes(device, link);
    if (shown.every((input) => input === null)) {
      throw new Error(link.closed ? 'closed the connection' : 'does not answer GET OUT0 VS');
    }
    health.answered(device.id, 'answering over telnet again');
    if (command && shown[command[0] - 1] !== command[1]) {
      console.warn(`${device.id}: output ${command[0]} shows input ${shown[command[0] - 1]} after SET OUT${command[0]} VS IN${command[1]}`);
    }
    show(device, shown);
    return {shown, answer};
  } catch (e) {
    links.drop(device);
    health.failed(device.id, `${e && e.message ? e.message : e}; its Items are UNDEF until it answers`);
    show(device, new Array(device.outputs).fill(null));
    return {shown:null, answer};
  }
}

// "2" (or "2.0") -> 2; anything else -> null.
function inputOf(command) {
  const match = /^(\d+)(\.0+)?$/.exec(command);
  return match ? Number(match[1]) : null;
}

openhab.rules.JSRule({
  id: `avpro_telnet_${DEVICES[0].id}_poll`,
  name: 'HDMI matrices: read their routing over telnet',
  triggers: [openhab.triggers.GenericCronTrigger(POLL)],
  execute: () => {
    for (const device of DEVICES) if (health.due(device.id)) session(device);
  },
});

for (const device of DEVICES) {
  const outputs = Array.from({ length: device.outputs }, (_, index) => index + 1);
  thermiteDevice.commandRule({
    id: `avpro_telnet_${device.id}_command`,
    name: `${device.id}: switch its outputs over telnet`,
    items: outputs.map((output) => `${device.id}_output${output}_input`),
    actor: () => routingLifecycleActor,
    direct: (event) => directRoutingCommand(device, event),
  });
}

function directRoutingCommand(device, event) {
  const output = Number(/_output(\d+)_input$/.exec(event.itemName)[1]);
  const command = String(event.receivedCommand);
  const input = inputOf(command);
  if (input === null || input < 1 || input > device.inputs) {
    console.warn(`${event.itemName}: ${device.id} has no input '${command}', dropped`);
    return;
  }
  return session(device, [output, input]);
}

function routingProvider() {
  const device=DEVICES[0];
  function output(resource) { const match=/_output(\d+)_input$/.exec(resource);return match?Number(match[1]):0; }
  function sample(resource,values) {
    const value=values && values[output(resource)-1];
    if (value===null || value===undefined) throw Error('native_avpro_status_unavailable');
    return thermiteDevice.sample(String(value));
  }
  return {
    has:(resource,value)=>resource.startsWith(device.id+'_') && output(resource)>=1 && output(resource)<=device.outputs &&
      inputOf(String(value))>=1 && inputOf(String(value))<=device.inputs,
    // The SET's own answer decides (send()). A connection opened for it,
    // and its listing, precede it: their failure sent nothing (unsent).
    // CMD ERR is the matrix declining (refused). A SET it acknowledged is
    // applied even when the read after it fails: the actor reads again, and
    // a slow switch is re-read too. A SET it did not answer may still have
    // switched, so it alone is unknown.
    execute:(resource,value)=>{
      const result=directRoutingCommand(device,{itemName:resource,receivedCommand:value});
      if (!result || result.answer===null) return {outcome:'unsent'};
      if (result.answer==='refused') return {outcome:'refused'};
      if (result.answer!=='acknowledged') return {outcome:'unknown'};
      return thermiteDevice.applied(()=>sample(resource,result.shown));
    },
    probe:resource=>sample(resource,session(device).shown),
  };
}

// Set only by the routing lifecycle renderer. Legacy configuration keeps
// this device's existing command rule and protocol ownership.
const ROUTING_EXECUTOR = null;
if (ROUTING_EXECUTOR) routingLifecycleActor = require('thermite-routing-io').actor(ROUTING_EXECUTOR, routingProvider(), {readonly: !SEND});
