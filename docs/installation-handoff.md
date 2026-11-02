# Installation work handoff

Updated 2026-11-02. Work stopped at the owner's request and is being saved on
`feature/productionize` in `spincyc/homelab` and `spincyc/thermite`. This batch
is not being published to `main`. Resume VM testing only when requested.

## Source and scope

The canonical implementation is `automation/openhab` in the private homelab
repository. Generate the public Thermite repository from that directory with
`python3 bin/thermite docs export --with-code --to /path/to/thermite`.
Do not fix the generated public copy independently.

The branch contains:

- Arch as the primary existing-server example and default installer ISO base,
  using the native Arch guided installer. Ubuntu Server remains supported by
  the application guide, with an optional Ubuntu ISO implementation.
- Cleaner installation documentation, published planning requirements, and
  existing screenshots captured from the fictional web and terminal remotes.
- Default TCP listeners at 13600–13625, with HTTPS at 13600 and separate
  loopback shutdown-control ports. See [ports](ports.md) for protocol exceptions
  and the range-selection evidence.
- Application services and deployed data under `thermite:thermite`, UID/GID
  9001, with explicit custody checks and migration from the old account name.
- Installer fixes for cold-start budgets, image prefetch, token readiness,
  native permissions, and early configuration/Git checks.
- Access staging with a private operator plan and resumable ownership script;
  corrected systemd quoting, cold worker startup, and bounded event-stream
  shutdown.
- A bounded boot-report snapshot reader and regressions for temporary REST
  outages. This last fix is unit-tested but has not yet passed a guest rerun.

## Actual verification status

All three requested installation paths still need final acceptance. Do not
replace the pending statement in [requirements](requirements.md) with a
three-path success claim.

| Path | Evidence obtained | Still outstanding |
|---|---|---|
| Stock Arch plus application | Installed application and native stack; authenticated web/API/TUI controls; ownership, ports, credentials and no-op rerun checks | Guest rerun of the latest boot-report fix; complete reboot acceptance and final client/custody comparisons |
| Thermite Arch ISO on a blank disk | Remaster implementation, prototype build and native-installer source review | Build current image; actual firmware boot, guided disk installation, no-CD first boot, and full runtime/client acceptance |
| Ubuntu Server plus application | Official base cached; procedure and compatibility reviewed | Fresh guest application install and full runtime/client/reboot acceptance |

The stock Arch guest used UEFI, 2 vCPUs, 4 GiB RAM and a sparse 64 GiB disk
under QEMU software emulation. Its inventory was fictional: a simulated Sony
display and HDMI matrix on separate guest-loopback addresses, with one room
and computer source. This establishes neither hardware compatibility nor a
supported household capacity.

Before reboot, actual checks passed for:

- Two native Things ONLINE, 26 Items, four add-ons, and 16 metrics targets up.
- All 12 application services and every inspected container payload process
  using UID/GID 9001; native and access-state custody; assigned TCP listeners.
- Admin/session/token access, source and Off commands with native readback,
  policy rejection, worker drift repair, and a second-worker lease rejection.
- Certificate-verified browser TLS, login, room controls and Users & Roles;
  installed TUI controller/events and an interactive authenticated terminal.
- Grafana admin authentication, its provisioned Prometheus connection and
  actual target queries; rejection of four external native ingress ports.
- No-op installation preserving service start times, deployed record, site
  identity and credential bytes/inodes. Replayed ownership handoff preserved
  existing state. A held authenticated event stream closed during service stop.

The worker correctly reported a degraded passive display endpoint; the fixture
does not verify a physical picture, sound, discovery or callback delivery.

## Last failure and interrupted reboot

The strict warm boot command was
`thermite test boot --timeout 1800 --require-online`. Both simulated Things
became ONLINE, then report collection encountered HTTP 404 while several REST
resources were being registered. It exited with a traceback. That run failed.

The committed fix discards incomplete snapshots, retries transient registration
or network outages within the original deadline, and requires fresh settling.
Terminal HTTP/JSON errors exit cleanly. Complete snapshots with failed Thing,
add-on, model or metric assertions are not retried. Forty-three focused mocked
tests passed. The documented three-second disposable-runtime cleanup is unchanged.
Run the real guest check again before accepting this fix.

A subsequent clean guest reboot reached a new boot ID. Services started
automatically; the installer found the existing deployment current. The saved
native token initially received 401, then recovered through the new readiness
wait without requesting replacement. Service-token verification reported no
change and the Grafana password was preserved. Metrics were 15/16 up when the
owner requested stopping. The installer then exited 255 as SSH closed during
the requested poweroff; this is an interrupted run, not an acceptance pass.

The guest completed ACPI poweroff and no QEMU process remains. No further guest
tests or ISO builds should be inferred from this handoff.

## Continue when authorized

1. Run the current branch's checks and build a fresh application artifact. An
   earlier prepared wheel contains the boot-report fix, but predates this
   handoff; do not assume it identifies the final branch bytes.
2. Finish stock Arch acceptance with the latest installed application. Reuse
   existing native credentials and persistent access state; do not bootstrap
   the existing site again. Recheck strict boot, no-op behavior, reboot,
   identities/modes, credential preservation, native ingress, metrics, HTTP,
   browser and TUI. Retain failures as well as successful evidence.
3. Build the current Arch ISO and follow [ISO installation](iso.md) on an
   explicitly disposable blank disk. Review the native storage confirmation,
   create the owner login, require the Thermite hook success, remove the media
   and prove installed-disk login before commissioning the dummy house.
4. Run the same acceptance on fresh Ubuntu Server. Exercise its actual older
   Podman image-prefetch fallback; do not bypass it by upgrading Podman merely
   for the test. Run the fixture with the application environment's Python,
   which has PyYAML, and resolve the guest's actual `nft` executable path.
5. Update evidence and resource guidance only after those runs. Regenerate
   the public repository, run each repository's required checks against its
   exact commit, and publish only the targets the owner authorizes.

The committed [fictional-house recipe](../tests/e2e/README.md) provides the
portable procedure. In the original homelab workspace, optional retained
local evidence and VM helpers are under clone-root `.scratch/e2e/`, with later
checkpoints in `CURRENT.md`, ISO preparation in `.scratch/iso-ui/`, and Ubuntu
preparation in `.scratch/ubuntu-prep/`. These ignored files are disposable and
are not part of either push. They include private test state: never commit
them or upload VM disks, credentials, firmware stores or installation media.
If they are absent, reconstruct the guests from the committed recipe.

The original test host has no usable `/dev/kvm`; its kernel reports VT-x
disabled in firmware. Enabling Intel virtualization and rebooting the host
would speed testing. Shut down any guest cleanly before a host reboot. Software
emulation remains usable. No physical household devices were commissioned.
