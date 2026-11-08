// A Denon or Marantz receiver's routing actor: under the routing lifecycle
// it powers and switches the receiver's zones in order, over the receiver's
// HTTP API, and reads them back (denonmarantz_http.py renders one per
// receiver under control). The binding keeps polling every Item; this rule
// only sends the lifecycle's and the explicit commands to these fields.
//
// The receiver's address is its Thing's current configuration, so a Thing
// pointed elsewhere (bin/thermite test) takes the actor with it. Reads use
// the verified wire format: the main zone through AppCommand.xml, any other
// zone through its XmlStatusLite page. Main-zone Off is ZMOFF, never the
// chassis PWSTANDBY, which would also stop zone 2.
'use strict';
const SEND = true;
const openhab = require('openhab');
// A source the receiver reports under another name than the one sent
// (denonmarantz_http.INPUT_ALIASES).
const INPUT_ALIASES = null;

// The receiver's provider for the generic actor (thermite-routing-io) of
// its fields.
function routingProvider(routingIO, fields) {
  function has(resource, value) { return !!fields[resource] && fields[resource].allowed.includes(String(value)); }
  function base(field) {
    const thing = routingIO.http.thing(field.thing);
    const host = thing.get('host'), port = thing.get('httpPort');
    return 'http://' + String(host === null || host === undefined ? '' : host) + ':'
      + Number(port === null || port === undefined ? 8080 : port);
  }
  // One element's text at `path`. A bounded (thermiteIO) but malformed or
  // ambiguous document is not physical evidence: no DTD, no external
  // entity or schema, secure processing, and exactly one element at each
  // step, whose last holds text only.
  function xmlField(xml, path) {
    const factory = Java.type('javax.xml.parsers.DocumentBuilderFactory').newInstance();
    factory.setFeature('http://apache.org/xml/features/disallow-doctype-decl', true);
    factory.setFeature('http://xml.org/sax/features/external-general-entities', false);
    factory.setFeature('http://xml.org/sax/features/external-parameter-entities', false);
    factory.setFeature('http://javax.xml.XMLConstants/feature/secure-processing', true);
    factory.setAttribute('http://javax.xml.XMLConstants/property/accessExternalDTD', '');
    factory.setAttribute('http://javax.xml.XMLConstants/property/accessExternalSchema', '');
    factory.setXIncludeAware(false);
    factory.setExpandEntityReferences(false);
    const parser = factory.newDocumentBuilder();
    parser.setErrorHandler(new (Java.type('org.xml.sax.helpers.DefaultHandler'))());
    const document = parser.parse(new (Java.type('org.xml.sax.InputSource'))(new (Java.type('java.io.StringReader'))(xml)));
    let node = document;
    for (const name of path) {
      const children = node.getChildNodes(), found = [];
      for (let i = 0; i < children.getLength(); ++i) {
        const child = children.item(i);
        if (child.getNodeType() === 1 && String(child.getNodeName()) === name) found.push(child);
      }
      if (found.length !== 1) throw Error('executor_status_unavailable');
      node = found[0];
    }
    const children = node.getChildNodes();
    for (let i = 0; i < children.getLength(); ++i) if (children.item(i).getNodeType() === 1) throw Error('executor_status_unavailable');
    return String(node.getTextContent()).trim();
  }
  function read(field) {
    const baseURL = base(field);
    let value;
    if (field.zone === 1) {
      const name = field.point === 'power' ? 'GetAllZonePowerStatus' : 'GetSourceStatus';
      // Receiver firmware can return HTTP 200 with an empty <rx> for compact
      // XML or a missing encoding declaration. Keep the verified wire format.
      const body = '<?xml version="1.0" encoding="utf-8"?>\r\n<tx>\r\n<cmd id="1">' + name + '</cmd>\r\n</tx>\r\n';
      const xml = routingIO.http.request(baseURL + '/goform/AppCommand.xml', 'POST', body, 'application/xml');
      value = xmlField(xml, ['rx', 'cmd', field.point === 'power' ? 'zone1' : 'source']);
    } else {
      const xml = routingIO.http.request(baseURL + '/goform/formZone' + field.zone + '_Zone' + field.zone + 'XmlStatusLite.xml', 'GET', null, null);
      value = xmlField(xml, ['item', field.point === 'power' ? 'Power' : 'InputFuncSelect', 'value']);
    }
    if (Object.prototype.hasOwnProperty.call(INPUT_ALIASES, value)) value = INPUT_ALIASES[value];
    return { value, fresh: true, basis: 'owned_native_read', observedAt: Date.now() };
  }
  function probe(resource) {
    const sample = read(fields[resource]);
    if (!has(resource, sample.value)) throw Error('executor_status_outside_domain');
    openhab.items.getItem(resource).postUpdate(sample.value);
    return sample;
  }
  function execute(resource, value) {
    // Everything before the request is local: a failure there sent nothing.
    let field, baseURL;
    try {
      if (!has(resource, value)) throw Error('invalid_executor_value');
      field = fields[resource]; baseURL = base(field);
    } catch (error) { return { outcome: 'unsent', ...routingIO.causeOf(error) }; }
    const command = field.zone === 1 ? (field.point === 'power' ? (value === 'ON' ? 'ZMON' : 'ZMOFF') : 'SI' + value) :
      'Z' + field.zone + value;
    const sent = routingIO.http.mutate(baseURL + '/goform/formiPhoneAppDirect.xml?' + encodeURIComponent(command), 'GET', null, null);
    if (sent.outcome) return sent;
    // The receiver answered the command request; its state follows.
    return { outcome: 'applied', sample: routingIO.http.after(() => {
      const observed = read(field);
      openhab.items.getItem(resource).postUpdate(observed.value);
      return observed;
    }) };
  }
  return { has, execute, probe };
}

// Set only by the routing lifecycle renderer (lifecycle_io.py); without it
// this rule does nothing.
const ROUTING_EXECUTOR = null;
if (ROUTING_EXECUTOR) {
  const routingIO = require('thermite-routing-io');
  routingIO.explicitControls(ROUTING_EXECUTOR, routingIO.actor(ROUTING_EXECUTOR, routingProvider(routingIO, ROUTING_EXECUTOR.fields), {readonly: !SEND}));
}
