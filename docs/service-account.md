# The Thermite service account

Thermite's application services run as `thermite:thermite`, with numeric
UID and GID 9001. This includes the native runtime, Apple TV helper,
monitoring services, HTTPS gateway, policy worker and optional callback
relay. The account has a `nologin` shell and is reserved for services.

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
Before an operator's deploy or rollback writes, it asks for approval of the
exact ownership-handoff command. When it needs that privilege, a
noninteractive run refuses before writing. A successful deploy hands its
named paths to the service account.

The host's systemd manager and Podman backend remain root-managed.
Application container entrypoints and processes start as UID/GID 9001,
including the native runtime; they do not start as root to repair mounted
files. Native access units use `User=thermite` and `Group=thermite`.
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

Before changing or removing a unit, the installer proves it is Thermite's.
It records each unit file it installs, with its digest and the application
release that rendered it, in the root-owned `/etc/thermite/units.json`. A
root-owned unit file is recognized when its exact bytes match that record, the
unit the current committed inputs render, or a unit an earlier verified build
of this site rendered; units installed before the record existed are taken
over once that way. Time zone edits and template changes therefore do not
block an upgrade. The effective unit must also resolve to that file without
unowned drop-ins. An unknown modification or other ownership stops the run for
review; independently installed `openhab`, `prometheus` and `grafana`
services are left alone.

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

The account and group entries should show 9001, your new login should include
group `thermite`, and status should show the expected services. The unified
installer adopts recognized gateway units, preserving their existing private
state, TLS and callback settings. Unknown ownership or unsafe overrides stop
for review; an existing identity is never bootstrapped again.

## Service registration

Every daemon and container uses the `thermite-` prefix, including
`thermite-openhab`, `thermite-prometheus` and `thermite-grafana`. All daemon
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

Default TCP listeners use Thermite's [13600–13626 range](ports.md).
Persistent files remain below the installation root's `runtime/`; upstream
container paths such as `/openhab` and `/var/lib/grafana` are private mounts,
not the host's independent application directories. Discovery protocols keep
their standard multicast ports; running two controllers against the same
physical devices is still subject to those devices' protocol limits.

## Gateway service ownership

`thermite install --config DIR` and `thermite upgrade --config DIR` manage the
HTTPS gateway, policy worker and verified callback relay together with the
native services. Their releases and identity state live in the installation
root's `access/`, beside the native `runtime/` and never inside it. `access/`
and its `releases/` belong to the operator, mode 0755; each run stages a new
release, `releases/<id>`, named after its install transaction. Private release
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
native ports and preserves verified callback routes. No separate staging or
activation script is needed. See [installation and recovery](installation.md#update-or-remove).

On a new site, ordinary controls work with authentication off. The Security
page enables it through an owner-issued setup code and first administrator
account. The service account still has no interactive login.

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
state before continuing. Stage a new release with
`--state /srv/thermite/access/state`, then run its explicit ownership commands
and service-user verification. This preserves the database, site identity,
signing keys, policies and locks. Skip bootstrap. Keep the prior release for
rollback; because its configuration names the old state location, it must be
restaged with the current location before reuse. Leave all access services
stopped until the new units and access settings have been checked.
