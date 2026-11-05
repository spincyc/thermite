// AVPro Edge HDMI matrices over telnet (lib/oh_integrations/avpro_telnet).
//
// DEVICES lists each matrix: its id (its Items are <id>_output<N>_input),
// the host and port of its telnet server, and its numbers of inputs and
// outputs. Every 5 s the rule opens one connection per matrix, reads the
// input each output shows (GET OUT0 VS) and posts them; a matrix that does
// not answer gets UNDEF. A command to an output's Item, an input number, is
// sent as SET OUT<n> VS IN<m> on a connection of its own, followed by the
// same read. A command that names no input of the matrix is dropped. With
// SEND false (oh_render.read_only) the rule still reads, and logs what it
// would send.
//
// The matrix takes CRLF-terminated ASCII lines and answers in lines, one
// `OUT<n> VS IN<m>` per output a request names. On connect it lists every
// output's input and other settings unasked, so a connection first reads
// until the matrix goes quiet, and only lines after a request answer it. A
// line it sends of its own accord, such as for a switch Control4 made at the
// same time, names a current route just the same. openHAB runs one rule of a
// script file at a time, which serializes the connections.
// Each rendered device gets its own context, including its ordered command
// and poll rules. A slow peer cannot hold another device's context.
const DEVICES = null;
const SEND = true;

const openhab = require('openhab');
let routingLifecycleActor = null;

// THERMITE_BOUNDED_IO

const POLL = '0/5 * * * * ?';
const CONNECT_MS = 2000;
// The listing on connect is over once the matrix is quiet this long.
const QUIET_MS = 300;
// The longest the listing is waited out, should the matrix keep talking.
const LISTING_MS = 3000;
// Time for the whole answer to a request.
const ANSWER_MS = 2000;
// A route line; the matrix's web page prefixes its own requests with an
// address (A00), which an answer may echo.
const ROUTE = /^(?:A\d+ )?OUT(\d+) VS IN(\d+)$/;
// Matrix ids that did not answer their last session: logged once.
const down = new Set();

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
  if (!SEND) {
    console.info(`${device.id}: SEND is false, not sending ${line}`);
    return;
  }
  write(link, line);
  console.info(`${device.id}: sent ${line}`);
  const end = Date.now() + ANSWER_MS;
  while (Date.now() < end) {
    const answer = next(link, end - Date.now());
    if (answer === null) return;
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
  let link = null, reached = false;
  const end = thermiteIO.deadline(10000);
  try {
    link = open(device, end);
    for (const [output, input] of commands) {
      reached = true;
      send(device, link, output, input);
    }
    const shown = routes(device, link);
    if (shown.every((input) => input === null)) throw new Error('does not answer GET OUT0 VS');
    if (down.delete(device.id)) console.info(`${device.id}: answering over telnet again`);
    for (const [output, input] of commands) {
      if (SEND && shown[output - 1] !== input) {
        console.warn(`${device.id}: output ${output} shows input ${shown[output - 1]} after SET OUT${output} VS IN${input}`);
      }
    }
    show(device, shown);
    return shown;
  } catch (e) {
    if (!down.has(device.id)) {
      down.add(device.id);
      console.warn(`${device.id}: ${e && e.message ? e.message : e}; its Items are UNDEF until it answers`);
    }
    show(device, new Array(device.outputs).fill(null));
    return {failed:true, reached};
  } finally {
    if (link) close(link);
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
    for (const device of DEVICES) session(device, []);
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
    // The connection and its listing precede the SET: their failure sent
    // nothing. A matrix that then answers GET OUT0 VS on the same connection
    // has processed the SET; a slow switch is re-read by the actor. No
    // answer after the SET is the only unknown outcome.
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
