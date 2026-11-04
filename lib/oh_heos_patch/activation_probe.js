// Owner-installed one-shot: refresh Karaf features once so a changed HEOS override takes effect, then log
// the sole HEOS bundle's version, state and patched-class hashes against the expected set.
// No Item trigger, Thing, device, credential or network access. Remove the file after its log line appears.
(() => {
  const config = __THERMITE_CONFIG__;
  const Framework = Java.type('org.osgi.framework.FrameworkUtil');
  const Registry = Java.type('org.openhab.core.thing.ThingRegistry');
  const EnumSet = Java.type('java.util.EnumSet');
  const Hex = Java.type('java.util.HexFormat');
  const Digest = Java.type('java.security.MessageDigest');
  const JavaThread = Java.type('java.lang.Thread');
  const context = Framework.getBundle(Registry.class).getBundleContext();
  const result = {version: 1, status: 'failed', verified: false};
  let reference = null;
  function hash(bundle, path) {
    const url = bundle.getEntry(path);
    if (url == null) return null;
    const stream = url.openStream();
    try { return String(Hex.of().formatHex(Digest.getInstance('SHA-256').digest(stream.readAllBytes()))); }
    finally { stream.close(); }
  }
  function heos() {
    return Array.from(context.getBundles()).filter(b => String(b.getSymbolicName()) === 'org.openhab.binding.heos')
      .map(b => ({id: Number(b.getBundleId()), state: Number(b.getState()), version: String(b.getVersion()),
        classes: Object.fromEntries(Object.keys(config.classes).map(path => [path, hash(b, path)]))}));
  }
  function matches(bundles) {
    return bundles.length === 1 && bundles[0].state === 32 && bundles[0].version === config.version
      && Object.keys(config.classes).every(path => bundles[0].classes[path] === config.classes[path]);
  }
  try {
    reference = context.getServiceReference('org.apache.karaf.features.FeaturesService');
    if (reference == null) throw Error('features_service_unavailable');
    const features = context.getService(reference);
    const option = features.getClass().getClassLoader().loadClass('org.apache.karaf.features.FeaturesService$Option');
    features.refreshFeatures(EnumSet.noneOf(option));
    let bundles = heos();
    for (let attempt = 0; attempt < 60 && !matches(bundles); attempt++) {
      JavaThread.sleep(500);
      bundles = heos();
    }
    result.bundles = bundles;
    result.verified = matches(bundles);
    result.status = 'ok';
  } catch (error) {
    result.reason = String(error).slice(0, 200);
  } finally {
    if (reference != null) { try { context.ungetService(reference); } catch (_) {} }
  }
  console.info('thermite-heos-patch ' + JSON.stringify(result));
})();
