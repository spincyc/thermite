// Native HEOS transport only. Never select media, change groups, inputs,
// power or volume. HTTP contains exactly two read-only receiver queries.
'use strict';
const openhab = require('openhab');
const ChannelUID = Java.type('org.openhab.core.thing.ChannelUID');
const StringType = Java.type('org.openhab.core.library.types.StringType');
const JavaURL = Java.type('java.net.URL');
const JavaProxy = Java.type('java.net.Proxy');
const StandardCharsets = Java.type('java.nio.charset.StandardCharsets');
const JString = Java.type('java.lang.String');
const ByteArrayOutputStream = Java.type('java.io.ByteArrayOutputStream');
const System = Java.type('java.lang.System');
const Bytes = Java.type('byte[]');
const ByteArrayInputStream = Java.type('java.io.ByteArrayInputStream');
const DocumentBuilderFactory = Java.type('javax.xml.parsers.DocumentBuilderFactory');
const DefaultHandler = Java.type('org.xml.sax.helpers.DefaultHandler');
const XMLConstants = Java.type('javax.xml.XMLConstants');
const DEVICES = null;
const SEND = true;
// Both reads share this deadline.
const READ_MS = 2500;
const MAX_BODY = 8192;
// Receiver firmware answers compact or encoding-less AppCommand XML with an
// empty <rx> (HTTP 200). Keep the routing executor's verified serialization
// (lifecycle_io.js): one command per request, UTF-8 declaration, CRLF lines.
function appCommand(name) {
  return '<?xml version="1.0" encoding="utf-8"?>\r\n<tx>\r\n<cmd id="1">' + name + '</cmd>\r\n</tx>\r\n';
}
const COMMANDS = Object.freeze({ previous: 'PREVIOUS', play: 'PLAY', pause: 'PAUSE', next: 'NEXT' });

function state(name) { return String(openhab.items.getItem(name).state); }
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
// Whether a reply is exactly <rx><cmd><name>expected</name>...</cmd></rx>.
function agrees(bytes, name, expected) {
  const factory = DocumentBuilderFactory.newInstance();
  factory.setFeature(XMLConstants.FEATURE_SECURE_PROCESSING, true);
  factory.setFeature('http://apache.org/xml/features/disallow-doctype-decl', true);
  factory.setFeature('http://xml.org/sax/features/external-general-entities', false);
  factory.setFeature('http://xml.org/sax/features/external-parameter-entities', false);
  factory.setFeature('http://apache.org/xml/features/nonvalidating/load-external-dtd', false);
  factory.setAttribute(XMLConstants.ACCESS_EXTERNAL_DTD, '');
  factory.setAttribute(XMLConstants.ACCESS_EXTERNAL_SCHEMA, '');
  factory.setXIncludeAware(false); factory.setExpandEntityReferences(false);
  const builder = factory.newDocumentBuilder();
  builder.setErrorHandler(new DefaultHandler()); // Never echo a raw XML error.
  const input = new ByteArrayInputStream(bytes);
  try {
    const root = builder.parse(input).getDocumentElement();
    if (String(root.getNodeName()) !== 'rx') return false;
    const replies = elements(root);
    return replies.length === 1 && String(replies[0].getNodeName()) === 'cmd' && field(replies[0], name) === expected;
  } finally { input.close(); }
}
// One AppCommand reply's bytes, or null for no complete bounded answer.
function query(device, name, deadline) {
  if (System.nanoTime() >= deadline) return null;
  let connection = null, stream = null, output = null;
  try {
    connection = new JavaURL(device.readURL).openConnection(JavaProxy.NO_PROXY);
    connection.setConnectTimeout(500); connection.setReadTimeout(250);
    connection.setInstanceFollowRedirects(false); connection.setUseCaches(false);
    connection.setRequestMethod('POST'); connection.setDoOutput(true);
    connection.setRequestProperty('Content-Type', 'application/xml');
    connection.setRequestProperty('Accept', 'application/xml');
    const request = new JString(appCommand(name)).getBytes(StandardCharsets.UTF_8);
    connection.setFixedLengthStreamingMode(request.length);
    output = connection.getOutputStream(); output.write(request); output.close(); output = null;
    if (connection.getResponseCode() !== 200 || connection.getContentLengthLong() > MAX_BODY) return null;
    stream = connection.getInputStream();
    const collected = new ByteArrayOutputStream(), buffer = new Bytes(1024);
    while (System.nanoTime() < deadline) {
      const count = stream.read(buffer, 0, Math.min(buffer.length, MAX_BODY + 1 - collected.size()));
      if (count < 0) return System.nanoTime() < deadline ? collected.toByteArray() : null;
      collected.write(buffer, 0, count);
      if (collected.size() > MAX_BODY) return null;
    }
    return null;
  } catch (_) {
    return null;
  } finally {
    if (output) { try { output.close(); } catch (_) {} }
    if (stream) { try { stream.close(); } catch (_) {} }
    if (connection) connection.disconnect();
  }
}
function answers(device, name, point, expected, deadline) {
  const bytes = query(device, name, deadline);
  try { return bytes !== null && agrees(bytes, point, expected); } catch (_) { return false; }
}
function freshReceiver(device) {
  const deadline = System.nanoTime() + READ_MS * 1000000;
  return answers(device, 'GetAllZonePowerStatus', 'zone1', 'ON', deadline)
    && answers(device, 'GetSourceStatus', 'source', 'NET', deadline);
}
function press(device, button) {
  if (!SEND || !Object.prototype.hasOwnProperty.call(COMMANDS, button)) return;
  // Native DenonMarantz suppresses unchanged Item updates. They are NOT
  // acquisition timestamps: obtain the receiver's state for THIS command.
  if (!ready(device) || !freshReceiver(device) || !ready(device)) {
    console.warn(`${device.id}: HEOS transport needs fresh main-zone ON and HEOS Music reports`);
    return;
  }
  const thing = openhab.things.getThing(device.thing), handler = thing && thing.rawThing.getHandler();
  if (!handler) return;
  try { handler.handleCommand(new ChannelUID(`${device.thing}:Control`), new StringType(COMMANDS[button])); }
  catch (_) { console.warn(`${device.id}: HEOS transport was not accepted`); }
  // Only the native HEOS binding supplies subsequent PLAY/PAUSE readback.
}
for (const device of DEVICES) {
  openhab.rules.JSRule({
    id: `heos_${device.id}_transport`, name: `${device.id}: guarded HEOS transport`,
    triggers: [openhab.triggers.ItemCommandTrigger(`${device.id}_key`),
      openhab.triggers.ThingStatusChangeTrigger(device.thing),
      openhab.triggers.ThingStatusChangeTrigger(device.receiver)],
    execute: event => {
      if (event.itemName === `${device.id}_key`) press(device, String(event.receivedCommand));
      else if (!online(device.thing) || !online(device.receiver)) openhab.items.getItem(`${device.id}_heos_playback`).postUpdate('UNDEF');
    },
  });
}
