# Pinned Android TV connection repair

This builds a narrow derivative of the openHAB Android TV **5.2.3** binding,
with bundle version **5.2.3.thermite1**. It does not upgrade the runtime or
replace Thermite's integration, pairing rules, protocol packets, trust
manager, certificates, keystores, channel definitions or translations.

The upstream connection method holds its lifecycle monitor during TCP and
TLS, publishes the socket only after the handshake, and has no explicit
connection timeout. Disposal needs that same monitor. A blocked socket can
therefore retain a shared `thingHandler` worker even after its Future was
cancelled. Replaced PIN children and debug START managers can also lose
their owning references. The source patch repairs those ownership paths.

The new socket helper publishes an unconnected socket before network I/O,
permits one current attempt per manager, and uses a 5-second TCP timeout,
10-second handshake read timeout and **15-second absolute opening deadline**.
The absolute deadline runs independently of `thingHandler`; a trickling TLS
peer cannot keep renewing it. The single daemon deadline worker removes
cancelled jobs, times out when idle, and has no binding context classloader.
Established connections return to the original unlimited reader timeout.
Disposal closes in-flight sockets without waiting for their I/O monitor.
Late failures cannot close a replacement; retry generations reject cancelled
callbacks. Repeated retries replace one tracked job. Parent disposal and PIN
replacement dispose every child, including a child created during disposal.
The existing optional shim listener is tracked and closed on disposal, with
its accept and TLS work outside the lifecycle monitor.

`services/threadpools.cfg` remains a separate defensive capacity mitigation.
The patch fixes the identified future connection retention paths; it cannot
retire threads already running the old classes or promise that unrelated
binding operations never block the shared scheduler.

## Reproduce the artifact

Use a Java 21 executable, including the JRE already cached by
`thermite sandbox fetch`. No Maven or JDK installation is required. The
official Eclipse compiler runs on that JRE. `inputs.json` pins every
download by SHA-256: original bundle, both unmodified upstream source files,
compiler and compile-time annotations. An aggregate hash also pins the
complete distribution's 338 runtime dependency JARs; a mixed or newer SDK is
refused before compilation. The bundle digest also matches the
official release repository's checksum. Downloads use the existing sandbox
checksum/cache helper and require the explicit `--fetch` flag.

From this area's root, with owned directories under `.scratch`:

```sh
PYTHONPATH=lib python3 -m oh_androidtv_patch \
  --java /absolute/path/to/java21/bin/java \
  --runtime /absolute/path/to/openhab-5.2.3/runtime \
  --work /absolute/path/to/.scratch/androidtv-inputs \
  --output /absolute/path/to/.scratch/androidtv-staged \
  --test-sources tests/androidtv_patch --fetch
```

Omit `--fetch` to require previously downloaded, hash-verified inputs. The
build applies exact patches with zero fuzz, compiles only the two patched
classes and their small helper, and refuses an unexpected class set. All
other bundle entries are preserved byte-for-byte apart from signature
metadata and the main manifest's version/patch marker. Upstream's shaded
signature files cannot authenticate a derived bundle and are removed, along
with obsolete entry digests. The output is a locally built derivative,
whose trust anchor is the pins in `inputs.json`; it carries no
upstream signature.
Upstream licenses and notices remain included. ZIP ordering, timestamps and
compression are deterministic; identical inputs produce identical bytes.
`build.json` records source, patch, compiler, dependency and artifact hashes.

The build must produce the classes in `classes/` and the bundle content
`inputs.json` pins (`classes`, `content_sha256`), or it fails: what an
install puts into a runtime is what this source compiles to. After a
reviewed change to a patch or an input, `--write-classes` (with
`--test-sources`, so only a gated build is recorded) replaces `classes/` and
those pins. The classes are committed so that an install needs no compiler,
no source download and no `patch`: it needs only the pinned upstream bundle.
The JAR itself is not pinned by its own SHA-256, because its compressed bytes
depend on the host's zlib; its content is.

The build and an install of a house with the binding refuse when
`versions.json` pins any other openHAB release than 5.2.3, and say what to
do: retire the repair once an upstream release contains the fix
(`retire_when` in `inputs.json`; record the pull request there once it is
filed), or rebase it. `tests/test_androidtv_patch.py` holds the pin in
`make check`.

The executable offline Java tests load the **compiled production manager**.
Only its handler/translation boundary and socket factory are fakes. PKI and
OSGi initialization are deliberately bypassed in the test fixture. A JVM
security guard rejects all real network connections and listeners. Tests
exercise non-interruptible connect/handshake cancellation, successful quiet
connections and reader/sender teardown, timeout and retry behavior, already
dequeued retry callbacks, stale attempts, PIN-child replacement, a child
created during disposal, stale child completion, and an independent absolute
deadline. A second small Java test uses the actual pinned Karaf4.4.11 and
Pax2.7.1 parser/resolver to match the override and resolve the exact artifact
from a private file repository, with socket/listener access forbidden and an
owned hosts-file resolver/settings/home/cache. These tests
do not boot a native runtime and do not prove physical-device pairing.

## Installation

`thermite install`, `upgrade` and `rollback` install this repair for every
house whose `conf/services/addons.cfg` lists `binding-androidtv`; no other house gets
it, and nobody installs it by hand. It is part of the install transaction
(`lib/oh_addons.py`, the steps *Add-on files*, *Add-on repository* and
*Binding repairs*):

1. **Before anything stops**, the transaction refuses a `versions.json` that
   pins another openHAB release than 5.2.3 (the message says how to
   retire or rebase the repair), and fetches and verifies the pinned upstream
   bundle with the house's other add-on files.
2. **With openHAB stopped, after its own offline update**, root rebuilds the
   bundle from that verified file and the classes in `classes/`, each checked
   against `inputs.json`. The result must have the pinned content
   (`content_sha256`: every entry but the manifest, by name and SHA-256) and
   the manifest's `Bundle-Version: 5.2.3.thermite1` and
   `Thermite-Patch: googletv-lifecycle-v1`. No compiler runs on the host. The bundle goes
   into the runtime's own repository, `userdata/thermite/maven`, at

   ```text
   org/openhab/addons/bundles/org.openhab.binding.androidtv/5.2.3.thermite1/org.openhab.binding.androidtv-5.2.3.thermite1.jar
   ```

   and `userdata/etc/overrides.properties` gains

   ```text
   # thermite pinned Android TV lifecycle repair
   mvn:org.openhab.addons.bundles/org.openhab.binding.androidtv/5.2.3.thermite1;range="[5.2.3,5.2.4)"
   ```

   openHAB's update replaces that file with the distribution's empty one, so
   every transaction writes it again. Another override of this binding is
   refused; other lines stay.
3. Karaf applies an override only when it provisions a feature: a restart
   with the override in place keeps a bundle it has already installed. So
   when the runtime's OSGi cache (`userdata/cache`) holds any other build of
   this binding, the step empties the cache, and the next start installs
   every bundle anew from local files. The emptied cache keeps its directory
   (`userdata/cache/org.eclipse.osgi`): where that is missing, openHAB's
   start script first builds the cache with a second server of its own, on
   the house's conf, and stops it while it installs the add-ons, which on
   5.2.3 can leave add-ons installed and never started, whatever is
   restarted afterwards. Nothing else under userdata is touched: Things,
   pairings, keystores and the JSON database stay. A cache that already
   holds this bundle stays as it is. Userdata that has no cache at all, as
   after openHAB's own update, gets the same empty directory, for the same
   reason.
4. **After the start**, once the add-ons are installed, the transaction reads
   the cache again and commits only when the binding it holds is this bundle
   with this content. Otherwise it says which bundle runs and rolls back.

The installed manifest records the repair among its pins
(`repair_binding_androidtv`, with the content digest). Everything it installs lies inside
userdata, which the transaction sets aside and puts back whole: a rollback or
`thermite revert` returns the runtime to exactly what ran before, cache
included, and an uninstall leaves or removes it with userdata. A house that
drops the binding loses the bundle and the override at its next install.
`thermite test boot` prepares its test runtime the same way, so a house with
this binding is boot-checked on the repaired bundle.

### What was proven, and how

The isolated pinned runtime passed 52 lifecycle checks: sole-bundle
substitution in fresh and in previously installed userdata, all cached entry
bytes, three loaded class hashes, existing and fresh PKI, 98.6 ms disposal, a
15.0-second absolute TLS deadline, and complete teardown. Those runs
installed the repair by hand and replaced an installed bundle with one
`FeaturesService.refreshFeatures` call; the installation above clears the
OSGi cache instead, which is the fresh-userdata case of that proof. Pax adds
a `Bundle-UpdateLocation` header to the manifest of the copy the framework
caches, which is why the check after the start compares content and reads
the manifest's headers, never the file's own digest.

The installation itself was then run offline on the pinned runtime, on a
house that lists both repaired bindings and has no device of theirs. In
fresh userdata, and in userdata that had run the unrepaired 5.2.3 bindings
and whose cache this step then emptied, the start installed both
`5.2.3.thermite1` bundles from userdata's repository, Karaf listed them
Active, and the check after the start passed on their content. With the
unrepaired cache kept, with or without the override, the runtime went on
running 5.2.3 and the check named it. With the cache removed outright the
start script's own cache server ran: of ten such starts, three ended with
every add-on bundle installed and never started (two with the repairs, one
without any), and the one restart tried did not start them; two more showed
one bundle not Active 45 seconds after its add-on was reported installed.
All ten starts on the emptied cache came up whole. That is why the cache's
directory stays.

Userdata that has state and no cache at all was counted next: it is what
openHAB's own update leaves when an upgrade changes the openHAB pin. Each
start began from userdata that had run, with `cache`, `tmp` and
`marketplace` removed as that update removes them, then this step, then
the start script alone: ten starts each way for this house and ten each
way for the example house, which has neither binding. With the empty
directory the step makes, all twenty starts came up whole. With that
directory removed again, eleven of twenty (seven of ten here, four of ten
without a repair) ended with every add-on bundle but one installed and
never started, start level 20 and every Thing `HANDLER_MISSING_ERROR`. So
the step gives userdata that has no cache the empty directory whether or
not the house has a repaired binding. Six starts of userdata that had
never run, given the directory the same way, came up whole too.

These are synthetic local-runtime proofs. On production, existing paired
Things must recover without a new pairing and shared dispatch must stay
timely; that remains the owner's check after the first install.

## Primary implementation references

- [Pinned Google TV manager](https://github.com/openhab/openhab-addons/blob/5.2.3/bundles/org.openhab.binding.androidtv/src/main/java/org/openhab/binding/androidtv/internal/protocol/googletv/GoogleTVConnectionManager.java)
- [Pinned handler ownership](https://github.com/openhab/openhab-addons/blob/5.2.3/bundles/org.openhab.binding.androidtv/src/main/java/org/openhab/binding/androidtv/internal/AndroidTVHandler.java)
- [Karaf4.4.11 default override location](https://github.com/apache/karaf/blob/karaf-4.4.11/features/core/src/main/java/org/apache/karaf/features/internal/osgi/Activator.java)
- [Karaf bundle-name/version override matching](https://github.com/apache/karaf/blob/karaf-4.4.11/features/core/src/main/java/org/apache/karaf/features/internal/service/Overrides.java)
- [Pinned override parser requires Maven coordinates](https://github.com/apache/karaf/blob/karaf-4.4.11/features/core/src/main/java/org/apache/karaf/features/internal/model/processing/FeaturesProcessing.java)
- [Pax2.7.1 read-only default repositories](https://github.com/ops4j/org.ops4j.pax.url/blob/url-2.7.1/pax-url-aether/src/main/java/org/ops4j/pax/url/mvn/internal/AetherBasedResolver.java)
- [Java21 socket timeout and close contract](https://docs.oracle.com/en/java/javase/21/docs/api/java.base/java/net/Socket.html)
