// Native HEOS transport only. Never select media, change groups, inputs,
// power or volume. HTTP contains exactly two read-only receiver queries.
// `reboot` to a receiver's <id>_reboot sends heos://system/reboot to its
// own HEOS CLI: the bridge's connection reaches only the receiver it is on.
'use strict';
const openhab = require('openhab');
const ChannelUID = Java.type('org.openhab.core.thing.ChannelUID');
const StringType = Java.type('org.openhab.core.library.types.StringType');
const StringReader = Java.type('java.io.StringReader');
const InputSource = Java.type('org.xml.sax.InputSource');
const DocumentBuilderFactory = Java.type('javax.xml.parsers.DocumentBuilderFactory');
const DefaultHandler = Java.type('org.xml.sax.helpers.DefaultHandler');
const XMLConstants = Java.type('javax.xml.XMLConstants');
// Each receiver's player on the house's one bridge (heos.py). The binding marks
// a player OFFLINE while its bridge is, so its status covers the bridge's too.
// Each rendered receiver gets its own context so a reboot never blocks another.
const DEVICES = null;
const SEND = true;
// THERMITE_BOUNDED_IO
// THERMITE_DEVICE
// Both reads share this deadline.
const READ_MS = 2500;
const MAX_BODY = 8192;
// The HEOS CLI's port, and how long a reboot's connection and answer take.
const CLI_PORT = 1255;
const CONNECT_MS = 1000;
const ANSWER_MS = 3000;
const MAX_LINE = 1024;
let xmlFactory = null;
// Receiver firmware answers compact or encoding-less AppCommand XML with an
// empty <rx> (HTTP 200). Keep the routing executor's verified serialization
// (denonmarantz_http.js): one command per request, UTF-8 declaration, CRLF lines.
function appCommand(name) {
  return '<?xml version="1.0" encoding="utf-8"?>\r\n<tx>\r\n<cmd id="1">' + name + '</cmd>\r\n</tx>\r\n';
}
const COMMANDS = Object.freeze({ previous: 'PREVIOUS', play: 'PLAY', pause: 'PAUSE', next: 'NEXT' });

function state(name) {
  const item = openhab.items.getItem(name, true);
  return item === null ? 'NULL' : String(item.state);
}
function online(uid) { return String((openhab.things.getThing(uid) || {}).status) === 'ONLINE'; }
function ready(device) {
  return online(device.receiver) && online(device.thing)
    && state(`${device.id}_main_power`) === 'ON' && state(`${device.id}_main_input`) === 'NET';
}
function elements(node) {
  const children = node.getChildNodes(), out = [];
  for (let i = 0; i < children.getLength(); i++) if (children.item(i).getNodeType() === 1) out.push(children.item(i));
  return out;
}
function field(node, name) {
  const found = elements(node).filter(child => String(child.getNodeName()) === name);
  return found.length === 1 && elements(found[0]).length === 0 ? String(found[0].getTextContent()).trim() : null;
}
// Reuse one hardened DocumentBuilderFactory per script context.
function documentBuilder() {
  if (xmlFactory === null) {
    const factory = DocumentBuilderFactory.newInstance();
    factory.setFeature(XMLConstants.FEATURE_SECURE_PROCESSING, true);
    factory.setFeature('http://apache.org/xml/features/disallow-doctype-decl', true);
    factory.setFeature('http://xml.org/sax/features/external-general-entities', false);
    factory.setFeature('http://xml.org/sax/features/external-parameter-entities', false);
    factory.setFeature('http://apache.org/xml/features/nonvalidating/load-external-dtd', false);
    factory.setAttribute(XMLConstants.ACCESS_EXTERNAL_DTD, '');
    factory.setAttribute(XMLConstants.ACCESS_EXTERNAL_SCHEMA, '');
    factory.setXIncludeAware(false); factory.setExpandEntityReferences(false);
    xmlFactory = factory;
  }
  const builder = xmlFactory.newDocumentBuilder();
  builder.setErrorHandler(new DefaultHandler()); // Never echo a raw XML error.
  return builder;
}
// Whether a reply is exactly <rx><cmd><name>expected</name>...</cmd></rx>.
function agrees(xml, name, expected) {
  const root = documentBuilder().parse(new InputSource(new StringReader(xml))).getDocumentElement();
  if (String(root.getNodeName()) !== 'rx') return false;
  const replies = elements(root);
  return replies.length === 1 && String(replies[0].getNodeName()) === 'cmd' && field(replies[0], name) === expected;
}
// One AppCommand reply's XML, or null for no complete bounded answer.
function query(device, name, end) {
  try {
    const timeout = thermiteIO.remaining(end);
    const response = thermiteIO.http(
      device.readURL, 'POST', appCommand(name),
      { 'Content-Type': 'application/xml', Accept: 'application/xml' },
      timeout, Math.min(500, timeout),
    );
    if (Number(response.getStatus()) !== 200) return null;
    const body = String(response.getContentAsString());
    return body.length <= MAX_BODY ? body : null;
  } catch (_) {
    return null;
  }
}
function answers(device, name, point, expected, end) {
  const xml = query(device, name, end);
  try { return xml !== null && agrees(xml, point, expected); } catch (_) { return false; }
}
function freshReceiver(device) {
  const end = thermiteIO.deadline(READ_MS);
  return answers(device, 'GetAllZonePowerStatus', 'zone1', 'ON', end)
    && answers(device, 'GetSourceStatus', 'source', 'NET', end);
}
// How a transport key ended, for the command record
// (thermiteDevice.commandRule): the binding acknowledges nothing, so a key
// its handler took is sent.
function press(device, button) {
  if (!Object.prototype.hasOwnProperty.call(COMMANDS, button)) return { outcome: 'dropped', code: 'invalid' };
  if (!SEND) return { outcome: 'dropped', code: 'read_only' };
  // Native DenonMarantz suppresses unchanged Item updates. They are NOT
  // acquisition timestamps: obtain the receiver's state for THIS command.
  if (!ready(device) || !freshReceiver(device) || !ready(device)) {
    console.warn(`${device.id}: HEOS transport needs fresh main-zone ON and HEOS Music reports`);
    return { outcome: 'unsent', code: online(device.receiver) && online(device.thing) ? 'not_ready' : 'unreachable' };
  }
  const thing = openhab.things.getThing(device.thing), handler = thing && thing.rawThing.getHandler();
  if (!handler) return { outcome: 'unsent', code: 'unreachable' };
  try { handler.handleCommand(new ChannelUID(`${device.thing}:Control`), new StringType(COMMANDS[button])); }
  catch (_) {
    console.warn(`${device.id}: HEOS transport was not accepted`);
    return { outcome: 'unknown', code: 'other' };
  }
  // Only the native HEOS binding supplies subsequent PLAY/PAUSE readback.
  return { outcome: 'sent' };
}
// heos://system/reboot to the receiver's HEOS CLI, and what it answered,
// logged: it restarts, and neither its HTTP API nor HEOS answers for a while.
// How it ended, for the command record.
function reboot(device) {
  if (!SEND) {
    console.info(`${device.id}: SEND is false, not sending heos://system/reboot`);
    return { outcome: 'dropped', code: 'read_only' };
  }
  let link = null, sent = false;
  try {
    const end = thermiteIO.deadline(CONNECT_MS + ANSWER_MS);
    link = thermiteIO.open(device.host, CLI_PORT, CONNECT_MS, end);
    thermiteIO.write(link, 'heos://system/reboot\r\n');
    sent = true;
    const raw = thermiteIO.line(link, ANSWER_MS);
    let result = null;
    if (raw !== null && raw.length <= MAX_LINE) {
      try { result = JSON.parse(raw).heos.result; } catch (_) {}
    }
    console.warn(`${device.id}: sent heos://system/reboot; it answered ` +
      (typeof result === 'string' ? result.slice(0, 40) : 'something else'));
    return result === 'success' ? { outcome: 'applied' }
      : typeof result === 'string' ? { outcome: 'refused', code: 'refused' } : { outcome: 'unknown', code: 'protocol' };
  } catch (e) {
    console.warn(`${device.id}: ${sent ? 'sent heos://system/reboot; no answer' : 'could not send heos://system/reboot'}` +
      `: ${e && e.message ? e.message : e}`);
    return sent ? { outcome: 'unknown', code: 'no_answer' } : { outcome: 'unsent', code: 'unreachable' };
  } finally {
    if (link) thermiteDevice.close(link);
  }
}
for (const device of DEVICES) {
  // A reboot holds its rule for seconds: the keys have their own.
  thermiteDevice.commandRule({
    id: `heos_${device.id}_reboot`, name: `${device.id}: reboot over HEOS`, device: device.id,
    items: [`${device.id}_reboot`],
    direct: event => {
      const command = String(event.receivedCommand);
      if (command === 'reboot') return reboot(device);
      console.warn(`${device.id}_reboot: '${command}' is not reboot, dropped`);
      return { outcome: 'dropped', code: 'invalid' };
    },
  });
  thermiteDevice.commandRule({
    id: `heos_${device.id}_transport`, name: `${device.id}: guarded HEOS transport`, device: device.id,
    items: [`${device.id}_key`],
    handles: (_, command) => Object.prototype.hasOwnProperty.call(COMMANDS, command),
    direct: event => press(device, String(event.receivedCommand)),
  });
  openhab.rules.JSRule({
    id: `heos_${device.id}_offline`, name: `${device.id}: no HEOS playback state while offline`,
    triggers: [openhab.triggers.ThingStatusChangeTrigger(device.thing),
      openhab.triggers.ThingStatusChangeTrigger(device.receiver)],
    execute: () => {
      if (!online(device.thing) || !online(device.receiver)) {
        const item = openhab.items.getItem(`${device.id}_heos_playback`, true);
        if (item !== null) item.postUpdate('UNDEF');
      }
    },
  });
}
