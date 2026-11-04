# Installation review and acceptance

The application now lives in the Thermite repository. Historical references below
to a canonical private checkout and public export describe earlier builds; they
do not define the current development or deployment workflow. Private sites now
pin an application release and keep generated files outside Git.

Updated 2026-11-03 UTC. The review began on `feature/productionize`.
Its tested history is preserved while reconciling with `main`; the original
installation acceptance and subsequent runtime integration are distinct below.

## Cold-review corrections

The subsequent cold review found remaining gaps in staging-path protection,
interrupted backup recovery, access-release activation, startup HTTP deadlines,
upgrade prerequisite detection and approval propagation. The fixes require
protected release/state parents before staging, publish backups with explicit
completion evidence, and provide an operator-reviewed access cutover script.
The browser regression harness also preserves its debugging descriptors across
exec and keeps temporary socket paths short in deeply nested workspaces.

For publication, these fixes were reconciled with canonical `main` at
`f5bf997`, retaining installation-wide approval, explicit runtime reset and
compact lifecycle progress. Native recovery uses that run's scoped approval.

These changes supersede the code in the guest-tested artifacts below. Their
validation uses regression tests, isolated filesystems, simulated service
commands, local HTTP servers and the real browser harness. The earlier Arch
and Ubuntu guest acceptance remains evidence for its named artifacts; it is
not a fresh guest acceptance claim for these corrections. Existing access
parents need the permission migration in the installation guide before staging
another release. Existing backup archives remain available for manual recovery,
but an archive without completion evidence is not reused by an upgrade.

## Main integration

Reconciled with canonical `main` at `de3c961`, retaining its pooled HTTP
transport, finite command batches, event-driven worker and routing lifecycle
changes alongside the production installation fixes.

- Canonical runtime commit: `88385f6`; matching public export: `35c7833`.
- Guest-tested integration release: `eea4ec8777575ca0fe877ddd217a8b7a1090951f393688c4ef8043f738eb6552`.
- Wheel SHA256: `87146acead8943440fd0e42a376cae4ad3635793c1cf838cc782306e3fa1e25a`;
  canonical/public wheels are byte-identical.
- Canonical checks: 1,830 tests, twelve optional skips; public checks: 228 tests,
  two optional skips, against those exact integration commits.

Both retained Arch and Ubuntu sites completed application/native/access updates,
strict boot, HTTP/browser/TUI/real-terminal checks, worker repair and singleton
checks, external native-port blocking, no-op protected-plan replay, reboot and
repeated clients. Existing credentials and identity were retained without
bootstrap or administrator recreation; quiescent staging/handoff preserved the
SQLite main file. No-op preserved the 12 service starts and 11 named stable
file fingerprints; reboot preserved the fingerprints with a new boot ID.
Ubuntu retained Podman 4.9.3 and enforced AppArmor, including the complete older
image-prefetch path and node-exporter's read-only systemd collection.

| Integration case | Native stop | Authenticated held-stream closure |
|---|---|---|
| Stock Arch | 9.64 s | 15.20 s |
| Ubuntu Server | 9.19 s | 15.12 s |

Native stops reported normal exit zero without forced kill or AppArmor signal
denials. Gateway stops completed bounded stream cancellation and retained
systemd success with the original SIGTERM status. Both guests powered off
cleanly. A separate owned-child signal probe and committed regression verify
that command batches drain before transport pools close, inside server shutdown
before Uvicorn re-raises SIGTERM. An idempotent outer cleanup still handles
startup/listener errors.

Boot-report collection retains its absolute deadline and strict response
validation while using full Things DTOs only for final classification. Native
readiness uses the same exact account-free HEOS warning predicate as `main`;
other configuration errors remain terminal. Configuration and account fields
are neither logged nor included in reports. Worker configuration-before-model
import, exact directory modes and removal of obsolete split rules are retained.

This integration changes runtime code from the earlier `7c05db18` candidate.
Its new guest coverage is retained-state Arch/Ubuntu upgrades, not another fresh
ISO installation. The fresh guided ISO evidence below remains tied to its exact
earlier artifact. The final documentation reconciliation changes only this
handoff, requirements Markdown and generated requirements HTML; package proofs
must compare every other byte and archive permission with this integration
wheel and verify both release manifests, RECORDs and canonical/public equality.
The final documentation commit records that package identity.

## Earlier installation candidate and fresh ISO evidence

For that earlier build, the implementation was `automation/openhab` in private
`spincyc/homelab`. Fix that source, then regenerate public `spincyc/thermite`
with `python3 bin/thermite docs export --with-code --to /path/to/thermite`.

- Canonical code commit: `fb65ece`; public code commit: `8d39d36`.
- Application: `0.1.0.dev0`.
- Release: `7c05db18a95ee2485b3e9b04b96b0608ffe52f8c910e6dc43b51ecfeeb18c77c`.
- Wheel SHA256: `77f824cdac198ce9dcb4489cb5c42d13ee533083a6791436d429c8b6ff2eb6fa`.
- Thermite Arch ISO SHA256: `1bd7dad024d893ce72ccc42ac8cd90922fc8d044708a99426022f8e0dcaeffb3`.

Checks passed against those exact code commits: canonical source, 1,710 tests
with ten optional skips; public export, 192 tests. Canonical and public wheels
are byte-identical.

The pre-integration documentation package was release
`7b00ad1f6e0859c3e9ade5cea28e99c0fca477a71a7f283a3d4449971e6524e2`,
wheel SHA256
`585919cf38f36d1738a8021e8b66d7cbef215571a02f384e0772dafcbcdb8e3d`.
Its exact payload comparison changed only the handoff, requirements and recipe
Markdown, generated requirements HTML, and derived release manifest/RECORD.
That historical documentation-only proof does not cover the later runtime
integration described above.

## Baseline acceptance (`7c05db18`)

Each guest uses KVM with host CPU exposure, four vCPUs, 8 GiB RAM, a sparse
64 GiB virtio disk, UEFI and an ext4 root. The fictional fixture has one room,
one computer source, an HTTP matrix and an observe-only Sony display on separate
guest-loopback addresses. The [acceptance recipe](../tests/e2e/README.md) defines
the checks; retain each actual allocation and artifact identity with its proof.

| Path | Result on `7c05db18` |
|---|---|
| Stock Arch plus application | Accepted application/native update and a new staged access release using the same authority; actual stopped-metrics restart, full clients, no-op/handoff replay, reboot and both shutdown checks passed. Earlier correction/resume and the empty-target failure remain retained. This is upgrade/resume coverage. |
| Thermite Arch ISO | Accepted fresh firmware boot and native guided storage/owner/password/sudo workflow, saved-config review before destructive confirmation, exact application-hook success, media removal and installed-disk console password/sudo checks; then full commissioning, clients, no-op replay, reboot and shutdown. |
| Ubuntu Server 24.04.5 plus application | Accepted application/native and staged-access upgrades while preserving the existing authority, followed by full clients, no-op replay, reboot and shutdown. Its fresh OS and deliberately interrupted-install/reboot checks began on `33d5ec91`; it is not a failure-free fresh installation of the final artifact. |

Earlier accepted evidence remains separate: Arch release
`b6cd35c4ca48c87b9ff2a5534e80489b524b0829b5b1d1e7937ce212fdc36f1a`
and Ubuntu release
`33d5ec911d354725546f90664432f879058e9b3bedda141143955c7aeff1f34c`,
followed by accepted `7a48ab33` ISO/Ubuntu runs. The subsequent stock Arch
upgrade exposed empty Prometheus target discovery with an old collector sample.
The corrected candidate above was then accepted on all three paths.

Arch starts from the official 2026.11.01 image with rolling installed packages.
Ubuntu starts from the official 24.04.5 Server image. Its external private OS
test seed is guarded to the disposable blank disk; it is not an unattended
Thermite storage/identity default. The optional Thermite Ubuntu ISO has build
and offline checks, without a full guest-installation acceptance claim.

The native ISO installer created the OS and owner. SSH and guest-only transport
and firewall test tools were configured after its application hook succeeded.
Runtime commissioning used the bundled `/opt/thermite/venv` application; no source
checkout substituted for it.

## What the completed evidence establishes

Production checks record two Things ONLINE, 26 Items, four requested add-ons,
answering authenticated native metrics endpoints and 16 Prometheus targets up.
Every final case records successful node_exporter systemd collection and
unit-state series, Grafana administrator login, denial of the default
administrator password, its provisioned datasource and an actual query returning
16 targets. Required native metric-series assertions belong to the strict boot
test's separate disposable runtime; the production evidence does not parse and
assert every such native series.

The application payload census verifies UID/GID 9001 across 12 application
services, including inspected container children and access main processes.
All cases record runc for all ten application containers. Ubuntu also records
enforced default AppArmor profiles without a crun stack and the dedicated
enforced profile for node_exporter. Root-managed systemd/Podman control processes
and the owner's simulator unit have different roles; this is not a claim that every process on
the machine is unprivileged.

The completed client and persistence checks cover:

- Authenticated HTTP sessions and personal tokens; anonymous/private-asset
  denial; simulated source/Off commands with native readback; locked-command
  rejection and worker repair after a direct simulated matrix change.
- Certificate-verified rendered browser controls and Users & Roles, installed
  TUI controller/events, and a populated real terminal quitting normally.
  Browser trust is isolated without TLS bypasses or host trust-store changes.
- Disabled native implicit roles and empty trusted networks; anonymous Item
  denial while the service bearer still works; external rejection of all four
  native TCP ingress paths with positive guest-filter counters, while verified
  gateway HTTPS continues to answer.
- A second worker rejected specifically by the shared singleton lease with
  `BlockingIOError`; staged custody verification and protected-plan replay.
- No-op comparison of recorded stable credential/identity/TLS/deployed-record
  fingerprints and service starts; after reboot, a changed boot ID and preserved
  stable fingerprints. This covers named files, not every userdata file or the
  active SQLite database.

The Ubuntu guard run deliberately declined before Grafana credential creation.
After reboot the actual unit skipped startup: no Grafana container or database
existed, and native API credential files were unchanged. The same artifact then
resumed successfully. Its deliberate installer exit 1 remains an expected
interruption, not a passing install exit. Full older-Podman prefetch replay used
actual Podman 4.9.3 without the unsupported pull-policy flag and preserved
credentials/identity; it did not upgrade Podman to obtain compatibility.

Actual final native-unit stops succeeded with normal exit status zero, without
recorded AppArmor denials or forced container kills:

| Case | Podman / runc | Native stop | Held-stream closure |
|---|---|---|---|
| Stock Arch | 6.1.4 / 1.5.2 | 8.99 s | 15.14 s |
| Fresh Thermite Arch ISO | 6.1.4 / 1.5.2 | 11.94 s | 15.16 s |
| Ubuntu Server | 4.9.3 / 1.3.4 | 9.95 s | 15.20 s |

Authenticated held-stream shutdown is a separate check: bounded Uvicorn
shutdown closes the stream and re-raises SIGTERM. Systemd reports
`Result=success`, `ExecMainCode=2`, `ExecMainStatus=15`; that is successful
shutdown, not a zero-process-exit-status claim. All owned guests subsequently
completed clean poweroff.

The passive display remains `passive_endpoint:example_tv`, with degraded
enforcement. This fixture establishes no physical picture/sound, hardware
compatibility, multicast discovery or callback delivery, supported household
capacity, or tested minimum resource allocation. No physical household has
been commissioned.

## Corrections and retained failed attempts

- Custody now uses an operator-owned protected plan, finite inode/type
  manifests, trusted helper code and protected activation-unit copies. Its
  descriptor-based preflight rejects replaced paths, unsafe hardlinks and mount
  crossings; virtual-environment links are handled without following them.
- Private traversal and inherited setgid handling were corrected. Ubuntu
  umask 002 exposed owner credential directories at 0775; ordinary-owner repair
  now narrows only the owned final directory to 0700 using no-follow descriptors,
  retaining existing credential bytes/inodes. Writes remain anchored to the
  protected parent. The root secret helper keeps its strict custody checks.
- The installation example places plans below a protected 0700 HOME parent,
  outside the service-writable site/release/state tree. Root activation reads
  only protected plan unit copies. Staging reuses existing service-owned 0700
  state without making its authority readable to the operator; no upgrade
  bootstraps an existing site.
- Strict boot discards incomplete/transient REST snapshots within an absolute
  header/body deadline, catches interrupted transport and retains complete
  assertion failures as failures. Fresh cache priming uses empty add-on
  configuration and waits for the actual temporary JVM to exit before normal
  startup. Failure retains its incomplete marker, cache and logs. Bounded
  authenticated loopback Karaf TCP shutdown and actual JVM observation replace
  unreliable child signals; original property bytes are restored only on exit.
- Native readiness requests Prometheus text rather than JSON, fixing HTTP 406.
  Token validation retries transient startup outages with the same paste, saves
  only after a timely authenticated Things success, and carries only that exact
  success through the current step. Fresh native readiness still checks add-ons,
  Thing handlers and metrics. Established-token replacement requires valid
  bounded runtime metadata; outage paths retain the saved credential.
- Final status refreshes the owner's newly granted group membership while
  retaining the selected site/interpreter and explicit privileged approval.
- Ubuntu Podman 4.9.3 predates Quadlet's `Entrypoint` key; the unit passes
  `--entrypoint=/bin/sh` through `PodmanArgs`. Prefetch detects its actual pull
  interface and uses the supported cache/pull fallback.
- runc is installed and selected explicitly for every application container and
  native preparation, avoiding crun/AppArmor signal stacking. Existing AppArmor
  confinement is retained, along with no-new-privileges, dropped capabilities
  and UID/GID 9001.
  Upgrade prerequisites run before any service stop. Native stop allows 60
  seconds of container grace inside systemd's 90-second allowance.
- Only node_exporter receives the dedicated policy permitting its read-only
  systemd D-Bus queries. The additive override requires Podman 4.5 or later.
  Conditional AppArmor packages, root-owned finite files, enforced-profile
  verification through the approved root helper and boot-time profile loading
  are prerequisites. The owner cannot assume securityfs is readable from its
  mode bits. Repairs persist a node-only recreation marker; current verification
  leaves starts unchanged. Enabled-AppArmor upgrades enter root verification
  even when disk artifacts are current. Readiness also waits for a successful
  systemd collector scrape rather than a cached earlier result.
- A partial-install reboot previously let Grafana initialize its default admin
  before the generated credential existed. That failed database is retained.
  Grafana now checks the secret as its actual payload identity before startup,
  including regular/readable/nonempty and successful nonempty read checks;
  `util-linux` supplies `setpriv`. Guarded interruption/reboot acceptance above
  checks absence of both the container and database before resuming.
- During stock Arch upgrade, Prometheus briefly returned an empty active-target
  inventory while older successful systemd collector samples remained queryable.
  The installer accepted `0/0`, then its final health check failed. Readiness now
  requires a nonempty target inventory. Two regressions and an actual stopped-
  metrics restart passed; all original authority files remained unchanged.
- A closed TCP connection in TIME_WAIT falsely appeared to occupy a stopped
  service port during replay. The probe now uses SO_REUSEADDR to match normal
  server restart behavior while a live listener still blocks. The prior failed
  prefetch attempt remains retained alongside the corrected immediate restart
  and full-prefetch proof.

Earlier report traceback/interrupted reboot, manual-bundle diagnostics,
startup token/status failures, Ubuntu primer/signal failures, Quadlet rejection,
graceless shutdown, private-directory/plan failures, collector denial and
default-admin initialization remain failed or incomplete attempts. No reset or
credential replacement converts them into clean acceptance. In particular,
stock Arch's accepted correction/resume remains distinct from a fresh final
installation, and `ubuntu-guard` does not rehabilitate the failed Ubuntu database.

## Persistence and evidence

Both access upgrades stopped the two access units and staged a new release
with the same state and original TLS/native-token inputs. Each used a new
protected plan and its protected unit copies, without bootstrap or replacement
CLI login. Quiescent checks preserved site identity, secret key and the actual
SQLite main file through staging/handoff; post-start comparisons covered stable
authority. New release TLS/token copies matched original bytes and their new
inodes were recorded; old copies and the saved CLI credential retained their
old fingerprints. Native userdata and administrator accounts were not recreated.

Every final case preserved all 12 service start timestamps and 11 named stable
file fingerprints during no-op installation and protected-plan replay. Reboot
required a new boot ID and the same stable fingerprints, with the native seal
persisting before repeated external ingress checks. These comparisons cover
named files, not every userdata file or the changing active SQLite database.

The final documentation pass reconciles [requirements](requirements.md) and
the committed [acceptance recipe](../tests/e2e/README.md). Its package comparison
and exact-commit checks accompany the publication commits. There is no physical
household commissioning, capacity certification or minimum-profile validation.

Logs, receipts, fingerprints and screenshots remain disposable private evidence
under clone-root `.scratch/e2e/` and `.scratch/browser-trust/`. Never commit or
upload credentials, private keys, state databases, VM disks, firmware stores or
installation media. Reconstruct missing guests from the committed recipe.
