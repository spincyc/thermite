# Installation review and acceptance

Updated 2026-11-03 UTC. This replaces the interrupted installation handoff.
Work belongs on `feature/productionize`; acceptance does not publish or merge
anything to `main`.

## Source and tested artifacts

The canonical implementation is `automation/openhab` in private
`spincyc/homelab`. Regenerate public `spincyc/thermite` from there with
`python3 bin/thermite docs export --with-code --to /path/to/thermite`.
Do not fix the generated public copy independently.

The earlier runtime candidate was built from homelab `cdd4b42` and
reproduced from public `09d4ad6`. Homelab `4d57495` adds the matching upgrade-test expectation;
it does not change the application artifact. Canonical and public wheels
are byte-identical:

- Application: `0.1.0.dev0`.
- Release: `7e0bd0190fb893f996f899ef3acbbfde0f77fc8d7da0f5b4f3e1f6ac4ac60044`.
- Wheel SHA256: `1b299ddb15da3ff993a0b01281c359c8008ff24aa5685083d1ad81d96ecd0339`.
- Tested Thermite Arch ISO SHA256: `88b76e7f3b1162db590b25e9e4c1bf43d86d30efa579f95aa419474e234bc1a3`.

For that candidate, source checks passed: 1,614 tests with 10 optional skips. Public checks passed:
145 tests. These include deterministic repackaging, export privacy, custody,
real HTTP transport, native preparation and installer lifecycle regressions.
Final evidence/documentation reconciliation is still in progress.

## Installation paths

Every case uses an independent disposable disk, KVM, host CPU exposure,
4 vCPUs, 8 GiB RAM, a sparse 64 GiB virtio disk, UEFI and an ext4 root.
The fictional fixture has one room, one computer source, an HTTP matrix and
an observe-only Sony display on separate guest-loopback addresses. The
committed [acceptance recipe](../tests/e2e/README.md) defines the procedure.

| Path | Actual result |
|---|---|
| Stock Arch plus application | Accepted after the documented application upgrade/resume described below. Final-release strict boot, installation, access clients, no-op handoff replay and reboot comparisons passed. |
| Thermite Arch ISO | Fresh firmware boot, native guided disk selection and destructive confirmation, owner/password/sudo creation, application-hook success, media removal and installed-disk password login passed. Fresh runtime commissioning and the complete client/no-op/reboot checks passed. |
| Ubuntu Server 24.04.5 plus application | Fresh stock OS installation, installed-disk login and strict boot passed. Native preparation then failed because the distro AppArmor policy denied child signals; this failed run is retained. The corrected primer passed a separate real pinned-image probe; fresh complete acceptance is pending. |

The Arch baseline is the official 2026.11.01 ISO with installed rolling
packages. Ubuntu uses the official 24.04.5 Server ISO and an external,
private test-only OS seed guarded to a blank 64 GiB `/dev/vda`; it is not
an unattended storage or identity default in a Thermite image. The optional
Thermite Ubuntu ISO has offline/build coverage, not a full guest installation
acceptance claim.

The Thermite Arch image's normal guided installer performed the OS and owner
setup. After its application hook succeeded, SSH and guest-only firewall test
tools were added for remote orchestration. Runtime commissioning used the
bundled `/opt/thermite/venv` application, not a source checkout.

## Checks completed on both Arch paths

Before and after a clean reboot, actual checks passed for:

- Two native Things ONLINE, 26 Items, four requested add-ons, required native
  metric series, and 16 Prometheus targets up. Grafana administrator login,
  its provisioned datasource and an actual `up` query succeeded.
- All 12 application services active under `thermite:thermite`, UID/GID 9001,
  with every inspected container payload and native access main process
  showing those effective identities. Service state and staged custody checks
  passed; the operator's fixture unit is separate test infrastructure.
- Authenticated HTTP sessions and personal tokens, anonymous/private-asset
  denial, source/Off commands with native readback, locked-command rejection
  and worker repair after a direct simulated matrix change.
- Certificate-verified browser login, rendered room controls and Users & Roles;
  installed TUI controller/events and a populated real terminal with normal
  quit. Browser trust was isolated without TLS bypasses or changes to the
  workstation's normal trust store.
- Disabled native implicit user role and empty trusted networks, with anonymous
  Item reads denied and the existing service bearer still accepted. Actual
  external forwards to all four native TCP ports were rejected, supported by
  positive guest filter counters; gateway HTTPS continued working.
- A second-worker singleton attempt rejected specifically with
  `BlockingIOError`. The same staged ownership plan replayed successfully.
- No-op installation preserving all 12 service start timestamps and 11 named
  credential, identity, TLS and deployed-record files' bytes, inodes, modes
  and owners. After reboot, a new boot ID and the same file fingerprints were
  required. This comparison starts after commissioning and does not cover
  every userdata file or the changing SQLite database.
- A held authenticated event stream closing about 15.2 seconds after service
  stop. Systemd reported success; Uvicorn completed its bounded shutdown and
  re-raised the original SIGTERM. This is not a zero exit-status claim or a
  host-forced VM power cut.

The worker correctly retained degraded enforcement for
`passive_endpoint:example_tv`. These runs do not verify a physical picture,
sound, hardware compatibility, multicast discovery, callback delivery, a
supported household capacity or the minimum planning profile.

## Review fixes and retained failures

The review corrected:

- Resumable custody escalation through service-owned path replacement. The
  saved operator plan now uses finite inode/type manifests, protected helper
  code, descriptor-based changes and preflight rejection of unexpected path
  replacements, hardlinks and mount crossings. Recorded virtual-environment
  symlinks are handled without following them. Root unit installation reads protected
  operator copies rather than mutable service-owned release files.
- Private directory traversal and inherited setgid modes, while retaining
  strict ownership and write protections.
- Boot-report collection during temporary REST registration outages, truncated
  replies and slowly arriving headers/bodies. Incomplete snapshots are
  discarded within one absolute deadline; complete assertion failures remain
  failures. Current real guest strict boot checks passed on both Arch paths.
- The native fresh-cache startup race. Declared add-ons could appear installed
  while their bundles remained resolved and Thing handlers uninitialized.
  Fresh, authority-free cache preparation now requires its temporary JVM to
  exit before normal startup. Failed preparation retains logs and an
  incomplete marker; established userdata and credentials are not reset.
  Authenticated readiness checks add-ons, handlers and native metrics, with
  at most one explicitly approved bounded recovery restart for known unchanged
  handler symptoms.
- Ubuntu primer shutdown: Podman 4.9.3 with its distro AppArmor profile denied
  TERM and KILL between confined child processes, exposing an unbounded wait
  after failed signal delivery. Preparation now uses Karaf's authenticated
  loopback shutdown protocol with a unique ephemeral port, bounded client and
  JVM observation, and idempotent cleanup. It restores the original properties
  only after actual exit. Thirteen focused tests and a real pinned-image
  Ubuntu probe passed with AppArmor unchanged; failed state remains retained.
- Native metrics content negotiation: the first fresh review candidate received
  HTTP 406 because readiness requested JSON from the Prometheus text endpoint.
  A real loopback HTTP regression now checks the request headers.
- Final installation status before a new login: the owner had been added to
  `thermite`, but the original process still lacked that group. Status now
  uses the existing owner group-refresh mechanism, preserving the selected
  interpreter/site and showing any required sudo command for approval.

The earlier handoff's report traceback and interrupted reboot were failed or
incomplete runs. A diagnostic native guest also needed manual bundle activation;
it is not a clean acceptance result. Those failures were retained while fixes
were tested.

Stock Arch's later `f19fb13c6f65` candidate passed native readiness and all
16 targets, then failed its final status read because of group membership.
The final application was installed using the documented force-reinstall and
`thermite init --upgrade` workflow, preserving inventory, pins, credentials and
native state. The changed site manifest was committed, strict boot and
installation were rerun, and only then was the no-op/reboot baseline taken.
This is accepted upgrade/resume coverage, not a failure-free fresh installation
of the final artifact. The Thermite Arch ISO provides that fresh-install check.

## Remaining work and evidence

Freeze the signal-free primer fix into a new reviewed artifact and rerun all
three fresh installation paths, including Ubuntu's actual older-Podman prefetch
fallback. Complete clients, no-op/handoff replay, reboot comparisons and bounded
stream stop. Then reconcile this record and [requirements](requirements.md),
regenerate the public export and run required checks against exact final commits.

Retained local logs, receipts, fingerprints and screenshots live under the
original workspace's clone-root `.scratch/e2e/` and `.scratch/browser-trust/`.
They are disposable private evidence, not part of a push. Never commit or upload
VM disks, firmware stores, credentials, private keys or installation media.
Reconstruct missing guests from the committed recipe. No physical household
was commissioned.
