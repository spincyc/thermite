// AVPro Edge HDMI matrices over telnet (lib/oh_integrations/avpro_telnet).
//
// DEVICES lists each matrix: its id (its Items are <id>_output<N>_input),
// the host and port of its telnet server, the port of its web server
// (webPort), and its numbers of inputs and outputs. The rule keeps one
// connection open to each matrix, as AVPro's own control drivers do, and
// every 10 s asks it for the input each output
// shows (GET OUT0 VS) and posts them, changed or not: each read is an Item
// update that keeps the reading fresh for the room policy worker, which
// counts one older than 30 s as unknown (thermiteDevice.reading). A command
// to an output's Item, an input number, is sent as SET OUT<n> VS IN<m> on
// that connection, followed by the same read. A command that names no input
// of the matrix is dropped.
//
// A command `reboot` to <id>_reboot restarts the matrix as its web page's
// Reboot does: SET RBT on that connection, which the matrix answers RBT
// before its controller restarts (in about 4 s, routes kept), and then its
// network module's own restart, POST /do {"cmd": "c s reboot"} to its web
// server (webPort). That ends every session, and the matrix answers ping,
// telnet and its web page again about 19 s later (firmware 1.39,
// 2026-11-19). Its outputs are UNDEF from its RBT until a read RESTART_MS
// after it, on a new connection; meanwhile no read is made and a switch is
// not sent. The nightly reboots (reboots.js) send the command unasked, to
// a matrix the house file gives `reboot: nightly`. Neither SET RST nor the
// web page's restorefactory is ever sent: each resets the matrix to its
// factory settings.
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
// send, and nothing probes the port (avpro_telnet.PROBED). The diagnostic
// reads ride the kept connection too and never end it for a slow answer
// (collectSignals()): on 2026-11-20 closing it after every late reply made
// a new session every 30 s, and the house's matrix locked up 7 hours later.
//
// The matrix takes CRLF-terminated ASCII lines and answers in lines, one
// `OUT<n> VS IN<m>` per output a request names, and CMD ERR (or NO
// SUPPORT) to a request it refuses; a routing executor is told which answer
// a SET got (send(), routingProvider()). On connect it lists every
// output's input and other settings unasked, so a connection first reads
// until the matrix goes quiet, and lines that came between requests are
// dropped before the next (thermiteDevice.links): only lines after a
// request answer it. A line it sends of its own accord, such as for a switch
// Control4 made at the same time, names a current route just the same, so
// only a line naming the input it was sent answers a SET.
//
// Each diagnostic reply `OUT<n> HPD 1` or `OUT<n> HPD 0` is also posted
// to <id>_output<n>_hotplug as ON or OFF (oh_integrations/_hotplug.py):
// whether a display is cabled to that output, which the routing checks
// before it believes a route to it. A failed read of the routing posts
// UNDEF to each, as it does to the outputs' inputs.
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
// Its answer to GET OUT<n> HPD, the hot-plug of a display on output n.
const HOTPLUG = /^(?:A\d+\s*)?OUT0*(\d+)\s+HPD\s+([01])$/i;
// Its answer to a request it refuses.
const REFUSED = /^(?:A\d+\s*)?(?:CMD ERR|NO SUPPORT)\b/i;
// Its answer to SET RBT, sent before it restarts.
const RESTARTS = /^(?:A\d+\s*)?RBT$/i;
// What its web page posts to /do to restart its network module.
const NETWORK_RESTART = JSON.stringify({ cmd: 'c s reboot' });
// How long a matrix is left to restart once it has answered SET RBT.
const RESTART_MS = 30000;
// Matrices that stopped answering, logged once, and their backoff.
const health = thermiteDevice.health(RETRY_POLLS);
// Device id -> when the restart it answered SET RBT for is over.
const restarts = new Map();

function restarting(device) {
  if (!restarts.has(device.id)) return false;
  if (Date.now() < restarts.get(device.id)) return true;
  restarts.delete(device.id);
  return false;
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
  catch (_) { throw thermiteDevice.failure(`cannot connect to ${device.host}:${device.port}`, 'unreachable'); }
  try {
    const listingEnd = Math.min(end, thermiteIO.deadline(LISTING_MS));
    while (!link.closed) {
      let left;
      try { left = thermiteIO.remaining(listingEnd); } catch (_) { break; }
      if (next(link, Math.min(QUIET_MS, left)) === null) break;
    }
    if (link.closed) throw thermiteDevice.failure('closed the connection', 'no_answer');
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
// switch: 'acknowledged' when a route line names the output and the input
// sent (the matrix answers a SET with the output's new route), 'refused' on
// CMD ERR or NO SUPPORT, and 'unanswered' when neither came in time or the
// matrix closed the connection. A line naming another input for the output
// is a route from before, or one another controller made meanwhile, not
// this SET's answer. Only 'unanswered' may have switched without saying so.
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
    if (found !== null && found[0] === output && found[1] === input) return 'acknowledged';
  }
  console.warn(`${device.id}: did not answer ${line}`);
  return 'unanswered';
}

function show(device, shown) {
  shown.forEach((input, index) => {
    reading(`${device.id}_output${index + 1}_input`, input === null ? 'UNDEF' : String(input));
  });
}

// An output's hot-plug Item from its HPD reply: ON or OFF, or UNDEF for a
// reply that says neither, or none (a matrix that cannot be read).
function plugged(device, output, reply) {
  const match = reply === null ? null : HOTPLUG.exec(reply);
  const state = match && Number(match[1]) === output ? (match[2] === '1' ? 'ON' : 'OFF') : 'UNDEF';
  reading(`${device.id}_output${output}_hotplug`, state);
}

// What a matrix that cannot be read shows: no route and no hot-plug known.
function unread(device) {
  show(device, new Array(device.outputs).fill(null));
  for (let output = 1; output <= device.outputs; output += 1) plugged(device, output, null);
}

// One use of the matrix's connection: the SET of `command` ([output,
// input]) if there is one, then the read. {shown, answer, code}: each
// output's input, or null when the read failed; the SET's answer (see
// send()), or null when no SET byte was written, so that a routing executor
// knows what the matrix did with it even when the read after it failed; and
// the problem code of a session that failed (oh_integrations/_commands.py).
// While the matrix restarts there is no session: nothing is sent.
function session(device, command = null) {
  if (!SEND) {
    if (command) withheld(device, `sending SET OUT${command[0]} VS IN${command[1]}`);
    return {shown:null, answer:null, code:'read_only'};
  }
  if (restarting(device)) {
    if (command) console.warn(`${device.id}: restarting, so SET OUT${command[0]} VS IN${command[1]} is not sent`);
    return {shown:null, answer:null, code:'not_ready'};
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
      throw thermiteDevice.failure(link.closed ? 'closed the connection' : 'does not answer GET OUT0 VS', 'no_answer');
    }
    health.answered(device.id, 'answering over telnet again');
    if (command && shown[command[0] - 1] !== command[1]) {
      console.warn(`${device.id}: output ${command[0]} shows input ${shown[command[0] - 1]} after SET OUT${command[0]} VS IN${command[1]}`);
    }
    show(device, shown);
    return {shown, answer};
  } catch (e) {
    links.drop(device);
    const code = thermiteDevice.codeOf(e, 'no_answer');
    health.failed(device.id, `${e && e.message ? e.message : e}; its Items are UNDEF until it answers`, code);
    unread(device);
    return {shown:null, answer, code};
  }
}

// The network module's restart, once the controller's is under way: null
// when the web server took it, else why not.
function restartNetwork(device) {
  try {
    const response = thermiteIO.http(`http://${device.host}:${device.webPort}/do`, 'POST', NETWORK_RESTART,
      { 'Content-Type': 'application/json;charset=utf-8' }, ANSWER_MS, CONNECT_MS);
    const status = Number(response.getStatus());
    return status === 200 ? null : `its web server answered HTTP ${status}`;
  } catch (e) {
    return `${e && e.message ? e.message : e}`;
  }
}

// SET RBT on the matrix's connection, then, once answered, the network
// module's restart. {answer, code}: answer null when no byte of SET RBT was
// written, 'acknowledged' once the matrix answered RBT, 'refused' on CMD
// ERR or NO SUPPORT, and 'unanswered' when neither came in time; and the
// problem code when it failed. A matrix that answered RBT restarts
// (restarting()), whether or not its network module took its own restart.
// One that did not answer may have restarted. Either way its connection is
// dropped and its outputs are UNDEF until it is read again.
function reboot(device) {
  if (withheld(device, 'sending SET RBT')) return {answer:null, code:'read_only'};
  if (restarting(device)) return {answer:null, code:'not_ready'};
  let answer = null, code;
  try {
    const link = links.connection(device, thermiteIO.deadline(SESSION_MS));
    // A write that fails part way may have sent some of the line.
    answer = 'unanswered';
    write(link, 'SET RBT');
    const end = Date.now() + ANSWER_MS;
    while (answer === 'unanswered' && Date.now() < end) {
      const line = next(link, end - Date.now());
      if (line === null) break;
      if (RESTARTS.test(line.trim())) answer = 'acknowledged';
      else if (REFUSED.test(line.trim())) answer = 'refused';
    }
  } catch (e) {
    code = thermiteDevice.codeOf(e, 'no_answer');
  }
  if (answer === 'acknowledged') {
    restarts.set(device.id, Date.now() + RESTART_MS);
    links.drop(device);
    const failed = restartNetwork(device);
    if (failed) console.warn(`${device.id}: answered SET RBT, but its network module was not restarted: ${failed}`);
    else console.info(`${device.id}: answered SET RBT and restarts its network module; it is read again in ${RESTART_MS / 1000} s`);
  } else {
    console.warn(`${device.id}: ${answer === null ? 'could not send' : answer === 'refused' ? 'refused' : 'did not answer'} SET RBT`);
    if (answer === 'unanswered') {
      links.drop(device);
      code = code || 'no_answer';
    }
  }
  if (answer === 'acknowledged' || answer === 'unanswered') unread(device);
  return {answer, code};
}

// How a SET or SET RBT ended, from its session, for the command record
// (thermiteDevice.commandRule) and the routing executor: no byte of it
// written is unsent (dropped by a read-only copy), CMD ERR is the matrix
// declining, and a SET it did not answer may still have switched, as a
// SET RBT it did not answer may have restarted it.
function concluded(result) {
  if (!result) return {outcome:'dropped', code:'invalid'};
  if (result.answer === null) return {outcome:result.code === 'read_only' ? 'dropped' : 'unsent', code:result.code};
  if (result.answer === 'acknowledged') return {outcome:'applied'};
  if (result.answer === 'refused') return {outcome:'refused', code:'refused'};
  return {outcome:'unknown', code:'no_answer'};
}

// "2" (or "2.0") -> 2; anything else -> null.
function inputOf(command) {
  const match = /^(\d+)(\.0+)?$/.exec(command);
  return match ? Number(match[1]) : null;
}

// The house's matrix answers a diagnostic read in about 150 ms, half of
// them later (2026-11-20), so each is given this long.
const DIAGNOSTIC_REPLY_MS = 500;
// Diagnostic reads are asked within this long of a collection's start.
const DIAGNOSTIC_MS = 1000;

// Vendor AC-MX-44X manual p.31 documents these reads, but not their reply
// grammar. Keep bounded port-labelled text, explicitly unparsed; do not
// turn settings, link presence or unsupported replies into video formats.
function diagnosticReply(link, port, query, end) {
  if (Date.now() >= end || link.closed) return null;
  write(link, `GET ${port} ${query}`);
  const until = Math.min(end, Date.now() + DIAGNOSTIC_REPLY_MS);
  const expected = new RegExp(`^(?:A\\d+\\s*)?${port.replace(/(\d+)$/, '0*$1')}\\s+${query.replace(/ /g, '\\s+')}\\b`, 'i');
  for (let count = 0; count < 32 && Date.now() < until; count += 1) {
    const line = next(link, until - Date.now());
    if (line === null || REFUSED.test(line.trim())) return null;
    if (ROUTE.test(line.trim()) || !expected.test(line.trim())) continue;
    // Raw response only. No component names or format values are inferred.
    return line.replace(/[\u0000-\u001f\u007f-\u009f\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]/g, '').trim().slice(0, 192);
  }
  return null;
}

// Whether everything asked on the kept connection has been answered. The
// matrix answers its requests in order, so a diagnostic reply that came too
// late arrives before the answer to GET OUT0 VS, and routes() passes over
// it as it reads only route lines; a late CMD ERR can then not be taken for
// the answer to a later SET. Once fenced, the connection stays open.
function fenced(device, link) {
  return !link.closed && routes(device, link).every((input) => input !== null);
}

let diagnosticStart = 0;
// The reads stop at the first one unanswered, which the next collection
// moves past, and the connection is fenced. Only a matrix that answers
// neither loses the connection, as a failed control session does, and the
// next poll connects again: a slow answer never costs the matrix a session.
function collectSignals(device) {
  if (!SEND) return;
  const observedAt = Date.now(), end = observedAt + DIAGNOSTIC_MS;
  const values = {input:{}, output:{}};
  let found = false;
  try {
    const link = restarting(device) ? null : links.listen(device, thermiteIO.deadline(DIAGNOSTIC_MS + ANSWER_MS));
    if (link) {
      const queries = [];
      for (let port = 1; port <= device.inputs; port += 1) queries.push(['input', port, 'format', 'VID FMT INF']);
      for (let port = 1; port <= device.inputs; port += 1) queries.push(['input', port, 'presence', 'SIG STA']);
      for (let port = 1; port <= device.outputs; port += 1) {
        queries.push(['output', port, 'presence', 'SIG STA'], ['output', port, 'hpd', 'HPD']);
      }
      // A silent optional command cannot starve the later ports forever.
      const start = diagnosticStart % queries.length;
      let count = 0, missed = false;
      for (; count < queries.length && Date.now() + DIAGNOSTIC_REPLY_MS <= end && !link.closed; count += 1) {
        const [side, port, key, query] = queries[(start + count) % queries.length];
        diagnosticStart = (start + count + 1) % queries.length;
        const reply = diagnosticReply(link, `${side === 'input' ? 'IN' : 'OUT'}${port}`, query, end);
        if (reply === null) { missed = true; break; }
        if (!values[side][port]) values[side][port] = {};
        values[side][port][key] = reply;
        if (key === 'hpd') plugged(device, port, reply);
        found = true;
      }
      if (missed && !fenced(device, link)) links.drop(device);
    }
  } catch (_) { links.drop(device); }
  reading(`${device.id}_signal`, JSON.stringify({version:1, observedAt, values:found ? values : {},
    ...(!found ? {status:'unavailable', reason:'no_response'} : {})}));
}

openhab.rules.JSRule({
  id: `avpro_telnet_${DEVICES[0].id}_signal`, name: 'HDMI matrices: read native diagnostic replies',
  triggers: [openhab.triggers.GenericCronTrigger('7/30 * * * * ?')],
  execute: () => { for (const device of DEVICES) collectSignals(device); },
});

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
    name: `${device.id}: switch its outputs, or reboot it, over telnet`,
    device: device.id,
    items: [...outputs.map((output) => `${device.id}_output${output}_input`), `${device.id}_reboot`],
    actor: () => routingLifecycleActor,
    direct: (event) => concluded(directRoutingCommand(device, event)),
  });
}

function directRoutingCommand(device, event) {
  const command = String(event.receivedCommand);
  if (event.itemName === `${device.id}_reboot`) {
    if (command !== 'reboot') {
      console.warn(`${event.itemName}: '${command}' is not reboot, dropped`);
      return;
    }
    return reboot(device);
  }
  const output = Number(/_output(\d+)_input$/.exec(event.itemName)[1]);
  const input = inputOf(command);
  if (input === null || input < 1 || input > device.inputs) {
    console.warn(`${event.itemName}: ${device.id} has no input '${command}', dropped`);
    return;
  }
  return session(device, [output, input]);
}

function routingProvider() {
  const device=DEVICES[0];
  const restart=`${device.id}_reboot`;
  function output(resource) { const match=/_output(\d+)_input$/.exec(resource);return match?Number(match[1]):0; }
  function sample(resource,values) {
    const value=values && values[output(resource)-1];
    if (value===null || value===undefined) throw Error('native_avpro_status_unavailable');
    return thermiteDevice.sample(String(value));
  }
  return {
    // A reboot is an action, not a value to hold; it leaves the routes as they were.
    oneShot:resource=>resource===restart,
    has:(resource,value)=>resource===restart ? String(value)==='reboot' :
      resource.startsWith(device.id+'_') && output(resource)>=1 && output(resource)<=device.outputs &&
      inputOf(String(value))>=1 && inputOf(String(value))<=device.inputs,
    // The SET's own answer decides (send()). A connection opened for it,
    // and its listing, precede it: their failure sent nothing (unsent).
    // CMD ERR is the matrix declining (refused). A SET it acknowledged is
    // applied even when the read after it fails: the actor reads again, and
    // a slow switch is re-read too. A SET it did not answer may still have
    // switched, so it alone is unknown. A SET RBT it answered is applied,
    // with nothing to read back while it restarts.
    execute:(resource,value)=>{
      const result=directRoutingCommand(device,{itemName:resource,receivedCommand:value});
      const done=concluded(result);
      if (done.outcome==='dropped') return {outcome:'unsent', code:done.code};
      if (done.outcome!=='applied' || resource===restart) return done;
      return thermiteDevice.applied(()=>sample(resource,result.shown));
    },
    probe:resource=>sample(resource,session(device).shown),
  };
}

// Set only by the routing lifecycle renderer. Legacy configuration keeps
// this device's existing command rule and protocol ownership.
const ROUTING_EXECUTOR = null;
if (ROUTING_EXECUTOR) routingLifecycleActor = require('thermite-routing-io').actor(ROUTING_EXECUTOR, routingProvider(),
  {readonly: !SEND, record: thermiteDevice.recorder(DEVICES[0].id)});
