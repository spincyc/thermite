# Pinned HEOS reconnect repair

This builds a narrow derivative of the openHAB HEOS **5.2.3** binding, with
bundle version **5.2.3.thermite1**. It changes three classes and nothing else:
no Thing, channel, configuration, translation or protocol command changes.

## The defect

Each HEOS bridge holds two telnet connections to the receiver's CLI on port
1255: one for commands and one for change events. The keepalive job
(30-second start delay, then every 60 seconds) sends a heartbeat on the
command connection. In 5.2.3 it also resets the bridge when the event stream
has delivered nothing for two hours (`LAST_EVENT_THRESHOLD`). The bridge then
reconnects 15 seconds later.

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

`inputs.json` pins the upstream release, every input by SHA-256 and the
upstream fixes that retire this patch (`retire_when`; record the quiet-stream
pull request there once it is filed). The build, `prepare`
and `make check` (`tests/test_heos_patch.py`) refuse when `versions.json`
pins any other openHAB release. The message says what to do:

- **Retire** when the new release's HEOS binding contains both fixes: roll
  back the override (below) on every runtime that applied it, then delete
  `lib/oh_heos_patch`, `tests/heos_patch`, `tests/test_heos_patch.py` and
  their `.thermite-export.json` entries.
- **Rebase** otherwise: update `inputs.json` (bundle, three sources, runtime
  dependency hash), regenerate the three patches so they apply with zero fuzz,
  and rebuild with `--test-sources`. The Java gate must pass on the new
  classes and still fail on the unmodified ones.

At runtime the override is limited to `[5.2.3,5.2.4)`. An upgraded openHAB
therefore keeps its own HEOS binding even if the override line is left
behind; remove the line during retirement anyway.

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
writes only to `--output`: the JAR, `build.json`,
`overrides.properties.fragment` and `maven-layout.json`.

## Tests

`tests/heos_patch/ReconnectTest.java` runs the **rebuilt production classes**
against two loopback fake HEOS CLIs (`FakeHeosServer.java`) that answer every
command and never send an event. A compressed scheduler divides every delay
by 200. The listener reconnects on `CONNECTION_RESTORED` the same way
`HeosBridgeHandler` does. A security manager refuses every non-loopback
connection. The test backdates the last event past the threshold and asserts:

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
pins, patch scope, deterministic packaging, the override and Maven
configuration merges, and the build and `prepare` refusals.

### Native proof

Before release, the coordinator ran a pinned 5.2.3 runtime in rootless
containers with no network, connected to the loopback fake HEOS CLI. These
are synthetic local proofs; the post-deploy checks below remain the
production acceptance.

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

## Activation

Ordinary deploy does not activate this derivative. Activation follows the
Android TV precedent: a Karaf override, a private Maven repository, a
stop-first restart, one feature refresh, then a second restart.

- `userdata/etc/overrides.properties` gains:

  ```text
  mvn:org.openhab.addons.bundles/org.openhab.binding.heos/5.2.3.thermite1;range="[5.2.3,5.2.4)"
  ```

- `org.ops4j.pax.url.mvn.defaultRepositories` in
  `userdata/etc/org.ops4j.pax.url.mvn.cfg` gains a first entry
  `file:///openhab/userdata/thermite/maven/@id=thermite.heos`. It shares the
  Android TV patch's directory but has its own id, so each patch rolls back
  alone.
- The JAR goes under that repository at `MAVEN_PATH`
  (`org/openhab/addons/bundles/org.openhab.binding.heos/5.2.3.thermite1/`).

Karaf 4.4.11 does not reprovision installed features at startup, so a
one-shot script calls `FeaturesService.refreshFeatures` once and logs the
sole HEOS bundle's version, state and patched-class hashes. The refresh stops
the old handlers through the defective 5.2.3 disposal path, which leaks their
sockets. The second restart releases them.

`prepare` produces every file the owner installs. It reads only **copies** of
the two Karaf files, requires a staged build that passed the Java gate and its
negative control, and writes into a fresh private directory:

- the JAR;
- the desired `overrides.properties` and `org.ops4j.pax.url.mvn.cfg`;
- their `.before` preimages;
- the apply and rollback probes;
- `SHA256SUMS`.

It never reads or writes live userdata, and refuses a competing HEOS override
or repository.

### Owner procedure

The service and file steps need a root shell; the assistant never runs them.
`STATE` is the runtime state root: the source of the userdata `Volume=` line
in `systemctl cat thermite-openhab.service`, by default
`/srv/thermite/runtime`. `OWNER` is your login, and `IN`, `OUT` are fresh
directories you own.

1. Root: copy the two Karaf files out for review.

   ```sh
   umask 077
   install -m 0600 -o "$OWNER" "$STATE/userdata/etc/org.ops4j.pax.url.mvn.cfg" "$IN/"
   install -m 0600 -o "$OWNER" "$STATE/userdata/etc/overrides.properties" "$IN/"
   ```

   Skip the second line when `overrides.properties` does not exist.

2. Owner, from the reviewed checkout: build (above), then prepare and review.

   ```sh
   PYTHONPATH=lib python3 -m oh_heos_patch prepare --staged "$STAGED" \
     --maven "$IN/org.ops4j.pax.url.mvn.cfg" --overrides "$IN/overrides.properties" \
     --output "$OUT"
   diff -u "$OUT/overrides.properties.before" "$OUT/overrides.properties"
   diff -u "$OUT/org.ops4j.pax.url.mvn.cfg.before" "$OUT/org.ops4j.pax.url.mvn.cfg"
   ```

   Each diff must add only the HEOS lines. Compare the printed
   `artifact_sha256` with the reviewed build's value.

3. Root: verify, stop, check the preimages and install.

   ```sh
   cd "$OUT" && sha256sum -c SHA256SUMS
   U=$(stat -c %u "$STATE/userdata"); G=$(stat -c %g "$STATE/userdata")
   systemctl stop thermite-openhab.service
   cmp org.ops4j.pax.url.mvn.cfg.before "$STATE/userdata/etc/org.ops4j.pax.url.mvn.cfg"
   cmp overrides.properties.before "$STATE/userdata/etc/overrides.properties"
   umask 022
   install -D -m 0644 org.openhab.binding.heos-5.2.3.thermite1.jar \
     "$STATE/userdata/thermite/maven/org/openhab/addons/bundles/org.openhab.binding.heos/5.2.3.thermite1/org.openhab.binding.heos-5.2.3.thermite1.jar"
   cat org.ops4j.pax.url.mvn.cfg > "$STATE/userdata/etc/org.ops4j.pax.url.mvn.cfg"
   cat overrides.properties > "$STATE/userdata/etc/overrides.properties"
   ```

   Stop if either `cmp` reports a difference: a file changed since it was
   copied, so start again from step 1. `cat >` keeps each file's owner and
   mode. If `overrides.properties` did not exist, skip its `cmp` and create it
   with `install -m 0600 -o "$U" -g "$G" overrides.properties "$STATE/userdata/etc/"`.

4. Root: start, refresh once and verify.

   ```sh
   LOG="$STATE/userdata/logs/openhab.log"; N=$(wc -l < "$LOG")
   systemctl start thermite-openhab.service
   install -m 0644 -o "$U" -g "$G" "apply-thermite_heos_patch_probe.js" \
     "$STATE/conf/automation/js/thermite_heos_patch_probe.js"
   timeout 900 sh -c "until tail -n +$((N + 1)) '$LOG' | grep -aq 'thermite-heos-patch '; do sleep 5; done"
   tail -n +$((N + 1)) "$LOG" | grep -a 'thermite-heos-patch '
   rm "$STATE/conf/automation/js/thermite_heos_patch_probe.js"
   systemctl restart thermite-openhab.service
   ```

   The log line must show `"status":"ok"` and `"verified":true`, with one
   bundle at version `5.2.3.thermite1`, state 32 (active). Otherwise, roll
   back. The script runs only after openHAB loads the automation, so the
   wait covers startup.

### Rollback

Rollback uses the same steps with the files from the same `OUT`:

1. Stop the service.
2. `cmp` each live file against the prepared desired file, to confirm nothing
   else changed it.
3. Restore both `.before` contents with `cat >`. Remove `overrides.properties`
   instead when no `.before` copy exists.
4. Remove the JAR and its `5.2.3.thermite1` directory.
5. Start the service and install `rollback-thermite_heos_patch_probe.js` the
   same way. It must log `"verified":true` with version `5.2.3`. The probe is
   required: until its refresh, the runtime keeps loading the patched bundle
   from its OSGi cache.
6. Remove the probe and restart.

### Android TV coexistence

The two overrides and repository entries are independent lines, and
`prepare` preserves the Android TV lines byte-for-byte. The Android TV
maintenance program refuses to roll back while either Karaf file differs
from its own post-apply state. **Roll back HEOS first**, then Android TV.
Applying HEOS after Android TV needs no special order.

## Post-deploy checks

For each receiver, the bridge should hold exactly two established CLI
connections, before and after the two-hour mark:

```sh
ss -Htn state established '( dport = :1255 )' dst RECEIVER_ADDRESS | wc -l
```

The unpatched binding raises this count by two about every 46 seconds once the
event stream has been quiet for two hours. After at least 2 hours 5 minutes of
uptime, openHAB's live thread count must also stay flat. In Thermite's
Prometheus:

```text
jvm_threads_live_threads{job="openhab"}
delta(jvm_threads_live_threads{job="openhab"}[30m])
```

The first should hold steady; the second should stay near zero. The defect
adds two reader threads per bridge about every 46 seconds, roughly 80 every
30 minutes.

## Primary references

- [Pinned HeosSystem](https://github.com/openhab/openhab-addons/blob/5.2.3/bundles/org.openhab.binding.heos/src/main/java/org/openhab/binding/heos/internal/api/HeosSystem.java)
- [Pinned HeosSendCommand](https://github.com/openhab/openhab-addons/blob/5.2.3/bundles/org.openhab.binding.heos/src/main/java/org/openhab/binding/heos/internal/resources/HeosSendCommand.java)
- [openhab-addons#21781](https://github.com/openhab/openhab-addons/pull/21781)
- [Commons Net 3.13.0 SocketClient.connect](https://github.com/apache/commons-net/blob/rel/commons-net-3.13.0/src/main/java/org/apache/commons/net/SocketClient.java)
- [Karaf 4.4.11 override matching](https://github.com/apache/karaf/blob/karaf-4.4.11/features/core/src/main/java/org/apache/karaf/features/internal/service/Overrides.java)
