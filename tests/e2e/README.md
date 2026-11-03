# Fictional-house guest acceptance

This is a maintainer recipe, not a record of passing installations. Run it
independently for each installation path below and retain the actual evidence.
Syntax checks, a packaged wheel, an ISO build and a responsive login page do not
establish end-to-end acceptance.

| Case | Starting point | Additional evidence required |
| --- | --- | --- |
| Thermite Arch ISO | Fresh virtual disk, booted from the reviewed default ISO | Actual OS installation, reboot into installed Arch and application present |
| Stock Arch plus application | Fresh installed Arch x86-64 guest | Application installed by the documented owner workflow |
| Ubuntu Server plus application | Fresh installed Ubuntu Server 24.04 LTS amd64 guest | Application installed by the documented owner workflow |

Use disposable guests with no household credentials and keep every fixture
device address on guest loopback. Record the OS/image
identity, artifact digest, CPU/RAM/disk allocation, acceleration, effective ports
and timings. See [requirements](../../docs/requirements.md) for planning profiles;
this fixture cannot establish capacity limits. Prepare artifacts before a live
run, run one guest at a time and shut it down cleanly before starting another.

All `sudo` commands in this recipe run **inside the disposable guest**. They are
owner-reviewed guest operations, not instructions to change the workstation or
a production server. External browser and ingress checks are identified
separately below.

Prepare the application and private site.

Follow the [installation guide](../../docs/installation.md) for the selected
application artifact. On stock Arch, the initial package step is:

```sh
sudo pacman -Syu --needed python python-pip git curl openssl nftables
```

On stock Ubuntu Server 24.04, use:

```sh
sudo apt-get update
sudo apt-get install python3-venv git curl openssl nftables
```

The interactive runtime installer installs Podman, make, Node.js, npm and its
other service packages. Reboot the guest first if its OS update replaced the
running kernel's modules. On stock OS cases, install the reviewed application
wheel in the owner's virtual environment; on the Thermite ISO case, use the
application installed at `/opt/thermite/venv`. Do not substitute a source checkout
for the application-installation case being tested.

Set `APP_VENV` to that environment, then run as the ordinary guest owner:

```sh
export PATH="$APP_VENV/bin:$PATH"
APP_PYTHON="$APP_VENV/bin/python"
PROGRAM=$("$APP_PYTHON" -c 'from pathlib import Path; import thermite_app; print(Path(thermite_app.__file__).resolve().parent / "runtime")')
SITE="$HOME/thermite-site"
thermite --version
thermite init "$SITE"
TEST_ROOT="$SITE/.scratch/e2e"
mkdir -p "$TEST_ROOT"
chmod 0700 "$TEST_ROOT"
"$APP_PYTHON" "$PROGRAM/tests/e2e/fixture.py" > "$TEST_ROOT/dummy-house.json"
cp "$TEST_ROOT/dummy-house.json" "$SITE/instance/devices.yaml"
cd "$SITE"
thermite generate
thermite validate
git init --initial-branch=main
git config user.name 'Installation fixture'
git config user.email 'fixture@example.invalid'
git add .
git commit -m 'Initialize fictional installation test house'
```

Keep the generated fictional host defaults for comparable cases: native state
at `/srv/thermite`, authenticated HTTPS at **13600** and native HTTP/HTTPS at
**13601/13602**. Inspect `instance/host.yaml`; record and propagate any intentional
overrides. Generated files and their matching inventory must be committed before
installation. The site's `.scratch`, `var` and `build` directories are ignored;
credentials, runtime state and screenshots never enter the commit.

The [fixture](fixture.py) derives fictional devices and MACs from the public
demo. It has one routed room, `example_west`, one source,
`example_computer`, an HTTP-controlled BK-808 matrix and an observe-only Sony
display. Its generated configuration points to separate guest-loopback
addresses. The computer has no integration or listener. There are no Sonos
devices or callback routes.

Start the protocol stand-ins, then test the real generated configuration.

The [simulator launcher](dummy-tv.py) uses the existing Sony and BK-808 protocol
implementations on guest-loopback HTTP port 80. Run it as the guest owner with
only the capability needed to bind that port:

```sh
sudo systemd-run --collect --unit=thermite-e2e-devices \
  --uid="$(id -un)" --gid="$(id -gn)" \
  --property=AmbientCapabilities=CAP_NET_BIND_SERVICE \
  --property=CapabilityBoundingSet=CAP_NET_BIND_SERVICE \
  --property=NoNewPrivileges=yes \
  "$APP_PYTHON" "$PROGRAM/tests/e2e/dummy-tv.py" "$PROGRAM"
sudo systemctl status thermite-e2e-devices.service
thermite test boot --timeout 1800 --require-online
```

This unit is **test infrastructure**, deliberately owned by the operator; it is
not a production Thermite service. Production application payloads must still
run as `thermite:thermite`. Stop the existing fixture unit before replacing its
script or starting a replacement. Do not start a second simulator on port 80.

The 1800-second boot budget is an allowance for a cold runtime under software
emulation; it does not turn a timeout or stalled add-on into a pass. Preserve
the failed report/logs, confirm its test process stopped, diagnose the failure
and rerun against the same committed configuration. Never manufacture a boot
receipt, remove add-ons or allow offline Things to obtain a green result.
`--timeout` belongs after `boot` in this command.

Required native metric-series assertions here concern the disposable boot-test
runtime. Record production native endpoints, Thing/add-on readiness and monitoring
targets separately; do not imply that the production series were all parsed.

Commission the native stack interactively as the owner:

```sh
thermite install --dry-run
thermite install
thermite status
thermite health --production
```

Review the dry plan and each privileged command. Complete actual native
administrator creation and provide a valid API token at the hidden installer
prompt. Do not run the whole installer as root or substitute canned HTTP replies
for readiness. Both HTTP Things must become ONLINE and the real metrics checks
must succeed. Record failures as failures. A stopped or failed step is resumed by
rerunning the installer after addressing its cause.

For a fresh native state, retain the preparation message confirming that its
temporary cache-priming JVM fully exited before native service startup. A
failed priming marker is a failed run requiring diagnosis; preserve its logs
and state. The subsequent authenticated readiness check must pass without
removing add-ons or replacing credentials.

On a fresh Ubuntu case, also exercise interrupted installation before Grafana's
administrator password exists. After creating the native administrator and
validating its token, decline the Grafana password step. Retain the expected
nonzero installer result and private fingerprints of both saved native-token
files. Reboot the same disk and artifact. Require the actual Grafana unit to
skip startup, with no Grafana container or database created and both token-file
fingerprints unchanged. Resume the owner installer with the same native state
and credentials. Verify that Grafana accepts the generated password and refuses
the default administrator password. Preserve a failed existing database for
diagnosis; deleting it or resetting its password is not this regression test.

During initial startup and metrics restart, Prometheus can briefly report no
active targets while older successful collector samples remain queryable.
Require a nonempty target inventory with every target up and a successful
systemd collector scrape before final health checks; `0/0` is not readiness.

Set up the authenticated gateway.

Follow [HTTPS and accounts](../../docs/installation.md#set-up-https-and-accounts),
including a new login for membership in group `thermite`, explicit ownership
handoff and service-user verification. Use `/srv/thermite-access` outside native
state. For this fixture, issue a test certificate covering `localhost` and
`127.0.0.1`, retain its key privately and trust its public certificate explicitly
in each test client. Stage with `--allowed-host localhost --allowed-host 127.0.0.1`,
port 13600 and the native loopback origin from the guide. Do not use a private owner-home path for service
releases or state.

Use these names consistently for the remaining guest commands:

```sh
ACCESS_ROOT=/srv/thermite-access
RELEASE="$ACCESS_ROOT/releases/first"
URL=https://127.0.0.1:13600
CERT="$TEST_ROOT/tls/ca.pem"
ADMIN_PASSWORD_FILE="$TEST_ROOT/access-admin-password"
MATRIX_URL=$("$APP_PYTHON" -c 'import json,sys; data=json.load(open(sys.argv[1])); print("http://" + next(d["ip"] for d in data["devices"] if d["id"] == "example_matrix"))' "$TEST_ROOT/dummy-house.json")
```

`CERT` must name the actual CA or trusted self-signed certificate that validates
the staged server certificate, not a placeholder file. `ADMIN_PASSWORD_FILE` must be an owner-owned, regular,
non-symlink mode-0600 file containing the same gateway administrator password
entered at bootstrap. A private PTY harness may feed its contents to the two
hidden prompts; never pass the value in arguments, environment or transcripts.
This gateway administrator is separate from the native runtime administrator.

Use the guide's `--plan-directory` and review the saved `handoff.sh`. Run it
as the ordinary owner to apply its finite custody plan and service-user
verification. Keep the operator-owned plan readable after handoff and test
replaying it without replacing credentials or state. Bootstrap
only this genuinely new site, as `thermite:thermite`. Complete the native cutover
checks below, then install/enable the staged access and worker units. This
fixture must stage exactly two units and no callback listener. Reuse the same
state on reruns and after reboot; never bootstrap it again.

Run the real HTTP and terminal acceptance helpers.

```sh
"$APP_PYTHON" "$PROGRAM/tests/e2e/check_access.py" \
  --url "$URL" --ca-file "$CERT" --admin admin \
  --password-file "$ADMIN_PASSWORD_FILE" --matrix-url "$MATRIX_URL" \
  --timeout 180
thermite access login --url "$URL" --user admin --ca-file "$CERT"
thermite remote --setup
```

The login command prompts privately and reports its credential filename. Set
`CLI_CREDENTIAL_FILE` to that reported path, without displaying its contents:

```sh
node "$PROGRAM/tests/e2e/check_tui.cjs" \
  --url "$URL" --room example_west --program "$PROGRAM" --site "$SITE" \
  --credential-file "$CLI_CREDENTIAL_FILE" --ca-file "$CERT" \
  --admin admin --timeout 180
thermite remote --url "$URL" --room example_west --still --no-log
```

Retain each helper's exit status and JSON proof. The final command is a separate
real-terminal smoke: inspect the populated room and quit normally. The Node
helper exercises the installed TUI controller, state reads and authenticated
SSE; it does not draw a terminal. It checks the same locked blessed installation
as `remote --setup`, and passes credential/CA paths rather than token values.

The HTTP helper checks anonymous denial, browser CSRF/session login, personal
token access, private assets, room commands, locked-command rejection and worker
repair after a direct simulated matrix change. It clears its lock and revokes
its in-memory token in `finally`; it does not change the CLI credential file.
It performs two password logins. Reuse the CLI credential after reboot and
respect the real login throttle when repeating checks; do not bypass it.

The matrix must read input 8 for Off and input 1 for Computer. The Sony display
is passive: `passive_endpoint:example_tv` must remain an enforcement limitation,
and enforcement remains **degraded**, even when the matrix is matched. Never
label this a verified physical display, a tested real device or a capacity
benchmark. The simulator resets its own device state when restarted.

Run the browser check from the external browser host.

Provision Playwright/Chromium and a launcher exporting
`async launchTrustedContext(caFile, options)` that returns a real Playwright
`BrowserContext`. The launcher must isolate test CA trust and retain normal TLS
verification; the repository does not install this external orchestration
dependency. Chromium's [Linux certificate documentation](https://chromium.googlesource.com/chromium/src/+/main/docs/linux/cert_management.md)
describes NSS trust. An isolated NSS database/mount namespace can supply it
without changing the operator's real HOME or trust store. Do not use
`ignoreHTTPSErrors`, certificate-error flags or disabled Node TLS verification.

Use the reviewed source checkout matching the guest artifact as `CHECKOUT`.
Set `HOST_SCRATCH` to its case-specific `.scratch` directory, `TRUST_HELPER` to
the absolute launcher filename, and `FORWARDED_GATEWAY_URL` to the external
loopback HTTPS forward to guest port 13600. Transfer the public certificate and
the dummy gateway password file securely; keep the latter owner-owned mode
0600. Create the screenshot parent beforehand and choose a new filename:

```sh
TMPDIR="$HOST_SCRATCH" node "$CHECKOUT/tests/e2e/check_browser.cjs" \
  --url "$FORWARDED_GATEWAY_URL" --ca-file "$HOST_CERT" \
  --admin-password-file "$HOST_ADMIN_PASSWORD_FILE" \
  --screenshot "$HOST_SCRATCH/remotes-initial.png" \
  --trust-helper "$TRUST_HELPER" --admin admin \
  --room example_west --source example_computer --timeout 180
```

The screenshot destination must be new and resolve below `.scratch`. Retain the
JSON proof and populated Remotes screenshot privately. The browser runner checks
real login, source/Off/source clicks and native readback, and the admin Users
page; it leaves Computer selected. A missing browser/trust helper is an unmet
prerequisite, not a reason to replace rendering with an HTTP-only claim.

Complete this cutover and persistence checklist for every case:

- Verify effective service users/groups and private state modes, plus the
  staged custody check. The owner fixture unit is the documented exception;
  root-managed systemd/Podman control processes are not application payloads.
- Record the actual Podman and runc versions and each container's recorded
  OCI runtime. Every generated payload must use runc. On Ubuntu, inspect the
  actual process AppArmor labels: require an enforced container profile without
  a stacked crun peer. Keep no-new-privileges and dropped capabilities enabled.
  Exercise the distribution's real older-Podman image-prefetch path; do not
  upgrade Podman to bypass a compatibility failure.
- Turn off native `implicitUserRole` and clear `trustedNetworks` through native
  administrator settings or its authenticated configuration API. Preserve other
  settings and check readback. Configuration reload can briefly unregister its
  REST resource; retry within a bounded deadline before declaring readback ready. Anonymous native Item reads must be refused while
  the gateway's service bearer still works; `/rest/` metadata alone is not an
  authentication test.
- Review and apply an explicit **guest-only** native-listener seal. This recipe
  does not modify production firewalls or invent callback maps. Inventory actual
  listeners against [ports](../../docs/ports.md): 13600 is authenticated HTTPS;
  13601/13602 are native HTTP/HTTPS, 13603 is loopback Karaf and 13604 is LSP.
  Native HTTP ports also carry websocket and alternate-UI paths. Investigate any
  legacy listeners instead of assuming generation removed them.
- From outside the guest, probe each native ingress path and the gateway after
  sealing. A forward to 13600 is not a native-HTTPS forward to 13602. An
  unforwarded port or a guest request to its own address does not establish
  external blocking. Require no native HTTP/TLS response and record the applied
  filtering evidence; HTTP 401/403 still proves the listener is reachable.
  Authenticated gateway requests must continue to succeed with verified TLS.
- While the worker service is active, prove its singleton lease: as
  `thermite:thermite`, use the staged program's `oh_room_policy.FileLocks` on
  `$ACCESS_ROOT/state/locks` and attempt `hold(["room-policy-worker"], blocking=False)`.
  Require `BlockingIOError`. A generic second-worker failure is insufficient.
- Rerun `thermite install` and the relevant helper checks with the same committed
  site and persistent access state. Preserve identities, users and credentials.
  Private credential verification may legitimately request another reviewed
  helper command; idempotence does not mean zero privileged prompts.
- Reboot the guest cleanly. The `systemd-run` fixture is transient: recreate it
  before post-boot device checks, or explicitly provision a persistent guest-only
  fixture unit and record that choice. Ensure the native seal is also persistent
  or explicitly reapplied before repeating external-ingress assertions.
- Repeat health, access/TUI/browser checks and the singleton probe after reboot.
  Use a new screenshot filename. Compare stable identity and credential material
  privately; do not expect the entire SQLite file to remain byte-identical after
  token, audit and policy operations. Do not reinitialize accounts to obtain a
  pass. Record the artifact, resource allocation, actual outcomes and remaining
  limits for all three cases before publishing any acceptance result.

After all running-service, client and persistence checks, test native shutdown
through its actual generated unit:

```sh
sudo systemctl stop openhab.service
sudo systemctl show openhab.service \
  --property=Result --property=ExecMainCode --property=ExecMainStatus
```

Require service success and a normal native exit, within the configured
60-second container grace and 90-second systemd allowance. Inspect the matching
unit and kernel journal window for forced SIGKILL or AppArmor signal denials;
`systemctl stop` returning zero alone is insufficient. Record the authenticated
held-event-stream gateway shutdown separately, then power off the guest cleanly.
