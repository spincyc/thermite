// Owner-installed one-shot maintenance; no Item triggers, passwords or device commands.
// refresh is allowed only with every GoogleTV Thing absent from the registry.
(() => {
  const config = __THERMITE_CONFIG__;
  const JavaSystem = Java.type('java.lang.System');
  const Paths = Java.type('java.nio.file.Paths');
  const Files = Java.type('java.nio.file.Files');
  const Write = Java.type('java.nio.file.StandardOpenOption');
  const Move = Java.type('java.nio.file.StandardCopyOption');
  const Framework = Java.type('org.osgi.framework.FrameworkUtil');
  const Registry = Java.type('org.openhab.core.thing.ThingRegistry');
  const Managed = Java.type('org.openhab.core.thing.ManagedThingProvider');
  const Hex = Java.type('java.util.HexFormat');
  const Digest = Java.type('java.security.MessageDigest');
  const context = Framework.getBundle(Registry.class).getBundleContext();
  const references = [];
  function service(name) {
    const ref = context.getServiceReference(name);
    if (ref == null) throw Error('service_unavailable');
    references.push(ref);
    return context.getService(ref);
  }
  function hash(bundle, path) {
    const url = bundle.getEntry(path);
    if (url == null) return null;
    const stream = url.openStream();
    try { return String(Hex.of().formatHex(Digest.getInstance('SHA-256').digest(stream.readNBytes(131072)))); }
    finally { stream.close(); }
  }
  function google(registry) {
    return Array.from(registry.getAll()).filter(t => String(t.getThingTypeUID()) === 'androidtv:googletv');
  }
  function bundles() {
    return Array.from(context.getBundles()).filter(b => String(b.getSymbolicName()) === 'org.openhab.binding.androidtv')
      .map(b => ({id: Number(b.getBundleId()), state: Number(b.getState()), version: String(b.getVersion()),
        manager_sha256: hash(b, 'org/openhab/binding/androidtv/internal/protocol/googletv/GoogleTVConnectionManager.class'),
        handler_sha256: hash(b, 'org/openhab/binding/androidtv/internal/AndroidTVHandler.class'),
        helper_sha256: hash(b, 'org/openhab/binding/androidtv/internal/protocol/googletv/GoogleTVSocketLifecycle.class')}));
  }
  const result = {version: 1, id: config.id, action: config.action, status: 'failed', reason: 'probe_failed'};
  try {
    if (!/^[a-f0-9]{32}$/.test(config.id) || !['inspect', 'refresh'].includes(config.action)) throw Error('invalid_request');
    const registry = service('org.openhab.core.thing.ThingRegistry');
    const managed = service('org.openhab.core.thing.ManagedThingProvider');
    const current = google(registry);
    const editable = new Set(Array.from(managed.getAll()).map(t => String(t.getUID())));
    result.things = current.map(t => ({uid: String(t.getUID()), editable: editable.has(String(t.getUID())),
      disabled: String(t.getStatusInfo().getStatusDetail()) === 'DISABLED'})).sort((a, b) => a.uid.localeCompare(b.uid));
    if (config.action === 'refresh') {
      if (current.length !== 0) { result.reason = 'google_things_present'; throw Error('refused'); }
      const features = service('org.apache.karaf.features.FeaturesService');
      const loader = features.getClass().getClassLoader();
      const option = loader.loadClass('org.apache.karaf.features.FeaturesService$Option');
      const options = Java.type('java.util.EnumSet').noneOf(option);
      features.refreshFeatures(options);
      if (google(registry).length !== 0) { result.reason = 'google_things_appeared'; throw Error('refused'); }
      result.refreshed = true;
    }
    result.bundles = bundles();
    result.status = 'ok'; result.reason = 'completed';
  } catch (_) { /* No native exception/body/config/credential is copied to the report. */ }
  finally {
    for (const reference of references) { try { context.ungetService(reference); } catch (_) {} }
  }
  const folder = Paths.get(String(JavaSystem.getProperty('openhab.userdata')), 'thermite', 'maintenance', config.id);
  const target = folder.resolve('report.json'), temporary = folder.resolve('report.tmp');
  if (!Files.isDirectory(folder) || Files.isSymbolicLink(folder) || Files.exists(target) || Files.exists(temporary)) return;
  try {
    Files.writeString(temporary, JSON.stringify(result), Write.CREATE_NEW, Write.WRITE);
    Files.move(temporary, target, Move.ATOMIC_MOVE);
  } catch (_) { /* The caller times out rather than accept an untrusted/stale report. */ }
})();
