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

Edit `instance/devices.yaml` and `instance/host.yaml` for your own equipment
and time zone; see [host layout](#host-layout) before adding a `root`.
Init's inventory is fictional: use it only
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

### Host layout

Everything the installation keeps under `/srv` lives in one directory, the
installation root:

```text
/srv/thermite/    root:root 0755; a btrfs subvolume when /srv is btrfs
├── runtime/      thermite 0750: the runtime state and the thermite account's home
├── access/       you, 0755: the HTTPS gateway root
└── .transaction/ root 0700: the install engine's transactions
```

`runtime/` holds the native runtime's configuration, userdata, logs and
add-ons, the Apple TV helper, secrets, the deploy records and the monitoring
data. `access/` holds the gateway's `releases/` (one per install transaction,
`releases/<id>`), its identity `state/` (thermite, 0700), its fixed `inputs/`
(the served TLS certificate and key and the verified callback map; you, 0700)
and the clients' trust certificate `tls-cert.pem`; an installation from before
the install engine also keeps its old identity `backups/` (root, 0700), which
nothing writes or reads any more. Neither directory is inside the other, so
the gateway's custody checks see only root- and operator-owned parents.
`.transaction/<id>/` holds the version an upgrade set aside, until the next
upgrade commits; see [how an upgrade runs](#how-an-upgrade-runs). On btrfs the installation root is the one subvolume to snapshot or
back up: `sudo btrfs subvolume snapshot -r /srv/thermite /srv/thermite-$(date +%F)`.

openHAB's logs are in `runtime/logs/`, `/srv/thermite/runtime/logs` by
default: `openhab.log`, `events.log`, their compressed rotations
(`events.log.1.gz` up to `.7.gz`, the highest number the newest) and the
updater's `update.log`. The directory belongs to `thermite`, mode 2750, and
those logs are 0640, so members of the `thermite` group read them without
sudo; they write nothing there, anything else in it stays private, and the
rest of `runtime/` gains no access. The pairing filter drops pairing codes on
every logger before a line reaches a file. An installation upgraded from a
release that logged inside userdata keeps those older logs in
`runtime/userdata/logs`, readable only with sudo; delete them once you no
longer need them.

```sh
tail -F /srv/thermite/runtime/logs/openhab.log
grep -h 'received command' /srv/thermite/runtime/logs/events.log | tail -n 20
zgrep -h 1a2b3c4d /srv/thermite/runtime/logs/events.log.*.gz
```

The installer creates this layout. A host that needs another location sets
`root: /absolute/path` in `instance/host.yaml`; `runtime/` and `access/` are
always its children. Unit files, `/etc/thermite`, the owner's
`~/.local/share/thermite` and the private site's `var/` stay where they are.

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
TLS material and callback routes are preserved across redeployment; the names
it answers to, its listening address and its timezone come from this host and
`instance/host.yaml` on every run. Internal runtime credentials remain separate
from household identities.

### Open the controls

Open the site's root URL for Thermite Home; existing `/overview/` bookmarks
also lead there. The web and terminal interfaces share navigation, room and
device controls, pairing, diagnostics, camera status, topology, accounts and
room schedules. Camera media and specialist administration open in a browser
from an explicit terminal choice. Ordinary controls are available without a
login in either authentication mode. Guests can control unlocked rooms. A room
that is Off shows no remote until a device is chosen for its buttons. In a
locked room the bubble for what it shows, Off included, carries a padlock and
the others are greyed out; selecting the padlocked bubble explains the lock.
Room lists, on Home and Remotes, show a padlock after a locked room's source.
If a room's lock cannot be read, its remote says the status is unknown and
keeps working; the server still refuses commands to a locked room.
The Security page enables accounts. Each account is either an administrator
or an ordinary account, with no other roles or per-account room access.
Guests control unlocked rooms; ordinary accounts add camera snapshots and live
video while authentication is on; administrators also manage locks,
schedules, users, pairing and the system. Guests still see camera status.

Administrators can use **Lock room**, under Info on Remotes, to hold the current reported
source or Off until **Unlock room** is selected. The server captures the source;
an unknown source cannot be locked. To lock on a particular source, right-click
or long-press its bubble, or Off (in the terminal, right-click it or press `o`),
and choose **Lock on** it; another source is chosen first and the room is locked
only once it reports that source. This manual lock survives restarts and takes
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

Installation prints the verified HTTPS URL, on port 13601 unless
`gateway: {port: N}` in `instance/host.yaml` moves it (see
[moving the HTTPS application](ports.md#moving-the-https-application)), and the
path to its trust certificate, `tls-cert.pem` in the gateway root (`/srv/thermite/access` by
default; see [host layout](#host-layout)). The installer creates a private certificate authority
for the site once, in `var/gateway/authority/` (directory mode 0700, key
0600, valid for 10 years), and serves an 825-day server certificate it signed
for this host's name and LAN address, `localhost` and `127.0.0.1`, as found on
each run. The trust file is the authority's certificate: add it once to each
browser or terminal client's trust configuration. Client trust is local to
each client; installing the server cannot install trust in your other devices.

Each install or upgrade reissues the server certificate from the same authority
when fewer than 90 days remain, or when the host's name or address no longer
matches the names it lists, and clients need no change. Run an upgrade at least
every two years so the certificate does not expire. An installation that still
serves an older self-signed certificate replaces it once, at its next install
or upgrade, and prints a notice to trust the new authority file on every
client. Back up `var/gateway/authority/` with the site's other private state:
if it is lost, the next run creates a new authority and every client must trust
it again.

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

jUPnP advertises the callback relay's port, which `thermite generate` writes
to `conf/services/upnp.cfg` as an integer string, as the pinned native runtime
requires. openHAB applies it as it starts; commissioning only checks that
openHAB uses it, because changing it while openHAB runs leaves Sonos events
unanswered (HTTP 501) until openHAB restarts. If the check fails, deploy the
generated configuration, restart openHAB and rerun. If commissioning
times out, its error identifies the operation waiting for readback without
printing private native settings or credentials.

The local terminal automatically uses the verified gateway URL and the
authority certificate saved by installation. Run `thermite remote --config DIR` for
ordinary controls without a login. To view cameras or administer the site, log in with
`thermite access login --config DIR --user USER`. This creates a personal
credential; installation itself creates no household account. Other computers
need the server's URL and a copy of `tls-cert.pem`, passed once with
`--ca-file`: `thermite tui --url URL --ca-file FILE` opens the terminal remote
there without a private site (with no `--url` it opens `https://thermite:13601`;
with `--config DIR` it uses that site's `gateway.port`), and
`thermite access login` takes the same switches.

## Guest access over plain HTTP

Phones on a guest network rarely trust the site's private authority, so the
HTTPS site warns them on every visit. For them the gateway can open a second,
opt-in port that serves Home and Remotes, as a Guest, over plain HTTP. It is
off unless `instance/host.yaml` asks for it. For a host with one address on
the guest network:

```yaml
guest_http:
  listen: [198.51.100.2]       # this host's address on the guest network
  clients: [198.51.100.0/24]   # the networks allowed to connect
  # port: 13600                # the default
  # names: [remote.guest.example]
```

`listen` names this host's own addresses, never a wildcard; `clients` names
the guest network. The gateway closes a connection from any other address
before reading it, and logs how many it closed at most once a minute. Guests
open `http://198.51.100.2:13600/`, or a name from the optional `names` that
resolves to a `listen` address; a request naming any other host is refused.
Commit the change and run `thermite upgrade --config DIR`. `thermite status`
lists the Guest addresses, and install and upgrade check each one as a Guest
when this host's own address is inside `clients`.

The listener only ever answers as a Guest: Home, Remotes, room states and
lock status, commands to unlocked rooms and All Off. Locks and schedules
apply as they do over HTTPS. Sign-in, Security, accounts, cameras, pairing,
settings and System are not found there, and no answer sets a cookie. A
request carrying the site's session cookie or an `Authorization` header is
refused, so a credential sent over plain HTTP is never used. The page offers
**Sign in on the secure site**, which opens the Account page at the gateway's
first HTTPS address, on its configured port. The terminal remote uses HTTPS
only, and says so when pointed at the Guest port.

Weigh the trade-offs before enabling it:

- Anyone on the guest network gets Guest controls: every unlocked room, and
  All Off. Lock the rooms guests should not use.
- Nothing is encrypted. Someone who can intercept the guest network can read
  the traffic and alter the page, for instance to point its sign-in link at a
  look-alike that collects passwords. Sign in from a bookmark of the HTTPS
  site, never by following that link on a network you do not trust.
- HSTS can never be enabled for this host: browsers apply it to every port of
  a name, so they would refuse the Guest port. Thermite sends none.
- Guests share the gateway's 100 requests in flight with HTTPS clients, so a
  busy guest network can briefly make the HTTPS site answer 503 too.
- Bind the listener only to the guest network's address and keep `clients` to
  that network. The managed `nftables` table guards native ports only and
  leaves this port alone; on a host with its own firewall, open 13600/tcp
  (or your `port`) from the guest network, and from nowhere else.

## Trace a command

Every command gets an 8-character ID, such as `1a2b3c4d`. The gateway
(`thermite-access.service`) or the policy worker
(`thermite-access-worker.service`) logs it in one `cmd` line; openHAB
receives it as the command's event source, `thermite.<origin>:<id>`; and the
routing rule logs the route and each device step it caused with
`route=<id>` in `thermite-openhab.service`'s journal, which carries
openhab.log (events.log is only a file, in [openHAB's logs](#host-layout)).
One search therefore follows a command from the client to the devices.
Reading the system journal needs membership in the `systemd-journal` group
(or `adm`, `wheel` where the distribution grants it, or sudo); the log files
need only the `thermite` group.

Who turned the den off around 18:00, and from where:

```sh
journalctl -u thermite-access.service -u thermite-access-worker.service \
  -S '2026-11-04 17:55' -U '2026-11-04 18:05' -g 'cmd .*room=den .*value=off'
```

`origin` is `web`, `tui`, `guest` (the plain-HTTP Guest listener), `api` (a
client that does not name itself), `all_off`, `schedule` or `repair` (the
worker). `client` is the browser tab's `web/<tab>` or the terminal's
`tui/<host>`: a label the client sends, not proof. `user` and `principal`
(`session`, `token`, `guest`, `local` or `worker`) name who sent it; `token`
is the first 8 characters of a personal token's ID, and a terminal names its
token after its host. `peer` is the first 8 characters of a keyed digest of
the client's address, never the address: match it against other lines, such
as that account's `audit action=login` lines. A room's All off command
carries `batch=` the `id` of its `all_off` line. `req` is the request's
`X-Request-Id`.

Why a switch failed or was slow:

```sh
journalctl -u thermite-access.service -g 'cmd .*outcome=runtime_unavailable'
journalctl -u thermite-access.service -g 'cmd .* ms=[0-9]{4,}'
journalctl -u thermite-access.service -u thermite-openhab.service -g 1a2b3c4d
grep -h 1a2b3c4d /srv/thermite/runtime/logs/events.log
```

`outcome` is `accepted` (openHAB took the command), a refusal (`room_locked`,
`device_locked`, `runtime_busy`, `policy_unavailable`) or
`runtime_unavailable(http=503)` or `runtime_unavailable(exc=ConnectTimeout)`:
openHAB's HTTP status or the exception class, never a message. `ms` includes
the wait for the room's lock. In openHAB's journal, `routing command` is the
routing rule receiving it, each `routing ticket` line one device step (its
`resource`, `value`, `outcome`, `reason`, whether a read-back confirmed it,
`ms`, `wait` in the queue, `polls`, and an HTTP status or exception class),
and a `routing deadline` warning names the steps still waiting, cleaning up or
failed when the route's time ran out:

```sh
journalctl -u thermite-openhab.service -g 'routing ticket .*(reason=[a-z]|readback=unconfirmed)'
journalctl -u thermite-openhab.service -g 'routing (deadline|ticket .* ms=[0-9]{4,})'
```

Which lock blocked a command, and who set it:

```sh
journalctl -u thermite-access.service -g 'cmd .*outcome=(room_locked|device_locked)'
journalctl -u thermite-access.service -g 'audit action=(room\.lock|room\.unlock|policy\.update) .*target=den'
journalctl -u thermite-access-worker.service -g '(boundary|phase) .*room=den'
```

`lock=manual` is an administrator's manual lock and `lock=schedule` a
schedule, held by `lock_room` (a shared device can be locked by another
room). The `audit` lines name who locked or unlocked the room or saved its
schedule, when and from which `peer`; the worker's `boundary` and `phase`
lines say when a schedule window started or ended and what it sent.

Security events: every audit row is also an `audit` line.

```sh
journalctl -u thermite-access.service -g 'audit action='
journalctl -u thermite-access.service -g 'audit action=login outcome=denied'
journalctl -u thermite-access.service -u thermite-access-worker.service -p warning
journalctl -u thermite-openhab.service -g '^\[(WARN|ERROR)'
RELEASE=$(systemctl show -P WorkingDirectory thermite-access.service)
sudo -u thermite -g thermite "$RELEASE/env/bin/python" "$RELEASE/program/bin/thermite" \
  access audit --config "$RELEASE/config.json" --since 7d --action login
```

A denied sign-in names the account's ID in `target` when the name exists,
and never what was typed. `thermite access audit` reads the audit table,
which outlives the journal's retention: the latest 5000 events and 1000
denied sign-ins. Requests that change something or fail have a `req` line
(method, path, status, `ms`, `peer`); successful reads and polls have none.
Event streams log `stream open` and `stream close` with their duration and
the peer's and gateway's stream counts. openHAB writes everything to the
journal at one priority, so its warnings are found by their `[WARN ]` text.
Deploys write a start and an end line, `journalctl -t thermite-deploy`, and
`thermite history --details` lists what each changed and reloaded and the
gateway release it activated.

## Update or remove

Use the same owner command from any directory; `--native-timeout SECONDS`
applies to upgrades as it does to installs:

```sh
thermite upgrade --config "$HOME/thermite-site" --dry-run
thermite upgrade --config "$HOME/thermite-site"
```

Upgrade fetches the official application's `main` branch after approval,
prepares or reuses a verified private environment, updates and commits the
site's exact application requirement, and then installs the new version as
one install transaction: it either commits the new version or puts the
installed one back, in the same run. There is no separate pip, site-pin,
native-runtime or access-release update sequence. For a reviewed local
checkout or an explicit Git revision, use `--source DIR` or `--revision REV`;
see [application releases](packaging.md#update-and-roll-back).

Every command that changes what is installed runs as one install
transaction: `thermite install` (and `--clean`), `thermite upgrade`,
`thermite uninstall` (and `--purge`), `thermite rollback` and `thermite
revert`. They differ only in what they set aside and what they switch in;
the upgrade below shows the whole sequence.

| Command | Sets aside | Switches in | Commit |
|---|---|---|---|
| `install`, `upgrade` | units, filter, AppArmor policy; copies of the state trees | the site's HEAD | keeps the version set aside |
| `install --clean` | as install, but the state trees under `runtime/` and monitoring are moved, and the deploy records and local credential copies cleared | HEAD on fresh state | deletes the moved state |
| `rollback [COMMIT]` | as install | an earlier site commit | keeps the version set aside |
| `revert` | units, filter, AppArmor policy; the state trees moved whole | the version the last install or upgrade kept, with its state | keeps the newer version set aside |
| `uninstall` | units, filter, AppArmor policy | nothing; the state stays in place | records "not installed" |
| `uninstall --purge` | as uninstall, and `runtime/` and `access/` whole | nothing | deletes the state, images and every transaction |

### How an upgrade runs

While Thermite keeps running, the upgrade prepares what belongs to the host
rather than to Thermite: the configuration checks and boot test, required
packages, AppArmor's own service, the `thermite` account and, on a new host,
the installation root, which the install engine works inside. It pulls every
pinned container image before anything stops. Then it shows the room the
install engine needs to set the installed version aside:

```text
Room to set aside the current version, on /srv/thermite (free 41.2 GiB):
  runtime/userdata                       1.4 GiB
  runtime/conf                           3.0 MiB
  access/state                          12.0 MiB
  14 root files and links                 <1 MiB
  the new release                      310.0 MiB
  margin                               256.0 MiB
  needed                                 2.0 GiB  (a full copy; cloning may use less)
```

Without that room the upgrade stops there, with exit status 1; nothing of
Thermite was stopped or changed. Otherwise the transaction runs. The install
engine runs as root from the copy pinned at begin,
`sudo /usr/bin/python3 -I -S /srv/thermite/.transaction/<id>/engine.py`:

1. **Begin**: the engine records the transaction and what it will set aside.
2. **Guard**: `thermite.target` is disabled, so a reboot in the middle never
   starts a half-installed version.
3. **Stop**: `systemctl stop thermite.target thermite.slice`, then a check
   that nothing in the slice still runs.
4. **Collect**: the installed units, firewall rules, AppArmor policy and state
   trees are copied or moved into `/srv/thermite/.transaction/<id>/`.
5. **Switch**: the new version's units, native preparation, native-port
   firewall, deployment, services, runtime access, HTTPS gateway (a new
   release, `access/releases/<id>`) and monitoring; then `thermite.target`
   starts.
6. **Verify**: monitoring answers and `thermite health --production
   --installation` passes.
7. **Commit**: the engine records the new version in
   `/etc/thermite/installed.json` and enables `thermite.target`. The version
   before it stays set aside in `/srv/thermite/.transaction/<id>/` until the
   next transaction commits; `thermite revert` puts it back.

Only after the commit does `~/.local/bin/thermite` switch to the new
application. Thermite is down from the stop until the target starts again:
for the copy of its state, which grows with `runtime/userdata`, and for the
runtime's native startup. A rerun with nothing to change only checks health
and stops nothing.

Every unit Thermite installs is part of `thermite.target`, runs in
`thermite.slice` and is wanted by the target alone. The native-port boundary
is the exception: it only loads the firewall rules, so stopping Thermite
leaves them loaded, and reapplying them does not restart openHAB, whose unit
wants the boundary and loads the same rules itself before every start, so it
never starts without them. The ownership repair covers only openHAB's trees
unless the service-account receipt, account or a state directory is missing.

The site's committed `versions.json` still selects native component versions.
Updating Thermite does not silently replace those pins. Keep the private
repository clean and keep `var/application-releases/` and
`var/gateway/authority/`. Application environments install their
dependencies from the hashed `lib/requirements-app.txt` and are rebuilt
automatically after a Python minor-version upgrade.

Each commit keeps two versions and removes older ones: the current and the
previous gateway release in `access/releases/`, the container images the two
pin, and the application environments of the current and the previous
release under `~/.local/share/thermite/application/`, with their records and
source checkouts. An environment that no record names is left alone. The
first commit on a host installed before the install engine also removes the
gateway releases staged before it, except the one that version ran;
`runtime/backups/` is never pruned.

Upgrades no longer write tar backups of openHAB's and Grafana's state to
`runtime/backups/`, and nothing reads the ones earlier releases wrote: the
version set aside by the last commit is the way back. Keep or delete those
archives as you see fit. openHAB's own updater still copies userdata to
`runtime/backups/thermite-before-*.tar` before it migrates it to a new openHAB
version.

If a missing kernel module stopped an older installer after services were
stopped, reboot into the installed kernel and then rerun the upgrade. The
reboot alone does not complete deployment: the upgrade must install and verify
the native-port boundary before starting the runtime.

Runtime and monitoring readiness poll within their allowances. Once the
required readiness or settling condition passes, that result completes the step;
an extra request outside the wait loop cannot fail it during a brief startup
interruption. Each later check and deployment run still queries the running
services. Dropped, refused or transiently failing native reads retry until the
native deadline; writes are never repeated, and credentials are not replaced.

### When an upgrade fails

Preparation or validation failures restore the bounded site edits when safe;
concurrent user edits are preserved. A failure before the transaction begins
also undoes the site's pin commit, so the site pins the version that runs;
on the first upgrade too, where the owner command from before the install
engine made that commit without naming it.

Any failure from begin on, in a step, the health check or the commit, rolls
the transaction back in the same run. The engine puts the set-aside version
back and starts it, with `thermite.target` enabled as before. Then the upgrade
puts back the files it keeps in the site's `var/` (the runtime token, the
Grafana password, the runtime credentials and the gateway profile), undoes
the site's pin commit when the site is still at it and clean, records the
rollback in `thermite history`, and checks the reinstated version's health
with the owner command it kept. Nothing starts a half-installed version again:
the rollback is the only undo.

| Exit status | Outcome |
|---|---|
| 0 | Committed; nothing to change and healthy; or nothing to uninstall. |
| 1 | Refused before the transaction began: nothing of Thermite was stopped or changed. Also declined, or a rerun that only finished or closed an earlier transaction. |
| 2 | Not started: Thermite's files without a manifest (see [a missing manifest](#a-missing-manifest)), nothing to roll back or revert to, or invalid arguments. |
| 3 | Rolled back: the version that ran before runs again, healthy. |
| 4 | Rolled back: the version that ran before runs again, but its health check fails; it keeps running. |
| 5 | The rollback is stuck: see [a stuck rollback](#a-stuck-rollback). |
| 130 | Interrupted: see [an interrupted upgrade](#an-interrupted-upgrade). |

After exit status 4, fix what health reports, then check again with
`thermite health --production --installation`. When the site could not be
reset (it moved on, or has uncommitted changes), the upgrade says so and
prints the `git revert` that pins the version that runs.

To go back after a commit, see [go back](#go-back). Do not initialize new
accounts or replace credentials merely to retry.

`--dry-run` prints a provisional host plan only when the site pins the release
that is running it; otherwise the host plan is resolved after approval.

`thermite install` runs as the same install transaction, with the same exit
statuses; on a host without Thermite, a rollback leaves nothing installed.
`thermite install --clean` is one too: it asks you to type the state path
before it begins, moves the state trees under `runtime/` aside whole and
clears the deploy's records and local credential copies inside the
transaction, and deletes what it moved aside only at the commit. Logs, the
deploy history, old backups and the gateway's identity stay.

### Go back

Two commands go back, each as its own install transaction with the exit
statuses above:

```sh
thermite rollback --config "$HOME/thermite-site" --dry-run
thermite rollback --config "$HOME/thermite-site" [COMMIT]
thermite revert --config "$HOME/thermite-site" --dry-run
thermite revert --config "$HOME/thermite-site"
```

`thermite rollback` installs an earlier site commit: by default the one
deployed before the current one (`thermite history` lists them). It installs
that commit as `thermite install` installs HEAD, with its boot test, units,
images, native-port filter and gateway; the state stays as it is, and the
deploy history records a rollback. The site's branch does not move, so the
next install or upgrade installs HEAD again; to keep the earlier
configuration, revert the site to it in a new commit. The commit must pin
the application release that runs the command; one pinned by another
release is refused with exit status 2.

`thermite revert` puts back the version the last install or upgrade set
aside, with its state as it was then. That means its files, units, filter
and confinement, its `runtime/` and `access/` trees, and the runtime token,
Grafana password, runtime credentials and gateway profile in the site's
`var/`. Everything Thermite wrote since that commit is lost: accounts,
pairings, credentials, history and monitoring data. Revert says so and asks
you to type the installation root before anything stops; until it commits, a
rollback keeps all of it. Inside the transaction, the site's branch goes
back to the commit before the upgrade's pin commit, so that the previous
version checks its health with its own application environment; that check
must pass before the revert commits, and a rollback puts the pin commit
back. Once the revert commits, `~/.local/bin/thermite` selects the previous
application again, even when its rollback or the next run finishes the
commit. A revert writes no deploy-history entry.

A revert is refused with exit status 2 after an uninstall, a clean install
or another revert, when the site is not at the commit that transaction
installed, when the previous application environment is gone, and after the
first transaction on a host installed before the install engine: that
transaction recorded no gateway release for the version before it. Use
`thermite rollback` there.

Pinning an application release older than the site's committed one requires
`--allow-downgrade`, after deliberately restoring state the newer release
may have migrated.

### An interrupted upgrade

Ctrl-C before the transaction begins changes nothing, and the site's pin
commit is undone. After it began, the transaction stays open: Thermite may
be stopped, with `thermite.target` kept from booting. `thermite status`
reports it as a problem and names the command to run. Run the command that
began it again, with `--config DIR`; any other command that runs a
transaction (`install`, `upgrade`, `uninstall`, `rollback` or `revert`) does
the same. It first finishes a transaction the engine already committed,
closes one that never began, or rolls back any other, then stops without
doing anything more and says so, with that outcome's exit status. Run it
once more to go on.

### A stuck rollback

Exit status 5 means the engine could not put something back, for example
because the disk is full. Thermite stays stopped and `thermite.target` stays
disabled; the transaction stays open and no other one can begin. Fix the
cause the command printed, then run it again with `--config DIR`: the
rollback continues from where it stopped.

When the engine itself cannot run, it prints the commands it would run, for
review and for running by hand as root:

```sh
sudo /usr/bin/python3 -I -S /srv/thermite/.transaction/<id>/engine.py \
  rollback --root /srv/thermite --id <id> --print
sudo /usr/bin/python3 -I -S /srv/thermite/.transaction/<id>/engine.py \
  status --root /srv/thermite
```

The command's message names the transaction and the engine copy; `status`
lists every transaction and its phase. After rolling back by hand, keep
`/srv/thermite/.transaction/<id>/` and run the command again: the engine's
journal still records the transaction as stuck, and only the engine closes
it. The rerun takes every entry already back as restored, checks that what
is live is the version it collected, restarts that version and closes the
transaction; only then can another begin.

### A missing manifest

An installation from this release on records what it installed in
`/etc/thermite/installed.json`. The first upgrade of an installation from
before the install engine describes it instead from `/etc/thermite/units.json`
and the fixed paths, and refuses before anything stops if any of them is not
exactly what the installer wrote; that first transaction then sets it aside
like any later one. Its commit removes the gateway releases staged before
it, except the one the old version ran; when that one is unknown, it keeps
them all.

When Thermite's units and files are here but neither manifest is, or
`installed.json` is damaged, nothing can prove which of them this
installation owns, so `thermite install`, `upgrade`, `uninstall`,
`rollback` and `revert` refuse with exit status 2 and change nothing, and
print these steps. To recover, as root: stop Thermite
(`systemctl stop thermite.target thermite.slice`); move its units
(`/etc/systemd/system/thermite*`, `/etc/containers/systemd/thermite*` and
their links in `/etc/systemd/system/multi-user.target.wants`), the native-port
rule file `/etc/thermite-native.nft` and the node exporter's AppArmor files
aside, out of those directories; run `systemctl daemon-reload`; then run
`thermite install --config DIR`. The state under `/srv/thermite` stays where
it is.

### Remove

```sh
thermite uninstall --config "$HOME/thermite-site" --dry-run
thermite uninstall --config "$HOME/thermite-site"
```

Uninstall is one install transaction too, with the same exit statuses.
Thermite stops, and the install engine sets aside everything it installed
outside the installation root: the units and quadlets and their links, the
boot-time `thermite-native-boundary.service`, the native-port rule file with
its loaded `inet thermite_native` table, and the node exporter's AppArmor
policy. The state stays where it is, `runtime/` and `access/` (accounts,
pairings, the gateway's identity and TLS key, history, monitoring data),
recorded in `/etc/thermite/installed.json` as not installed, so a later
`thermite install` finds it again. The images, the `thermite` account and
the packages stay too. What was set aside stays in
`/srv/thermite/.transaction/<id>/` until the next transaction commits.

`thermite uninstall --purge` asks you to type the installation root, then
also moves `runtime/` and `access/` aside whole; its commit removes them, the
installed manifest (and `/etc/thermite` once empty), Thermite's images and
every transaction, its own included, and the site's `var/transaction/`. The
site, with `var/gateway/authority/`, stays. Back up private configuration,
runtime data, credentials, `var/gateway/authority/` and release artifacts
before purging an installation.

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
