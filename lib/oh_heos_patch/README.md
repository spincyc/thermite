# Pinned HEOS reconnect repair

This builds a narrow derivative of the openHAB HEOS **5.2.3** binding, with
bundle version **5.2.3.thermite1**. It changes three classes and nothing else:
no Thing, channel, configuration, translation or protocol command changes.

## The defect

Each HEOS bridge holds two telnet connections to its receiver's CLI on port
1255: one for commands and one for change events. Thermite renders one
bridge per house, on its host receiver (`lib/oh_integrations/heos.py`).
The keepalive job (30-second start delay, then every 60 seconds) sends a
heartbeat on the command connection. In 5.2.3 it also resets the bridge
when the event stream has delivered nothing for two hours
(`LAST_EVENT_THRESHOLD`). The bridge then reconnects 15 seconds later.

Three upstream defects combine:

1. `HeosSendCommand.disconnect()` returns early **when the client is
   connected**, so it never closes a live socket. The next
   `TelnetClient.connect()` on the same client replaces the socket without
   closing it, which leaks the socket and its `TelnetInputStream` reader
   thread (`Thread-N`).
2. `HeosSystem.closeConnection()` uses the same inverted guard for its
   executor.
3. The last-event time is never refreshed on reconnect. After two quiet hours
   every following heartbeat resets the connection again, about every 46
   seconds.

Together, a quiet house leaks two sockets and two reader threads per bridge
about every 46 seconds, indefinitely, and commands fail while the bridge is
reconnecting.

## The repair

- **Connection leak:** a backport of upstream
  [openhab-addons#21781](https://github.com/openhab/openhab-addons/pull/21781)
  ("[heos] Fix connection leaks on reconnect and setup"). It is merged on
  `main` but in no 5.2.x release. It corrects both guards and closes the
  connection when `establishConnection` fails part way.
- **Quiet event stream:** after the event listener is registered, the event
  controller's last-event time restarts (`resetLastEventTime()`), and the
  field becomes `volatile`, because the keepalive thread reads it. A house
  that sends no events is reset at most once per two hours instead of every
  heartbeat. Treating the heartbeat response as liveness was rejected: it
  travels on the command connection, so it would no longer detect a dead
  event connection. A heartbeat on the event connection would interleave with
  events, which the binding's line reader cannot separate.

The patched sources equal `main` plus the quiet-stream change for these three
files. Detection of a genuinely silent event stream, the heartbeat schedule
and all other behaviour are unchanged.

## Version guard and retirement

`inputs.json` pins the upstream release, every input by SHA-256, the compiled
classes and the content of the rebuilt bundle, and the upstream fixes that
retire this patch (`retire_when`; record the quiet-stream pull request there
once it is filed). The build, an install of a house with the binding and
`make check` (`tests/test_heos_patch.py`) refuse when `versions.json` pins
any other openHAB release. The message says what to do:

- **Retire** when the new release's HEOS binding contains both fixes: delete
  `lib/oh_heos_patch`, `tests/heos_patch`, `tests/test_heos_patch.py`, their
  `.thermite-export.json` entries and the package's name in
  `lib/oh_addons.py` `REPAIRS`. The next install takes the bundle and the
  override line out of each runtime.
- **Rebase** otherwise: update `inputs.json` (bundle, three sources, runtime
  dependency hash), regenerate the three patches so they apply with zero fuzz,
  and rebuild with `--test-sources --write-classes`. The Java gate must pass
  on the new classes and still fail on the unmodified ones. Commit `classes/`
  and `inputs.json` with the patches.

At runtime the override is limited to `[5.2.3,5.2.4)`.

## Reproduce the artifact

Use a Java 21 executable, such as the JRE cached by `thermite sandbox fetch`,
and the pinned 5.2.3 runtime directory. No Maven or JDK installation is
required: the pinned Eclipse compiler runs on the JRE. All 338 runtime
dependency JARs are pinned by one aggregate hash; any other SDK is refused.

From this area's root, with owned directories under `.scratch`:

```sh
PYTHONPATH=lib python3 -m oh_heos_patch build \
  --java /absolute/path/to/java21/bin/java \
  --runtime /absolute/path/to/openhab-5.2.3/runtime \
  --work /absolute/path/to/.scratch/heos-inputs \
  --output /absolute/path/to/.scratch/heos-staged \
  --test-sources tests/heos_patch --fetch
```

Omit `--fetch` to require previously downloaded, hash-verified inputs. The
build applies the three patches with zero fuzz, compiles only the patched
classes and refuses any other class set. It keeps every other bundle entry
byte-for-byte except the main manifest's version and `Thermite-Patch` marker.
ZIP order, timestamps and compression are deterministic. `build.json`
records the input, patch, dependency, class and artifact hashes. The builder
writes only to `--output`: the JAR and `build.json`.

The build must produce the classes in `classes/` and the bundle content
`inputs.json` pins, or it fails: what an install puts into a runtime is what
this source compiles to. After a reviewed change to a patch or an input,
`--write-classes` (with `--test-sources`, so only a gated build is recorded)
replaces `classes/` and those pins. The classes are committed so that an
install needs no compiler, no source download and no `patch`: it needs only
the pinned upstream bundle. The JAR itself is not pinned by its own SHA-256,
because its compressed bytes depend on the host's zlib; its content is.

## Tests

`tests/heos_patch/ReconnectTest.java` runs the **rebuilt production classes**
against two loopback fake HEOS CLIs (`FakeHeosServer.java`) that answer every
command and never send an event. A compressed scheduler divides every delay
by 200. The listener reconnects on `CONNECTION_RESTORED` the same way
`HeosBridgeHandler` does. Each fake CLI has a bridge of its own, and every
assertion holds for each; Thermite itself renders one bridge per house. A
security manager refuses every non-loopback connection. The test backdates
the last event past the threshold and asserts:

- exactly one reset per receiver, followed by six normal heartbeats;
- exactly one reconnect: four sockets accepted in total, two open;
- the previous sockets closed and one reader thread per live socket;
- `closeConnection()` closes every socket and ends every reader thread.

With `--test-sources`, the build publishes nothing unless this test passes
on the rebuilt JAR **and fails on the unmodified 5.2.3 JAR** (the negative
control). On 5.2.3 it fails 12 of 16 assertions. Within seconds, each receiver
is reset hundreds of times and accumulates hundreds of open sockets and reader
threads.

`tests/test_heos_patch.py` runs in `make check`: the version guard, exact
pins, patch scope, deterministic packaging, the committed classes and the
build refusals. `tests/test_binding_repairs.py` covers the installation: the
repository, the override, the OSGi cache and the check after the start.

### Native proof

Before release, the coordinator ran a pinned 5.2.3 runtime in rootless
containers with no network, connected to the loopback fake HEOS CLI. These
are synthetic local proofs; the post-deploy checks below remain the
production acceptance. They were made when the repair was installed by hand,
with a feature refresh in place of the cleared cache the installation now
uses. What they establish about Karaf is why the installation clears the
cache: a restart alone neither installs nor removes the repaired bundle.
The installation as it now runs was proven afterwards, offline, on the
pinned runtime: `lib/oh_androidtv_patch/README.md` ("What was proven, and
how") says what was run and seen for both bindings.

- **Activation on an existing install:** a restart with the override, the
  repository entry and the JAR in place kept 5.2.3 and logged no
  provisioning. The apply probe's refresh substituted `5.2.3.thermite1`
  under the same bundle id and logged `verified:true` with the four patched
  class hashes. The retiring 5.2.3 handler kept its two sockets and reader
  threads until the second restart. After that restart the bridge held two
  sockets and sent a heartbeat every 60 seconds without reconnecting.
- **Rollback:** a restart after restoring the files and deleting the JAR
  still ran `5.2.3.thermite1` from the OSGi bundle cache. The rollback
  probe's refresh restored 5.2.3 offline and logged `verified:true` with the
  upstream class hashes. The retiring patched handler closed its sockets.
- **Lowered threshold (test-only builds with a 120-second threshold):** the
  patched classes reset a quiet stream once every 166 seconds (the third
  heartbeat check plus the 16-second reconnect). They held two sockets and
  two reader threads for 11 minutes. The unpatched classes reset it every 46
  seconds, and sockets and reader threads grew from 4 to 24 in 10 minutes.

The unpatched binding also leaks on any handler disposal, such as a bundle
refresh during startup or a Thing edit, not only on quiet-stream resets.
`refreshFeatures` reprovisions every feature, so it also applies any other
pending feature change. On a fresh test install it added one unrelated
Jetty bundle.

## Installation

`thermite install`, `upgrade` and `rollback` install this repair for every
house whose `conf/services/addons.cfg` lists `binding-heos`; no other house gets
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
   `Thermite-Patch: heos-reconnect-v1`. No compiler runs on the host. The bundle goes
   into the runtime's own repository, `userdata/thermite/maven`, at

   ```text
   org/openhab/addons/bundles/org.openhab.binding.heos/5.2.3.thermite1/org.openhab.binding.heos-5.2.3.thermite1.jar
   ```

   and `userdata/etc/overrides.properties` gains

   ```text
   # thermite pinned HEOS reconnect repair
   mvn:org.openhab.addons.bundles/org.openhab.binding.heos/5.2.3.thermite1;range="[5.2.3,5.2.4)"
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
   holds this bundle stays as it is.
4. **After the start**, once the add-ons are installed, the transaction reads
   the cache again and commits only when the binding it holds is this bundle
   with this content. Otherwise it says which bundle runs and rolls back.

The installed manifest records the repair among its pins
(`repair_binding_heos`, with the content digest). Everything it installs lies inside
userdata, which the transaction sets aside and puts back whole: a rollback or
`thermite revert` returns the runtime to exactly what ran before, cache
included, and an uninstall leaves or removes it with userdata. A house that
drops the binding loses the bundle and the override at its next install.
`thermite test boot` prepares its test runtime the same way, so a house with
this binding is boot-checked on the repaired bundle.

The first install of this repair on a runtime that ran the unrepaired binding
stops openHAB through the defective 5.2.3 disposal path; the stop ends the
process, so the leaked sockets go with it.

## Post-deploy checks

The house's one bridge should hold exactly two established CLI connections,
both to its host receiver (the address of the `heos:bridge:<host>` Thing),
before and after the two-hour mark:

```sh
ss -Htn state established '( dport = :1255 )' dst HOST_ADDRESS | wc -l
ss -Htn state established '( dport = :1255 )' | wc -l
```

Both must print 2: openHAB holds no HEOS connection to the other receivers.

The unpatched binding raises these counts by two about every 46 seconds once
the event stream has been quiet for two hours. After at least 2 hours 5
minutes of uptime, openHAB's live thread count must also stay flat. In
Thermite's Prometheus:

```text
jvm_threads_live_threads{job="openhab"}
delta(jvm_threads_live_threads{job="openhab"}[30m])
```

The first should hold steady; the second should stay near zero. The defect
adds two reader threads per bridge about every 46 seconds, roughly 80 every
30 minutes for the house's one bridge.

## Primary references

- [Pinned HeosSystem](https://github.com/openhab/openhab-addons/blob/5.2.3/bundles/org.openhab.binding.heos/src/main/java/org/openhab/binding/heos/internal/api/HeosSystem.java)
- [Pinned HeosSendCommand](https://github.com/openhab/openhab-addons/blob/5.2.3/bundles/org.openhab.binding.heos/src/main/java/org/openhab/binding/heos/internal/resources/HeosSendCommand.java)
- [openhab-addons#21781](https://github.com/openhab/openhab-addons/pull/21781)
- [Commons Net 3.13.0 SocketClient.connect](https://github.com/apache/commons-net/blob/rel/commons-net-3.13.0/src/main/java/org/apache/commons/net/SocketClient.java)
- [Karaf 4.4.11 override matching](https://github.com/apache/karaf/blob/karaf-4.4.11/features/core/src/main/java/org/apache/karaf/features/internal/service/Overrides.java)
