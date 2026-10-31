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
const DEVICES = null;
const SEND = true;

const openhab = require('openhab');

const Socket = Java.type('java.net.Socket');
const InetSocketAddress = Java.type('java.net.InetSocketAddress');
const BufferedReader = Java.type('java.io.BufferedReader');
const InputStreamReader = Java.type('java.io.InputStreamReader');
const OutputStreamWriter = Java.type('java.io.OutputStreamWriter');
const StandardCharsets = Java.type('java.nio.charset.StandardCharsets');

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
  try {
    link.socket.close();
  } catch (e) {
    // Closing a broken socket has nothing left to report.
  }
}

// The next line within `ms`, or null: none came, or the matrix closed the
// connection (link.closed).
function next(link, ms) {
  if (link.closed) return null;
  try {
    link.socket.setSoTimeout(Math.max(1, Math.round(ms)));
    const line = link.reader.readLine();
    if (line === null) link.closed = true;
    return line === null ? null : String(line);
  } catch (e) {
    return null;
  }
}

function write(link, line) {
  try {
    link.writer.write(`${line}\r\n`);
    link.writer.flush();
  } catch (e) {
    throw new Error(`cannot send ${line} (${e})`);
  }
}

function open(device) {
  const socket = new Socket();
  const link = { socket, closed: false };
  try {
    socket.connect(new InetSocketAddress(device.host, device.port), CONNECT_MS);
    link.reader = new BufferedReader(new InputStreamReader(socket.getInputStream(), StandardCharsets.US_ASCII));
    link.writer = new OutputStreamWriter(socket.getOutputStream(), StandardCharsets.US_ASCII);
  } catch (e) {
    close(link);
    throw new Error(`cannot connect to ${device.host}:${device.port} (${e})`);
  }
  const end = Date.now() + LISTING_MS;
  let line = '';
  while (line !== null && Date.now() < end) line = next(link, QUIET_MS);
  if (link.closed) {
    close(link);
    throw new Error('closed the connection');
  }
  return link;
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

function session(device, commands) {
  let link = null;
  try {
    link = open(device);
    for (const [output, input] of commands) send(device, link, output, input);
    const shown = routes(device, link);
    if (shown.every((input) => input === null)) throw new Error('does not answer GET OUT0 VS');
    if (down.delete(device.id)) console.info(`${device.id}: answering over telnet again`);
    for (const [output, input] of commands) {
      if (SEND && shown[output - 1] !== input) {
        console.warn(`${device.id}: output ${output} shows input ${shown[output - 1]} after SET OUT${output} VS IN${input}`);
      }
    }
    show(device, shown);
  } catch (e) {
    if (!down.has(device.id)) {
      down.add(device.id);
      console.warn(`${device.id}: ${e && e.message ? e.message : e}; its Items are UNDEF until it answers`);
    }
    show(device, new Array(device.outputs).fill(null));
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
  id: 'avpro_telnet_poll',
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
      const output = Number(/_output(\d+)_input$/.exec(event.itemName)[1]);
      const command = String(event.receivedCommand);
      const input = inputOf(command);
      if (input === null || input < 1 || input > device.inputs) {
        console.warn(`${event.itemName}: ${device.id} has no input '${command}', dropped`);
        return;
      }
      session(device, [[output, input]]);
    },
  });
}
