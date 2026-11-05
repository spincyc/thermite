// AVPro Edge HDMI matrices over telnet (lib/oh_integrations/avpro_telnet).
//
// DEVICES lists each matrix: its id (its Items are <id>_output<N>_input),
// the host and port of its telnet server, and its numbers of inputs and
// outputs. The rule keeps one connection open to each matrix, as AVPro's
// own control drivers do, and every 10 s asks it for the input each output
// shows (GET OUT0 VS) and posts them. A command to an output's Item, an
// input number, is sent as SET OUT<n> VS IN<m> on that connection, followed
// by the same read. A command that names no input of the matrix is dropped.
//
// A matrix that does not answer within ANSWER_MS, or closes the connection
// while answering, gets UNDEF and a new connection: at the next poll, then
// after 2, 4 and at most RETRY_POLLS polls while it keeps refusing
// (later()). A command or a route probe connects at once. A connection the
// matrix closed while idle is replaced within the same poll. The script
// closes its connections when it unloads.
//
// The matrix serves few telnet sessions, and Control4 keeps one. Each
// connection costs it a session and a listing of its settings, and the
// house's has been seen accepting every new connection and closing it at
// once. So a read-only copy (SEND false, oh_render.read_only), which runs
// beside the live runtime, connects to nothing and only logs what it would
// send, and nothing probes the port (avpro_telnet.PROBED).
//
// The matrix takes CRLF-terminated ASCII lines and answers in lines, one
// `OUT<n> VS IN<m>` per output a request names. On connect it lists every
// output's input and other settings unasked, so a connection first reads
// until the matrix goes quiet, and lines that came between requests are
// dropped before the next (drain()): only lines after a request answer it.
// A line it sends of its own accord, such as for a switch Control4 made at
// the same time, names a current route just the same. openHAB runs one rule
// of a script file at a time, which serializes each connection's use.
// Each rendered device gets its own context, including its ordered command
// and poll rules. A slow peer cannot hold another device's context.
const DEVICES = null;
const SEND = true;

const openhab = require('openhab');
let routingLifecycleActor = null;

// THERMITE_BOUNDED_IO

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
// Matrix id -> its open connection.
const links = new Map();
// Matrix ids that did not answer their last session: logged once.
const down = new Set();
// Matrix id -> {every, left}: a matrix that refused its last session is
// tried every that many polls, left more from now.
const retry = new Map();

// The backoff after a refused session, given the one before it.
function later(last) {
  const every = last ? Math.min(last.every * 2, RETRY_POLLS) : 1;
  return { every, left: every - 1 };
}

function close(link) {
  try { thermiteIO.close(link); } catch (_) { /* no resource remains usable */ }
}

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

// Drop the lines that came since the last request: none answers the next.
function drain(link) {
  while (next(link, 1) !== null) { /* unasked */ }
}

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

// The matrix's connection for a session ending at `end`, with nothing
// unread: the one kept, or a new one if there is none or the matrix closed
// it while idle.
function connection(device, end) {
  const kept = links.get(device.id);
  if (kept) {
    thermiteIO.renew(kept, end);
    drain(kept);
    if (!kept.closed) return kept;
    links.delete(device.id);
  }
  const link = open(device, end);
  links.set(device.id, link);
  return link;
}

function drop(device) {
  const link = links.get(device.id);
  links.delete(device.id);
  if (link) close(link);
}

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

// SET, then its answer, the new route's line, waited for so that the read
// after it sees the switch.
function send(device, link, output, input) {
  const line = `SET OUT${output} VS IN${input}`;
  write(link, line);
  console.info(`${device.id}: sent ${line}`);
  const end = Date.now() + ANSWER_MS;
  while (Date.now() < end) {
    const answer = next(link, end - Date.now());
    if (answer === null) return;
    if (REFUSED.test(answer.trim())) {
      console.warn(`${device.id}: answered ${line} with ${answer.trim()}`);
      return;
    }
    const found = route(device, answer);
    if (found !== null && found[0] === output) return;
  }
}

function update(name, state) {
  const item = openhab.items.getItem(name);
  if (String(item.state) !== state) item.postUpdate(state);
}

function show(device, shown) {
  shown.forEach((input, index) => {
    update(`${device.id}_output${index + 1}_input`, input === null ? 'UNDEF' : String(input));
  });
}

// Each output's input; on failure {failed, reached}: whether a SET was
// written before it, so a routing executor knows when nothing was sent.
function session(device, commands) {
  if (!SEND) {
    for (const [output, input] of commands) {
      console.info(`${device.id}: SEND is false, not sending SET OUT${output} VS IN${input}`);
    }
    return {failed:true, reached:false};
  }
  let reached = false;
  try {
    const link = connection(device, thermiteIO.deadline(SESSION_MS));
    for (const [output, input] of commands) {
      reached = true;
      send(device, link, output, input);
    }
    const shown = routes(device, link);
    if (shown.every((input) => input === null)) {
      throw new Error(link.closed ? 'closed the connection' : 'does not answer GET OUT0 VS');
    }
    if (down.delete(device.id)) console.info(`${device.id}: answering over telnet again`);
    retry.delete(device.id);
    for (const [output, input] of commands) {
      if (SEND && shown[output - 1] !== input) {
        console.warn(`${device.id}: output ${output} shows input ${shown[output - 1]} after SET OUT${output} VS IN${input}`);
      }
    }
    show(device, shown);
    return shown;
  } catch (e) {
    drop(device);
    if (!down.has(device.id)) {
      down.add(device.id);
      console.warn(`${device.id}: ${e && e.message ? e.message : e}; its Items are UNDEF until it answers`);
    }
    retry.set(device.id, later(retry.get(device.id)));
    show(device, new Array(device.outputs).fill(null));
    return {failed:true, reached};
  }
}

// A redeploy or a stop unloads the script: free the matrix's sessions now.
require('@runtime').lifecycleTracker.addDisposeHook(() => {
  for (const device of DEVICES) drop(device);
});

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
    for (const device of DEVICES) {
      const backoff = retry.get(device.id);
      if (backoff && backoff.left > 0) backoff.left -= 1;
      else session(device, []);
    }
  },
});

for (const device of DEVICES) {
  const outputs = Array.from({ length: device.outputs }, (_, index) => index + 1);
  openhab.rules.JSRule({
    id: `avpro_telnet_${device.id}_command`,
    name: `${device.id}: switch its outputs over telnet`,
    triggers: outputs.map((output) => openhab.triggers.ItemCommandTrigger(`${device.id}_output${output}_input`)),
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
  const output = Number(/_output(\d+)_input$/.exec(event.itemName)[1]);
  const command = String(event.receivedCommand);
  const input = inputOf(command);
  if (input === null || input < 1 || input > device.inputs) {
    console.warn(`${event.itemName}: ${device.id} has no input '${command}', dropped`);
    return;
  }
  return session(device, [[output, input]]);
}

function routingProvider() {
  const device=DEVICES[0];
  function output(resource) { const match=/_output(\d+)_input$/.exec(resource);return match?Number(match[1]):0; }
  function sample(resource,values) {
    const value=values && values[output(resource)-1];
    if (value===null || value===undefined) throw Error('native_avpro_status_unavailable');
    return {value:String(value),fresh:true,basis:'owned_native_read',observedAt:Date.now()};
  }
  return {
    has:(resource,value)=>resource.startsWith(device.id+'_') && output(resource)>=1 && output(resource)<=device.outputs &&
      inputOf(String(value))>=1 && inputOf(String(value))<=device.inputs,
    // A connection opened for the SET, and its listing, precede it: their
    // failure sent nothing. A matrix that then answers GET OUT0 VS on the
    // same connection has processed the SET; a slow switch is re-read by
    // the actor. No answer after the SET is the only unknown outcome.
    execute:(resource,value)=>{
      const values=directRoutingCommand(device,{itemName:resource,receivedCommand:value});
      if (!values || values.failed && !values.reached) return {outcome:'unsent'};
      if (values.failed) return {outcome:'unknown'};
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
