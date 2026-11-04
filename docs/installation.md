# Install on a Linux server

Use an x86-64 machine running Arch Linux or Ubuntu Server 24.04 LTS.
Review the [system requirements](requirements.md) first. These are development
builds, currently `0.1.0.dev0`.

Bootstrap the application once, create a private site, then deploy it with
`thermite install --config DIR`. That command installs the native services and
HTTPS gateway together. Later, `thermite upgrade --config DIR` updates the
application and the complete installation. Installing a Python package alone
starts no service.
For a bare machine, use the [installer ISO](iso.md), then continue at
[Create your private site](#create-your-private-site).

For a stock OS installation, first follow the official
[Arch installation guide](https://wiki.archlinux.org/title/Installation_guide)
(or its guided `archinstall` installer), or
[Ubuntu Server installation guide](https://ubuntu.com/server/docs/installation/).
Create your own login with sudo access and working networking, then reboot
into the installed system. The commands below run there, not in the live
installer environment.

## Bootstrap the application once

Use your ordinary owner account. On stock Arch, install the application
prerequisites with a full system update:

```sh
sudo pacman -Syu --needed python python-pip git
```

On Ubuntu Server 24.04, use this package step instead:

```sh
sudo apt-get update
sudo apt-get install python3-venv git
```

Then use the same one-time bootstrap on either distribution. This environment
is outside the OS-managed Python installation:

```sh
git clone https://github.com/spincyc/thermite.git "$HOME/thermite-source"
python3 -m venv "$HOME/.local/share/thermite/venv"
. "$HOME/.local/share/thermite/venv/bin/activate"
python -m pip install "$HOME/thermite-source"
thermite --version
```

To install from a wheel instead, build one from the source with
`python3 bin/thermite package --out .scratch/releases` and pass the resulting
wheel filename to `python -m pip install`. PyYAML is installed as a dependency.
For offline application installation, supply PyYAML separately and install
the application wheel with `--no-index --no-deps`. Starting the runtime still
requires the pinned native downloads and container images.

The [ISO](iso.md) installs the application at `/opt/thermite/venv` and adds
`thermite` to PATH. Skip the bootstrap above on that machine. After deployment,
Thermite manages its own application environments and owner command; normal
updates need no pip invocation, environment activation or manual site-pin edit.

## Create your private site

```sh
thermite init "$HOME/thermite-site"
cd "$HOME/thermite-site"
```

Edit `instance/devices.yaml` and `instance/host.yaml` for your own equipment,
time zone and state directory. Init's inventory is fictional: use it only
for the mock example, then replace its devices before commissioning real
equipment. Keep the default gateway access mode. The gateway's persistent
Security switch controls household authentication separately; a new site
starts with that switch off.
Port assignments are listed in [ports](ports.md).

```sh
thermite check --config "$HOME/thermite-site"
git init --initial-branch=main
git add .
git commit -m 'Initialize private Thermite site'
```

Configure your Git author name and email if Git requests them. Keep this
repository private. Commit the site manifest, instance configuration and
`versions.json`. Generated `conf/`, `deploy/`, `metrics/`, credentials, caches
and runtime state stay outside Git. Init supplies the ignore rules. It creates no Git repository or
commit automatically.

Use the installed `thermite` command with `--config /path/to/site` from any
directory. The application remains outside the private repository; the site
needs no launcher or Makefile. `--site` remains a compatible alias.

## Deploy the complete site

Use a terminal on the server, as the owner account, with Internet access:

```sh
thermite install --config "$HOME/thermite-site" --dry-run
thermite install --config "$HOME/thermite-site"
```

Use `-v` for stage details and commands. Failures always show diagnostics;
interactive commands keep control of the terminal. `--dry-run` describes the
application selection and complete workflow without fetching, creating an
environment, changing the site pin or touching services. For an upgrade from
GitHub it cannot know the remote commit or its detailed changes without a fetch,
and says so.

Read the plan first. One approval covers application preparation, the private
site's release commit, native services and the HTTPS gateway, including their
sudo commands. Install uses the application release that launched it. It
prepares a private environment, installing its dependencies from the hashed
`lib/requirements-app.txt`, validates that release and the site, then commits
only the application requirement. Your inventory and native pins stay under
your control.

The host workflow checks architecture, kernel support, ports and IDs; installs
prerequisites; creates the service account and state directories; boots the
configuration on a test runtime; deploys the committed site; installs systemd
units; starts services; and provisions internal runtime access. It also stages
and activates the HTTPS gateway, its worker and required callbacks, backs up
existing identity state before migration, and verifies native authentication,
live service releases and HTTPS readiness. It switches
`~/.local/bin/thermite` to the prepared application only after every phase passes,
and a successful run warns when `thermite` on PATH is a different launcher.
No separate access staging, handoff or activation commands are required.

Each run has one native readiness allowance, 900 seconds by default. Runtime
startup, a settle before gateway commissioning, commissioning itself and the
final native readiness wait share it; time outside those phases, such as image
downloads, does not count. Give a slower host more with
`--native-timeout SECONDS` (300–7200). Gateway commissioning can reload native
authentication and jUPnP services, so the authoritative readiness check runs
after commissioning and service activation and requires ten uninterrupted
seconds of complete initialization. Only Thing configuration errors fail
immediately. Handler, bridge and device initialization (`UNINITIALIZED`,
`INITIALIZING`, `UNKNOWN`) is pending until the allowance ends; reviewed
pairing and connectivity problems stay visible warnings. A failed or
crash-looping `thermite-openhab.service` ends the wait early with a
`journalctl` hint.

The run ends with a read-only check, `thermite health --production
--installation --settle 120`. It polls Things still initializing for up to two
minutes and reports any that remain as warnings, not failures; configuration
errors, missing add-ons, monitoring and web-entry failures still fail.

Keep `~/.local/bin` on your shell's PATH. If you activated the bootstrap
environment above, leave it with `deactivate` after this first deployment and
use the stable owner command for subsequent runs. Do not run the whole CLI as
root. Rerun the same install command after correcting a reported failure;
each new run asks for approval again.

### Service ownership

Application services run as the non-login user and group `thermite`, UID/GID
9001, and deployed files belong to that account. Your login keeps ownership
of the private configuration and Git repository. The host's systemd manager
and Podman backend remain root-managed. The installer requires runc and selects
it explicitly for container payloads. This preserves AppArmor confinement on
Ubuntu 24.04, where crun can stack profiles under no-new-privileges and deny
application signals. Native shutdown gets a 60-second grace period. On AppArmor
hosts, node_exporter receives a separate policy for read-only systemd D-Bus
queries. Its container configuration applies only to that service; other
containers retain their default profiles. The installer presents policy loading
and enables AppArmor's boot-time loading when needed. Grafana startup waits for
a readable, nonempty administrator credential as the service user, including
after an interrupted installation and reboot. Existing administrator databases
and passwords are preserved.

The installer records the unit files it installs, with their digests, in the
root-owned `/etc/thermite/units.json`. A later run recognizes its own units from
that record, so time zone edits and template changes do not block an upgrade.
An unknown modification or an unowned drop-in stops for review; see
[service ownership](service-account.md).

### Startup and recovery

The native boot test downloads its pinned runtime and JRE. Before starting
or restarting a service, the installer downloads any missing pinned container
image with visible progress. Downloads finish before the service startup
timer begins. This can take several minutes. Pending steps explain their work,
and the terminal shows elapsed time while they run. The first configuration
boot check also warms a reusable test cache; later installs skip that check
when the exact configuration has already passed. Follow the
installer through its checks. It provisions private internal runtime access
automatically, without a separate account page or token paste. Existing valid
access is preserved; interrupted setup resumes from its private credential record.
On a fresh clone with no local credentials, it can recover the installed
service's token after verifying it against the local runtime, under the same
install approval. No account is reset. If no usable service copy remains,
restore the private site backup or supply an existing private credential with
`--runtime-token-file PATH`. See [using a fresh clone](packaging.md#use-a-fresh-clone-on-an-existing-host).
Log out and back in after the installer adds you to the service group.

For newly initialized native userdata, preparation builds the OSGi cache
with empty add-on configuration and waits for that temporary JVM to exit
before starting the real runtime. This avoids the upstream first-start
cache race. Existing userdata without a cache uses normal startup, so
offline preparation does not run its managed rules or Things.

After validating the API token, installation waits for the required add-ons,
Thing handlers and authenticated native metrics. Unchanged known first-start
handler symptoms can prompt one reviewed restart of `thermite-openhab.service` within
the native readiness allowance. Configuration and authentication errors require fixing
their reported cause. A persistent failure stops installation and retains
userdata and credentials. Failed fresh cache priming also retains its
`.thermite-prime-incomplete` marker and logs; inspect them before explicit
recovery. Preparation and normal startup refuse that incomplete cache.

On a slow machine or under software CPU emulation, give the boot check a
longer startup budget, then rerun the installer with a larger native
readiness allowance:

```sh
thermite test boot --config "$HOME/thermite-site" --timeout 1800
thermite install --config "$HOME/thermite-site" --native-timeout 1800
```

A failed check does not approve a deployment. The longer check must pass
against the same committed configuration before installation continues.
If a system update replaced the running kernel's modules, reboot before
commissioning so Podman and the native-port firewall can load their required
modules. Installation validates firewall access, policy ownership and the
required rules before stopping services or replacing units. Failed inspection
reports the underlying command's error; the validation does not apply rules or
change existing firewall tables.

The gateway uses the same verified application and generated client assets as
the native deployment. Existing household identities, authentication settings,
TLS material and callback routes are preserved across redeployment. Internal
runtime credentials remain separate from household identities.

### Open the controls

Open the site's root URL for Thermite Home; existing `/overview/` bookmarks
also lead there. The web and terminal interfaces share navigation, room and
device controls, pairing, diagnostics, camera status, topology, accounts and
room schedules. Camera media and specialist administration open in a browser
from an explicit terminal choice. Ordinary controls are available without a
login in either authentication mode. Guests can control unlocked rooms; locked
rooms retain the remote layout and show their lock status with controls disabled.
The Security page enables accounts. Each account is either an administrator
or an ordinary account, with no other roles or per-account room access.
Guests control unlocked rooms; ordinary accounts add camera snapshots and live
video while authentication is on; administrators also manage locks,
schedules, users, pairing and the system. Guests still see camera status.

Administrators can use **Lock room**, under Info on Remotes, to hold the current reported
source or Off until **Unlock room** is selected. The server captures the source;
an unknown source cannot be locked. This manual lock survives restarts and takes
precedence over schedules, even when the schedule itself is disabled. Unlocking
always succeeds and releases the manual hold; an active scheduled lock can still
keep the room locked. The lock blocks ordinary controls, including administrator
commands and All Off, and uses the same configured-device enforcement as
scheduled locks. Saved policies for rooms removed from the house remain stored
until `thermite access prune --config FILE`, run as the `thermite` account with
the gateway's service configuration file, deletes them.

Use System (F10 in the terminal) for deliberate access to openHAB, Grafana,
Prometheus, Alertmanager and ntfy. Local-only tools remain local and provide
SSH forwarding instructions. System's configured version is not a health
check; the following commands report live deployment and device health.

```sh
thermite status --config "$HOME/thermite-site"
thermite health --config "$HOME/thermite-site" --production
```

Health reports real device availability; fictional example addresses will
not establish a healthy house. Pair and verify equipment before relying on
automation; `thermite pair --help` lists supported pairing steps.

## Set up HTTPS and accounts

Installation prints the verified HTTPS URL and the path to its trust
certificate, `tls-cert.pem` in the gateway root (by default the state root
with `-access` appended). The installer creates a private certificate authority
for the site once, in `var/gateway/authority/` (directory mode 0700, key
0600, valid for 10 years), and serves an 825-day server certificate it signed
for the hostnames and addresses the installer selected. The trust file is the
authority's certificate: add it once to each browser or terminal client's
trust configuration. Client trust is local to each client; installing the
server cannot install trust in your other devices.

Each install or upgrade reissues the server certificate from the same
authority when fewer than 90 days remain, and clients need no change. Run an
upgrade at least every two years so the certificate does not expire.
An installation that still serves an older self-signed certificate replaces it
once, at its next install or upgrade, and prints a notice to trust the new
authority file on every client. Back up `var/gateway/authority/` with the
site's other private state: if it is lost, the next run creates a new
authority and every client must trust it again.

A new identity starts with authentication **off**. Ordinary house controls
remain available through HTTPS without a household login when authentication
is enabled; ordinary accounts can also view camera snapshots and live video,
and administrators manage locks, schedules, users, pairing and the system. Open **Security** to
enable accounts when ready. The first enable requires owner-authorized
setup and creation of the first administrator; visiting the page does not
claim ownership. Once an administrator exists, changing the switch requires
administrator authentication. Disabling it suspends locks and schedules while
preserving accounts, locks and schedules for later use; existing sessions and
personal tokens are revoked. See the
[identity reference](https://github.com/spincyc/thermite/blob/main/lib/oh_identity/README.md)
for owner setup and the account model.

The installer binds native HTTP/HTTPS to loopback and applies a dedicated
`nftables` table that blocks non-loopback access to native ports, including
the language server. It preserves other firewall tables, disables implicit native
user access and trusted-network grants, and verifies authenticated internal
access plus rejection of anonymous native requests. Existing callback routes
are retained. For supported Sonos devices it derives finite callback routes
from their committed identities and live descriptions; missing or inconsistent
evidence stops commissioning. These checks do not prove that a physical
device has delivered an event: verify device updates after commissioning.
Household clients use the gateway URL; the native loopback service remains
available to the gateway and monitoring.

Commissioning writes jUPnP's callback port as an integer string, as required by
the pinned native runtime. An interrupted upgrade that stored a decimal string
repairs it when rerun; no manual configuration edit is needed. If commissioning
times out, its error identifies the operation waiting for readback without
printing private native settings or credentials.

The local terminal automatically uses the verified gateway URL and the
authority certificate saved by installation. Run `thermite remote --config DIR` for
ordinary controls without a login. To view cameras or administer the site, log in with
`thermite access login --config DIR --user USER`. This creates a personal
credential; installation itself creates no household account. Other computers
need the server's URL and a copy of `tls-cert.pem`, passed once with
`--ca-file` at login.

## Update or remove

Use the same owner command from any directory; `--native-timeout SECONDS`
applies to upgrades as it does to installs:

```sh
thermite upgrade --config "$HOME/thermite-site" --dry-run
thermite upgrade --config "$HOME/thermite-site"
```

Upgrade fetches the official application's `main` branch after approval,
prepares or reuses a verified private environment, updates and commits the
site's exact application requirement, and runs the complete native and gateway
workflow. It deploys shared application changes and restarts the runtime when
needed, migrates identity state after a complete stopped-state backup, checks
authentication and HTTPS, then switches the owner command. There is no separate
pip, site-pin, native-runtime or access-release update sequence. For a reviewed
local checkout or an explicit Git revision, use `--source DIR` or `--revision REV`;
see [application releases](packaging.md#update-and-roll-back).

The site's committed `versions.json` still selects native component versions.
Updating Thermite does not silently replace those pins. Keep the private
repository clean and retain `var/application-releases/`, `var/gateway/authority/`,
previous application environments, gateway releases and the printed backup
paths for recovery. Application environments install their dependencies from
the hashed `lib/requirements-app.txt` and are rebuilt automatically after a
Python minor-version upgrade; the previous environment is retained.

Preparation or validation failures restore the bounded site edits when safe;
concurrent user edits are preserved. A host failure retains the committed
candidate pin and previous owner launcher, and prints a Resume command that
names the fetched commit with `--revision <commit>`. Rerun that command after
fixing the cause. Until then, the installed `thermite` runs site commands with
the site's pinned release from `var/application-releases/<digest>` and says so.

If a run fails after stopping services, it starts them again and says so;
the installation remains incomplete until a rerun succeeds. Ctrl-C lets the
running host step stop its own commands and then prints the command that
restarts any stopped services, without running it. Before commissioning
replaces a native service configuration, it saves the original privately in
the gateway deployment directory.

The launcher staying on its previous application does **not** undo services
already changed, and does not restore native data or an identity database.
To go back, follow the printed recovery advice: restore the printed backup
when one was made, revert the site's release-pin commit, then run the older
release's cached launcher, `python3 var/application-releases/<digest>/bin/thermite
install --config DIR`; that install also reinstalls the release's units.
Pinning a release older than the site's committed one requires
`--allow-downgrade`, after deliberately restoring state the newer release may
have migrated. Recover native and identity state from their corresponding
backups only with the appropriate compatible application release; do not
initialize new accounts or replace credentials merely to retry.

`--dry-run` prints a provisional host plan only when the site pins the release
that is running it; otherwise the host plan is resolved after approval.

If a missing kernel module stopped an older installer after services were
stopped, reboot into the installed kernel and then rerun the upgrade. The
reboot alone does not complete deployment: the upgrade must install and verify
the native-port boundary before starting the runtime. Keep the printed backup
until the upgrade succeeds.

Runtime and monitoring readiness poll within their allowances. Once the
required readiness or settling condition passes, that result completes the step;
an extra request outside the wait loop cannot fail it during a brief startup
interruption. Each later check and deployment run still queries the running
services. Dropped, refused or transiently failing native reads retry until the
native deadline; writes are never repeated, and credentials are not replaced.

`thermite uninstall --config "$HOME/thermite-site" --dry-run` shows service
removal, including the boot-time `thermite-native-boundary.service`. State is
retained by default; review the explicit purge option separately. Uninstall
keeps the HTTPS gateway units, their identity state in `<state_root>-access`
(accounts, sessions and the TLS key) and the native port filter (the
`inet thermite_native` nft table and its rule file), and prints the commands
that remove each. Back up private configuration, runtime data, credentials,
`var/gateway/authority/` and release artifacts before deleting an
installation.

## Native lifecycle regression

Developers can run the commissioning regression with cached public dependencies:

```sh
THERMITE_JAVA=/path/to/pinned-jre/bin/java \
THERMITE_RUNTIME=/path/to/openhab-distribution \
THERMITE_RUNTIME_ARCHIVE=/path/to/pinned-openhab.zip \
THERMITE_SONOS_JAR=/path/to/pinned-sonos.jar \
python -m unittest tests.test_native_gateway_lifecycle -v
```

The test checks the pinned distribution archive checksum and runtime versions,
then boots fresh configuration in isolated Linux user, network and PID
namespaces. It makes no physical-device requests and copies no live userdata.
It exercises real authentication, configuration and Sonos handler reloads,
including the full readiness settling interval; only metrics availability is a
test fixture. It verifies that credentials and the JVM survive unchanged, and
stops the disposable runtime on success or failure. `unshare` and `iproute2` are
required. Ordinary discovery reports an explicit skip without these optional
dependency paths. The archive defaults to the adjacent sandbox download cache;
the Sonos path can be omitted if its jar is already in the distribution.
