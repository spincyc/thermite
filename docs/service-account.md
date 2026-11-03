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
operator. Packages, application environments and installed systemd unit files
retain their normal administrator ownership.

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

Older installations named the UID/GID 9001 account `openhab`. Use the current
installer or upgrade workflow to migrate it. From your private site or
complete source checkout, as its owner:

```sh
thermite upgrade --dry-run
thermite upgrade
```

For a source checkout without the installed command, use
`python3 bin/thermite upgrade --dry-run` and
`python3 bin/thermite upgrade`. Review the plan and keep a current backup
before applying it. The owner approves the privileged commands shown by
the workflow.

The migration stops the application services before renaming the existing
user and group. It keeps UID and GID 9001, so existing files still belong to
the same numeric account. It then repairs the configured service tree's
ownership and permissions and installs the current units. An unrelated
account using either name or numeric ID is refused for manual resolution.

The account rename does not rename service units, relocate the state root or
reinitialize a database. Keep your existing `state_root`, even if it is named
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
group `thermite`, and status should show the expected services. The HTTPS
gateway has a separate release handoff described in
[server installation](installation.md#set-up-https-and-accounts). The main
installer cannot rewrite separately staged access releases without their
owner's configuration. Restage older access units for `thermite` before
starting them; keep their existing persistent state and do not bootstrap
it again.

## Stage native access services

Use a persistent directory outside a private home, such as
`/srv/thermite-access`, outside the native runtime's `state_root`. A service
cannot traverse an operator's mode-0700
home directory. Stage a release as the operator, then follow its receipt to
hand the release and state to `thermite:thermite` before bootstrap or service
startup. Do not make your home directory public or run the whole CLI as root.

The first administrator is created by an explicit command run as `thermite`;
the service account still has no interactive login. Later releases reuse the
same private state. See the complete commands in
[server installation](installation.md#set-up-https-and-accounts).
Access staging parents are operator-owned `2750`, so the service can traverse
them but cannot replace an in-progress release. Tighten older `2770` access
parents before staging. The protected plan's separate `activate.sh` handles
both first installation and upgrade cutover, including stopping old processes
and verifying the running release; see
[access upgrades and recovery](installation.md#update-the-access-release).

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
