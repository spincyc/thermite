const fs = require('fs'), vm = require('vm'), assert = require('assert');
const template = fs.readFileSync(process.argv[2], 'utf8');
function run({action = 'inspect', things = [], appears = false, exists = false} = {}) {
  const writes = [], calls = [];
  const registry = {getAll: () => things};
  const managed = {getAll: () => things.filter(t => t.editable)};
  const services = {
    'org.openhab.core.thing.ThingRegistry': registry,
    'org.openhab.core.thing.ManagedThingProvider': managed,
    'org.apache.karaf.features.FeaturesService': {
      getClass: () => ({getClassLoader: () => ({loadClass: name => name})}),
      refreshFeatures: options => { assert.equal(options, 'empty-enum-options'); calls.push('refresh'); if (appears) things.push(thing('late')); }
    }
  };
  const bundles = [{getSymbolicName: () => 'org.openhab.binding.androidtv', getState: () => 32,
    getVersion: () => '5.2.3.thermite1', getBundleId: () => 7, getEntry: () => null}];
  const context = {getServiceReference: name => name, getService: name => services[name], ungetService() {}, getBundles: () => bundles};
  const path = value => ({resolve: name => path(value + '/' + name), toString: () => value});
  const types = {
    'java.lang.System': {getProperty: () => '/synthetic/userdata'},
    'java.nio.file.Paths': {get: (...pieces) => path(pieces.join('/'))},
    'java.nio.file.Files': {isDirectory: () => true, isSymbolicLink: () => false, exists: () => exists,
      writeString: (target, body) => { writes.push(JSON.parse(body)); }, move() {}},
    'java.nio.file.StandardOpenOption': {}, 'java.nio.file.StandardCopyOption': {},
    'org.osgi.framework.FrameworkUtil': {getBundle: () => ({getBundleContext: () => context})},
    'org.openhab.core.thing.ThingRegistry': {}, 'org.openhab.core.thing.ManagedThingProvider': {},
    'java.util.HexFormat': {}, 'java.security.MessageDigest': {},
    'java.util.EnumSet': {noneOf: type => { assert.equal(type, 'org.apache.karaf.features.FeaturesService$Option'); return 'empty-enum-options'; }}
  };
  const source = template.replace('__THERMITE_CONFIG__', JSON.stringify({id: '0'.repeat(32), action}));
  vm.runInNewContext(source, {Java: {type: name => { assert(Object.hasOwn(types, name), 'Unexpected Java authority: ' + name); return types[name]; }}}, {timeout: 1000});
  return {writes, calls};
}
function thing(id, disabled = false, editable = false) {
  return {editable, getThingTypeUID: () => 'androidtv:googletv', getUID: () => 'androidtv:googletv:' + id,
    getStatusInfo: () => ({getStatusDetail: () => disabled ? 'DISABLED' : 'NONE'})};
}
let value = run({things: [thing('b', true), thing('a', false, true)]});
assert.equal(value.writes[0].status, 'ok');
assert.equal(value.writes[0].things[0].editable, true);
assert.equal(value.writes[0].things[1].disabled, true);
assert.equal(value.calls.length, 0);
value = run({action: 'refresh', things: [thing('present')]});
assert.equal(value.writes[0].status, 'failed');
assert.equal(value.writes[0].reason, 'google_things_present');
assert.equal(value.calls.length, 0);
value = run({action: 'refresh'});
assert.equal(value.calls.length, 1);
assert.equal(value.writes[0].status, 'ok');
assert.equal(value.writes[0].refreshed, true);
value = run({action: 'refresh', appears: true});
assert.equal(value.writes[0].status, 'failed');
assert.equal(value.writes[0].reason, 'google_things_appeared');
value = run({exists: true});
assert.equal(value.writes.length, 0);
console.log('maintenance one-shot: inspect/disabled/editable/zero-Thing refresh/late-Thing/stale-report guards PASS');
