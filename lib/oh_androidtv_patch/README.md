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
whose trust anchor is the coordinator-reviewed receipt hash; it carries no
upstream signature.
Upstream licenses and notices remain included. ZIP ordering, timestamps and
compression are deterministic; identical inputs produce identical bytes.
`build.json` records source, patch, compiler, dependency and artifact hashes.

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

## Deployment and rollback boundary

The isolated pinned runtime passed 52 lifecycle checks, including in-flight
disposal and the absolute TLS deadline. The separate three-boot maintenance
proof passed 26 checks for activation, exact rollback, source/disabled-flag
preservation and unchanged keystore metadata; all ten cleanup checks passed.
These are synthetic local-runtime proofs. Production replacement and recovery
of existing physical paired devices remain owner maintenance checks.

The coordinator's isolated native lifecycle run passed **52 checks**: fresh
and installed-cache substitution, all cached entry bytes, three loaded class
hashes, existing and fresh PKI, 98.6 ms disposal, a 15.0-second absolute TLS
deadline, and complete teardown. This establishes the binding mechanism;
physical paired-device acceptance remains an owner check. The separate
stop-first transaction must also have a passing, externally pinned native
apply **and rollback** receipt before the maintenance CLI will stage a
production package. Its first run proved apply and then stopped safely on
Pax's original-bundle manifest addition during rollback; that exact addition
now has a separate strict original-cache verifier. No failed receipt qualifies.

### Owner maintenance commands

Ordinary deploy does not activate this derivative. Retain the reviewed JAR,
official original JAR, build receipt, lifecycle proof and final maintenance
proof under a persistent private directory such as
`~/.local/share/thermite/androidtv-patch/5.2.3.thermite1/`. The coordinator
copies the reviewed artifacts there; no binary or instance state belongs in
Git. Supply all SHA-256 pins from the independent handoff, rather than deriving
an approval from the incoming files themselves. The runtime state below must
be the actual Quadlet mount root. Preflight checks all three container mounts.

As the ordinary owner, from this area's reviewed checkout:

```sh
PYTHONPATH=lib python3 -B -m oh_androidtv_patch.maintenance package-program \
  --output /absolute/persistent/incoming/program
PYTHONPATH=lib python3 -B -m oh_androidtv_patch.maintenance stage \
  --state /absolute/installed/runtime-state \
  --artifact /absolute/persistent/inputs/org.openhab.binding.androidtv-5.2.3.thermite1.jar \
  --original-artifact /absolute/persistent/inputs/org.openhab.binding.androidtv-5.2.3.jar \
  --build-receipt /absolute/persistent/inputs/build.json \
  --build-receipt-sha256 EXTERNALLY_APPROVED_BUILD_RECEIPT_SHA256 \
  --native-proof /absolute/persistent/inputs/lifecycle-proof.json \
  --native-proof-sha256 EXTERNALLY_APPROVED_LIFECYCLE_PROOF_SHA256 \
  --maintenance-proof /absolute/persistent/inputs/maintenance-proof.json \
  --maintenance-proof-sha256 EXTERNALLY_APPROVED_MAINTENANCE_PROOF_SHA256 \
  --package /absolute/persistent/incoming/data
```

Both commands stage data only. Record the returned program and manifest SHA
in the separate trusted owner handoff. The final maintenance proof must match
this exact executable program, artifact and cleanup receipt. Source changes
require another native transaction proof; a receipt from a different program
cannot be reused.

The following installation and service actions require the owner to open a
root shell. The assistant never invokes sudo or these actions. Use a **fresh**
root package under `/var/lib`, not a root-owned leaf beneath a user-writable
home directory. Verify `/`, `/var` and `/var/lib` are root-owned, non-symlinks
and not group/world writable. With root umask 077, copy the two approved
incoming directories as data, without dereferencing links or preserving
ownership, into fresh `program` and `data` directories under
`/var/lib/thermite-androidtv-maintenance/v1`. Refuse an existing destination;
never recursively chown or execute an incoming user-owned tree.

The following literal preparation checks ancestors before creating the fresh
root destination, then copies only data. Replace the two incoming paths with
the persistent paths used above:

```sh
umask 077
/usr/bin/python3 -I -S -B - <<'PY'
import os, pathlib, stat
destination = pathlib.Path('/var/lib/thermite-androidtv-maintenance/v1')
for path in reversed(destination.parents):
    try:
        info = path.lstat()
    except FileNotFoundError:
        path.mkdir(mode=0o700)
        info = path.lstat()
    assert stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022
assert not os.path.lexists(destination)
destination.mkdir(mode=0o700)
PY
/usr/bin/cp -R --no-dereference --no-preserve=ownership,mode \
  /absolute/persistent/incoming/program /var/lib/thermite-androidtv-maintenance/v1/program
/usr/bin/cp -R --no-dereference --no-preserve=ownership,mode \
  /absolute/persistent/incoming/data /var/lib/thermite-androidtv-maintenance/v1/data
```

Before executing any copied code, use only the system Python standard
library to validate the externally approved program receipt and every
program file. In the root shell:

```sh
/usr/bin/python3 -I -S -B - \
  /var/lib/thermite-androidtv-maintenance/v1/program \
  EXTERNALLY_APPROVED_PROGRAM_SHA256 <<'PY'
import hashlib, json, os, pathlib, re, stat, sys
root = pathlib.Path(sys.argv[1])
receipt = root / 'program.json'
info = receipt.lstat()
assert stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size <= 32768
assert info.st_uid == 0 and not info.st_mode & 0o022
assert hashlib.sha256(receipt.read_bytes()).hexdigest() == sys.argv[2]
value = json.loads(receipt.read_bytes())
package = root / 'oh_androidtv_patch'
assert value['version'] == 1 and set(value['files']) == {p.name for p in package.iterdir()}
for name, expected in value['files'].items():
    assert re.fullmatch(r'[A-Za-z0-9_.-]+', name)
    path = package / name
    for parent in (path, *path.parents):
        info = parent.lstat()
        assert not stat.S_ISLNK(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022
    info = path.stat()
    assert stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size <= 131072
    assert hashlib.sha256(path.read_bytes()).hexdigest() == expected
print('Reviewed program bytes and root-owned ancestry verified')
PY
```

Then apply, or explicitly roll back the matching transaction:

```sh
/usr/bin/python3 -I -S -B \
  /var/lib/thermite-androidtv-maintenance/v1/program/oh_androidtv_patch/owner_run.py \
  --program-sha256 EXTERNALLY_APPROVED_PROGRAM_SHA256 apply \
  --package /var/lib/thermite-androidtv-maintenance/v1/data \
  --manifest-sha256 EXTERNALLY_APPROVED_MANIFEST_SHA256
/usr/bin/python3 -I -S -B \
  /var/lib/thermite-androidtv-maintenance/v1/program/oh_androidtv_patch/owner_run.py \
  --program-sha256 EXTERNALLY_APPROVED_PROGRAM_SHA256 rollback \
  --package /var/lib/thermite-androidtv-maintenance/v1/data \
  --manifest-sha256 EXTERNALLY_APPROVED_MANIFEST_SHA256
```

`owner_run.py` requires isolated Python with site startup and bytecode disabled,
checks the entire root-owned executable ancestry and external program hash,
and imports local code only after validation. It exposes only apply/rollback;
no arbitrary service, shell command, URL or native API option exists. The
service is fixed to the rootful `openhab.service`/`openhab` container. Direct
`PYTHONPATH=lib ... apply` from a user-writable checkout is refused.

Apply validates the running container's conf/userdata/addons mounts, rejects
editable or unexpected GoogleTV Things, competing overrides and watched
AndroidTV JARs, and verifies the original loaded classes/cache. It stops the
service and checks the old cgroup, PID start times and container before
quarantining exactly the managed GoogleTV YAML and JS. It installs only the
three approved core files, starts with no GoogleTV Things, and performs a
fixed one-shot `FeaturesService.refreshFeatures` call. The one-shot has no
Item trigger, native credential or device command. Only after sole binding,
loaded class and every cached entry verification does it remove the one-shot
and restore both exact source files and native disabled flags.

Rollback uses the same stop-first sequence and verifies the original binding
before restoring source files. It preserves current disabled flags when
available, otherwise the matching interrupted apply receipt. File content,
ownership or permission drift causes refusal. No cache, keystore, account,
unrelated config or pairing data is deleted. Failed phases stop the runtime
and retain quarantine for explicit rollback; a failure to confirm stopping
is reported distinctly. Keep normal deploy, installer and manual native
config changes idle throughout this controlled maintenance window.

The builder **stages only** the JAR, build receipt,
`overrides.properties.fragment` and `maven-layout.json`. It never writes live addons/userdata,
calls an API, restarts a service or changes a native bundle. The coordinator
must integrate the staged artifact into the normal deployment lifecycle;
ordinary conf-only deployment does not activate this patch automatically.

The pinned runtime uses Karaf **4.4.11**. Its feature-service activator reads
`userdata/etc/overrides.properties` by default. The fragment names the same
bundle symbolic name at a higher qualifier version and limits substitution
to the pinned release range. This processing path requires an **mvn:** URI;
plain file URIs are ignored by the pinned implementation:

```text
mvn:org.openhab.addons.bundles/org.openhab.binding.androidtv/5.2.3.thermite1;range="[5.2.3,5.2.4)"
```

The container mounts persistent userdata at `/openhab/userdata`. Stage the
artifact at `userdata/thermite/maven/` followed by `MAVEN_PATH` from the module
(the standard group/artifact/version/JAR layout). Add that dedicated local
repository before the existing `org.ops4j.pax.url.mvn.defaultRepositories`
entries in `userdata/etc/org.ops4j.pax.url.mvn.cfg`.
`maven_config_text(previous, repository=absolute_path)` performs the pure
idempotent merge, retaining all current default repositories, local cache
and unrelated settings. It refuses missing/duplicate effective properties
or a competing owned repository. `repository_entry()` supplies the container
default `file:///openhab/userdata/thermite/maven/@id=thermite.androidtv`.
Use the sandbox's own absolute repository path for a fixture. Do not rely
on temporary `tmp/mvn` as the sole authoritative copy, or replace the normal
runtime's local Maven cache. The coordinator creates the dedicated repository
with reviewed ownership; the build changes no ownership or permissions.
Never copy a second binding into watched addons and assume
it replaced the feature-installed one: verify there is exactly one active
Android TV binding, with the expected qualifier version and authenticated
source JAR. Pax repacks the cached archive and adds exactly
`Bundle-UpdateLocation: mvn:org.openhab.addons.bundles/org.openhab.binding.androidtv/5.2.3.thermite1`.
Consequently its container SHA can differ. `cached_jar_report()` requires the
original staged receipt SHA, identical unique entry sets, every non-manifest
entry's exact bytes, and parsed manifest equality allowing only that exact
added header. Duplicate headers/entries, other changed or added metadata,
class/resource changes and extra manifest sections fail closed. Verify the
three loaded class hashes as well, and record the cache's separate SHA.
`override_text(previous)` performs a pure, idempotent merge, preserves
unrelated overrides and refuses a competing Android TV override.

Before activation, save exact preimages/hashes for only the owned artifact,
override file and the existing Pax Maven configuration. Use a
**coordinator-owned isolated fixture gate**, then a normal
coordinator-controlled native restart after persistent staging. A restart
retires old blocked workers; hot updating the old bundle can itself enter
the defective old disposal path. Do not delete native caches, Things,
userdata or pairings to install this patch. The fixture must demonstrate
sole-bundle substitution in both fresh and previously installed userdata,
bounded failed TLS, disposal, no retry resurrection
and retained protocol/Thing schema. Karaf's `feature:refresh` command reloads
processing instructions and reprovisions existing features; do not assume
that an override file alone replaced an already persisted bundle. Do not
hot-refresh defective old handlers before the controlled maintenance plan
has retired their blocking work. On production, verify existing paired
Things recover and shared dispatch remains timely, without forcing re-pair.

Rollback restores the exact override and Pax configuration preimages, restores/removes only the
owned derivative artifact, and uses the same controlled restart. Verify
the original pinned binding is the sole active version afterward. Preserve
the separate capacity mitigation, all existing keystores and other userdata.
Both native activation and physical-device validation remain coordinator
gates; the staged artifact makes no claim that they already passed.

## Primary implementation references

- [Pinned Google TV manager](https://github.com/openhab/openhab-addons/blob/5.2.3/bundles/org.openhab.binding.androidtv/src/main/java/org/openhab/binding/androidtv/internal/protocol/googletv/GoogleTVConnectionManager.java)
- [Pinned handler ownership](https://github.com/openhab/openhab-addons/blob/5.2.3/bundles/org.openhab.binding.androidtv/src/main/java/org/openhab/binding/androidtv/internal/AndroidTVHandler.java)
- [Karaf4.4.11 default override location](https://github.com/apache/karaf/blob/karaf-4.4.11/features/core/src/main/java/org/apache/karaf/features/internal/osgi/Activator.java)
- [Karaf bundle-name/version override matching](https://github.com/apache/karaf/blob/karaf-4.4.11/features/core/src/main/java/org/apache/karaf/features/internal/service/Overrides.java)
- [Pinned override parser requires Maven coordinates](https://github.com/apache/karaf/blob/karaf-4.4.11/features/core/src/main/java/org/apache/karaf/features/internal/model/processing/FeaturesProcessing.java)
- [Pax2.7.1 read-only default repositories](https://github.com/ops4j/org.ops4j.pax.url/blob/url-2.7.1/pax-url-aether/src/main/java/org/ops4j/pax/url/mvn/internal/AetherBasedResolver.java)
- [Java21 socket timeout and close contract](https://docs.oracle.com/en/java/javase/21/docs/api/java.base/java/net/Socket.html)
