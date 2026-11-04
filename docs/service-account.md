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

The private source checkout or site, its launcher and its Git history stay
owned by their human operator. The deployed service tree is owned by
`thermite:thermite`. The installer creates the layout and the deploy command
preserves its permissions:

| Deployed files | Directories | Files |
|---|---|---|
| Configuration and program shared with trusted operators | `2770` | `0660`; executable programs `0770` |
| Helper environments and monitoring stores | `0750` | `0640`; executable programs `0750` |
| Backup archives | `0750` | `0600` |
| Native userdata, credentials, pairings and access-service identity state | `0700` | `0600` |

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
The recognized, versioned Graal attach executable may retain its producer's
`0775` mode only when every parent inside userdata is service-owned `0700`.
Those directories deny access to other identities. An existing `0700` mode
is retained; other private userdata files and credentials remain `0600`.
The Apple TV helper checks its environment after package installation and
on reuse, since wheel installers can set modes explicitly despite the umask.
The backup directory lets trusted operators list and inspect archive metadata;
the archive contents remain private to the service account. New backups have
mode-0640 completion receipts readable by the trusted operator group. Permission
maintenance preserves that mode without widening private files.
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

Existing Thermite installations can migrate their old `openhab*` unit and
container names while preserving the service account and data. From your
private site, as its owner:

```sh
thermite upgrade --config /path/to/private-site --dry-run
thermite upgrade --config /path/to/private-site
```

The same command updates the application environment, native services and
HTTPS gateway. Review the plan and keep a current backup before applying it.
One approval covers the complete workflow.

The migration verifies generated unit contents, their state bindings and
the effective systemd registration before stopping or removing them. New
inactive unit files are installed before old registrations are retired, so
an interrupted migration retains proof of ownership for the next run. A
shadowing service, unknown modification or ambiguous ownership stops the
migration for review. Independently installed `openhab`, `prometheus` and
`grafana` services are left alone. A nonempty state directory also needs proof
that it belongs to Thermite; an unrelated application directory is not adopted.
Empty installation scaffolding can be resumed.

An independent `openhab` account with different numeric IDs can coexist.
Older Thermite installations that still use an `openhab` account at the
configured service UID/GID need an administrator to resolve that collision
first. The installer does not infer exclusive account ownership from its
name or numeric ID and does not rename it automatically. Preserve numeric
ownership for existing Thermite data; do not change IDs merely to bypass this
check.

Unit migration does not relocate the state root or reinitialize a database.
Keep your existing `state_root`, even if it is named
`/srv/old-runtime`. Configuration, users, pairings, histories and data
stay in place. Do not replace them with the fictional example or change the
numeric IDs to obtain the new account name.

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

Default TCP listeners use Thermite's [13600–13625 range](ports.md).
Persistent files remain below the selected Thermite state root; upstream
container paths such as `/openhab` and `/var/lib/grafana` are private mounts,
not the host's independent application directories. Discovery protocols keep
their standard multicast ports; running two controllers against the same
physical devices is still subject to those devices' protocol limits.

## Gateway service ownership

`thermite install --config DIR` and `thermite upgrade --config DIR` manage the
HTTPS gateway, policy worker and verified callback relay together with the
native services. Their releases and identity state live outside the native
state root, by default at the same path with `-access` appended. Release
parents belong to the operator; private release files and persistent identity
belong to `thermite:thermite`. Root owns the installed unit copies and backups.

The workflow retains existing identity and TLS state, stops gateway writers,
backs up the complete private state, migrates the schema, starts the new
services and verifies their live release and HTTPS response. It also seals
native ports and preserves verified callback routes. No separate staging or
activation script is needed. See [installation and recovery](installation.md#update-or-remove).

On a new site, ordinary controls work with authentication off. The Security
page enables it through an owner-issued setup code and first administrator
account. The service account still has no interactive login.

### Move existing access state

An older HTTPS release may keep its private access state inside an operator's
home. If `thermite` cannot traverse that location, move only that access-state
directory once. This does not move the native runtime's `state_root`. First
stop the gateway, worker, any callback service and any user-service preview
that uses the state. Keep a backup and choose an unused destination; never
merge it with another site's state.

As the owner, with the `thermite` account already installed:

```sh
ACCESS_ROOT=/srv/thermite-access
OLD_ACCESS_STATE=/absolute/path/to/existing/access-state
sudo install -d -o "$(id -un)" -g thermite -m 2750 \
  "$ACCESS_ROOT" "$ACCESS_ROOT/releases"
sudo mv --no-clobber --no-target-directory -- \
  "$OLD_ACCESS_STATE" "$ACCESS_ROOT/state"
```

Confirm the old path is gone and the new directory contains the existing
state before continuing. Stage a new release with
`--state /srv/thermite-access/state`, then run its explicit ownership commands
and service-user verification. This preserves the database, site identity,
signing keys, policies and locks. Skip bootstrap. Keep the prior release for
rollback; because its configuration names the old state location, it must be
restaged with the current location before reuse. Leave all access services
stopped until the new units and access settings have been checked.
