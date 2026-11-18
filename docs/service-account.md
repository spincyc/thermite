# The Thermite service account

Thermite's application services run as `thermite:thermite`, with numeric
UID and GID 9001. This includes the native runtime, Apple TV helper,
monitoring services, HTTPS gateway, policy worker and optional callback
relay. The account has a `nologin` shell and is reserved for services.
One monitoring service has an account of its own, `thermite-node`: see
[the node_exporter account](#the-node-exporter-account).

Continue to use your own account for the CLI, terminal remote, builds and
private Git repository. The installer adds the installing owner to the
`thermite` group. Add another trusted operator explicitly, then start a new
login session:

```sh
sudo usermod -aG thermite <operator-login>
```

Group members can change the deployed configuration and program. Grant that
membership only to people who administer the installation.

## Files and permissions

The private site and its Git history stay owned by their human operator.
Everything else lives in the installation root, `/srv/thermite` by default
(see [host layout](installation.md#host-layout)). Root owns that directory,
mode 0755; on btrfs it is a subvolume. Its `runtime/` is the deployed service
tree, owned by `thermite:thermite` and the account's home; its `access/` is the
HTTPS gateway root. The installer creates the layout and the deploy command
preserves the runtime's permissions:

| Deployed files | Directories | Files |
|---|---|---|
| Configuration and program shared with trusted operators | `2770` | `0660`; executable programs `0770` |
| Helper environments and monitoring stores | `0750` | `0640`; executable programs `0750` |
| Backup archives | `0750` | `0600` |
| Native userdata, credentials, pairings and access-service identity state | `0700` | `0600` |
| openHAB's logs, `runtime/logs` | `2750` | `0640`; anything else `0600` |
| Metrics for node_exporter, `runtime/textfile` | `2775` | `0644` |

The leading `2` on shared directories makes new files inherit group
`thermite`. Service credentials are private to the service account; an
operator's local API token and terminal login token stay private to that
operator. Fresh installation also keeps a generated internal runtime account
and token in `var/<host>/runtime-credentials.json`, mode 0600. This record lets
setup resume without asking users to manage the runtime's own account pages;
it is separate from household sign-in. Packages, application environments and installed systemd unit files
retain their normal administrator ownership.

When a fresh configuration clone lacks both operator credentials, the installer
can restore `var/<host>/api-token` from this installation's private monitoring
token. The approved administrative helper validates it using a read-only
loopback request before writing an operator-owned mode-0600 copy. It never
replaces existing operator credentials, changes accounts, or rewrites the
service token. Keep the original managed credential record in private backups;
the helper cannot reconstruct it from an API token.

openHAB's Karaf console (SSH on 127.0.0.1:13604, account `openhab`) does not
keep the password the distribution publishes. Every install and upgrade,
while openHAB is stopped, gives the account this installation's
own: 48 random hexadecimal digits in `/etc/thermite/karaf-password`, root's
alone (0600), made the first time and kept after that. Userdata's
`etc/users.properties` holds only its SHA-256 digest, as Karaf stores a
password, and no service is given the file: read it with `sudo cat` when you
want the console. Leave the file as it is. The installed manifest records
it, like every root file of Thermite's, and a changed or missing one refuses
the next transaction. `thermite revert` to a release from before it takes
the file away as it puts that release's userdata back, and the next upgrade
makes a new one. A developer sandbox and the boot check keep the
distribution's password on purpose: their consoles are loopback listeners on
their own ports, over userdata in the developer's own `var/`.

Of the monitoring credentials in `runtime/monitoring/secrets`, Prometheus and
the Item exporter, which send the API token, mount the directory. Grafana's
container is given the file of its own administrator password and nothing
else there.

More restrictive existing configuration modes are retained. Native userdata
is private as a whole because it also contains identities and pairing keys.
openHAB therefore writes its logs outside it, to `runtime/logs`, where the
group reads but never writes them. Permission maintenance makes openHAB's and
its updater's logs there 0640 and anything else private; see
[host layout](installation.md#host-layout).
The recognized, versioned Graal attach executable may retain its producer's
`0775` mode only when every parent inside userdata is service-owned `0700`.
Those directories deny access to other identities. An existing `0700` mode
is retained; other private userdata files and credentials remain `0600`.
The Apple TV helper checks its environment after package installation and
on reuse, since wheel installers can set modes explicitly despite the umask.
Upgrades no longer write state backups to `runtime/backups/`; archives and
mode-0640 completion receipts earlier releases wrote there keep their modes,
and permission maintenance preserves them without widening private files.
An install, upgrade or rollback writes the shared configuration as the
owner, through the `thermite` group, and then hands the paths it wrote to
the service account with a root command of the same approved run. It asks
no separate question for that.

### What each service may use

Every long-running service has a record of the user it runs as, its memory
ceiling, its network and the secret stores it can read: `oh_units.BOUNDS`
for openHAB, the Apple TV helper, Music Assistant and the three access
services, and the row of each monitoring service in `oh_metrics.services()`.
No field has a default, the units are written from the records, and
`tests/test_unit_invariants.py` fails when a rendered unit lacks a field, says
something else than its record, or mounts a secret its record does not
declare. The access services are host services that run as the state's
user and need nothing of the runtime state but the directory their metrics
go to: their units put an empty read-only directory in its place
(`TemporaryFileSystem=`) and bind that one directory inside. The records
also show what is still wide: every unit is on the host's network.

### The node_exporter account

node_exporter measures the host, so it has all of `/` mounted read-only and
shares the host's process namespace. As the service account it could read
every secret store of the runtime state and signal every Thermite process.
It runs as `thermite-node:thermite-node` instead: a system account with a
`nologin` shell and no home, which owns no file and whose group has no
members. `runtime/` is mode 0750 for `thermite:thermite`, so below `/host`
node_exporter reads nothing of the runtime state, and of other accounts'
processes it sees what any unprivileged user of the host sees. The one
directory it needs, `runtime/textfile`, where the access services and each
deploy write their metrics, is mounted for it by name at `/textfile`. That
directory is `2775` and its files `0644`; nobody else gains by it, since
only a member of `thermite` gets past `runtime/`. Its record says so (user
of its own, no secret store), and `tests/test_unit_invariants.py` fails
when the record or the unit goes back to the service account.

Its UID and GID are one below the service account's: 9000. `useradd` gives
the next ordinary user the number above the highest in use, which the
service account's usually is, and hands out nothing below it. The installer
creates the group and the user before it changes anything else, so the
number stays reserved, and stops when the number or the name belongs to
another account. Choose another free number in `instance/host.yaml`:

```yaml
metrics:
  node_exporter_uid: 61000
```

Like the service account, `thermite-node` stays after an uninstall, a
rollback or a revert to a release that ran node_exporter as the service
account. It owns nothing, so `userdel thermite-node` removes all of it.

Each unit states `MemoryHigh=` and `MemoryMax=` for its cgroup, page cache
included. From the first the kernel reclaims the unit's cache and slows its
allocations; at the second it kills a process of the unit and systemd
restarts the service. The ceilings stop one runaway service from taking the
host with it. They are not a budget: together they exceed a 4 GiB host, on
which Thermite uses about 2.3 GiB.

| Service | `MemoryHigh` | `MemoryMax` | Basis |
|---|---|---|---|
| openHAB | 2560M | 3G | The JVM's heap ends at 1536M (`-Xmx`, and the JVM exits on `OutOfMemoryError`); metaspace, the code cache, thread stacks and direct buffers come on top. Measured peak 1.6 GiB. A kill stops the house, so slowing starts a gigabyte above the heap and the kill half a gigabyte above that. |
| Music Assistant | 1536M | 2560M | Measured peak 0.6 GiB; it grows with the library and with each stream it transcodes. |
| Grafana | 1G | 1536M | Measured peak 0.7 GiB, while it starts. |
| Prometheus | 1536M | 2560M | Measured peak 0.5 GiB, much of it page cache of its samples, which `MemoryHigh` reclaims. One query may hold up to 50 million samples, about 0.8 GiB, and the history it reads grows for as long as samples are kept. |
| Gateway and policy worker | 256M | 512M | Measured peaks 84 and 100 MiB. |
| Callback relay | 128M | 256M | Measured peak 52 MiB. |
| Apple TV helper | 256M | 512M | Measured peak 119 MiB. |
| node_exporter, blackbox and Item exporter | 128M | 256M | Measured peaks 47, 70 and 58 MiB. |

The peaks are systemd's `MemoryPeak` of each unit on one installed house,
eight hours after a start. Every `MemoryHigh` is at least one and a half
times that peak. `systemctl show -p MemoryPeak 'thermite-*.service'` shows
yours; a service that stays near its `MemoryHigh` is slowed, not killed, and
its record is where to raise it.

The host's systemd manager and Podman backend remain root-managed.
Application container entrypoints and processes start as UID/GID 9001,
including the native runtime, and node_exporter as 9000; they do not start
as root to repair mounted files. Native access units use `User=thermite` and `Group=thermite`.
Privileged host service management remains the administrator's responsibility.

## Upgrade an existing installation

Upgrades keep the service account, its numeric IDs and the data in place.
From your private site, as its owner:

```sh
thermite upgrade --config /path/to/private-site --dry-run
thermite upgrade --config /path/to/private-site
```

The same command updates the application environment, native services and
HTTPS gateway, as one install transaction that commits the new version or puts
the installed one back. Review the plan before applying it. One approval
covers the complete workflow.

Each commit records what is installed in the root-owned
`/etc/thermite/installed.json`: every unit, link, rule file and policy
outside the installation root, the state trees, the installed release, its
images and application, and the transaction that set the previous version
aside. Install, upgrade, uninstall, rollback and revert set aside exactly
what it names, so only Thermite's own files change. When Thermite's units or
files are here but that manifest is missing or damaged, nothing can prove
which of them are Thermite's: every one of those commands refuses before
anything stops and prints the recovery steps, which move Thermite's units,
rule file and AppArmor files aside by hand and leave the state in place; see
[a missing manifest](installation.md#a-missing-manifest). An installation
from before the install engine has no `installed.json` and takes the same
recovery.

Before changing or removing a unit, the installer proves it is Thermite's.
A committed install records each unit file it installed, with its digest, in
the root-owned installed manifest, `/etc/thermite/installed.json`. A
root-owned unit file is recognized when its exact bytes match that record, or
are the unit the current committed inputs render. Time zone edits and
template changes therefore do not block an upgrade. The effective unit must
also resolve to that file without unowned drop-ins. An unknown modification
or other ownership stops the run for review; independently installed
`openhab`, `prometheus` and `grafana` services are left alone. Releases
before this one kept a second record, `/etc/thermite/units.json`; the upgrade
from one of them removes it, and a revert puts it back.

If the configured service UID or GID already belongs to another account, such
as an independent `openhab` account, the installer stops: it does not rename
or adopt another service account. An administrator must resolve that
collision first. Preserve numeric ownership for existing Thermite data; do not
change IDs merely to bypass this check.

Upgrades do not relocate the installation root or reinitialize a database.
Configuration, users, pairings, histories and data stay in place. Do not
replace them with the fictional example.

After the workflow succeeds, log out and back in, then check:

```sh
getent passwd thermite
getent group thermite
id -nG
thermite status
thermite health --production
```

The account and group entries should show 9001 (`getent passwd thermite-node`
shows node_exporter's 9000), your new login should include
group `thermite`, and status should show the expected services. The
installer replaces the gateway units it recognizes with those of the release
it stages, on the identity state, TLS material and callback routes at their
fixed paths under `access/`. Unknown ownership or unsafe overrides stop for
review; an existing identity is never bootstrapped again.

## Service registration

Every daemon and container uses the `thermite-` prefix, including
`thermite-openhab`, `thermite-prometheus`, `thermite-grafana` and, on a host
with `music:`, `thermite-music` ([Music Assistant](music.md)). All daemon
processes belong to `thermite.slice`, and `thermite.target` groups their
systemd lifecycle. Each container refuses to replace an existing namesake;
service cleanup uses its own container ID. After commissioning, manage the
installed stack with:

```sh
systemctl status thermite.target
systemctl list-dependencies thermite.target
sudo systemctl restart thermite.target
sudo systemctl stop thermite.target
```

Installation still starts components in stages and checks their readiness
before enabling the target. An active target does not replace those health
checks. The gateway, worker and verified callback relay are commissioned by
the same workflow and join the same target and slice. Generating files alone
starts no service.

Default TCP listeners use Thermite's [13600–13628 range](ports.md).
Persistent files remain below the installation root's `runtime/`; upstream
container paths such as `/openhab`, `/var/lib/grafana` and Music Assistant's
`/data` are private mounts, not the host's independent application
directories. Discovery protocols keep
their standard multicast ports; running two controllers against the same
physical devices is still subject to those devices' protocol limits.

## Gateway service ownership

`thermite install --config DIR` and `thermite upgrade --config DIR` manage the
HTTPS gateway, policy worker and verified callback relay together with the
native services. Their releases and identity state live in the installation
root's `access/`, beside the native `runtime/` and never inside it. `access/`
and its `releases/` belong to the operator, mode 0755; each run stages a new
release, `releases/<id>`, named after its install transaction; each commit
keeps the current and the previous release and removes older ones. Private release
files and the persistent identity in `state/` belong to `thermite:thermite`,
mode 0700. The served TLS certificate and key and the verified callback map
live in `inputs/`, private to the operator (0700), so a transaction can set
them aside with the identity. Root owns the installed unit copies and the
trust certificate `tls-cert.pem`; an installation from before the install
engine also keeps its old identity `backups/` (0700), which nothing writes or
reads any more.

The gateway's private certificate authority is separate: it lives in the
private site's `var/gateway/authority/`, owned by the operator (directory 0700,
key 0600). Back it up with the site's other private state.

The workflow retains existing identity and TLS state, stops gateway writers,
migrates the schema, starts the new services and verifies their live release
and HTTPS response. In an upgrade, the install transaction has set the
identity state aside first, and a rollback puts it back. It also seals
native ports and preserves verified callback routes. There is no separate
staging or activation command. See [installation and recovery](installation.md#update-or-remove).

On a new site, ordinary controls work with no account. The Security
page creates the first administrator account from an owner-issued setup
code. The service account still has no interactive login.

### Move existing access state

An older HTTPS release may keep its private access state inside an operator's
home. If `thermite` cannot traverse that location, move only that access-state
directory once. This does not move the native `runtime/`. First
stop the gateway, worker, any callback service and any user-service preview
that uses the state. Keep a backup and choose an unused destination; never
merge it with another site's state.

As the owner, with the `thermite` account and the root-owned installation
root already installed:

```sh
ACCESS_ROOT=/srv/thermite/access
OLD_ACCESS_STATE=/absolute/path/to/existing/access-state
sudo install -d -o "$(id -un)" -g "$(id -gn)" -m 0755 \
  "$ACCESS_ROOT" "$ACCESS_ROOT/releases"
sudo mv --no-clobber --no-target-directory -- \
  "$OLD_ACCESS_STATE" "$ACCESS_ROOT/state"
```

Confirm the old path is gone and the new directory contains the existing
state before continuing. Then run `thermite install --config DIR`: it stages
a new release on the state in that place, hands both to the service account,
verifies them as `thermite`, migrates and starts the services. This preserves
the database, site identity, signing keys, policies and locks. Skip
bootstrap. The prior release's configuration names the old state location,
so it cannot serve again as it is.
