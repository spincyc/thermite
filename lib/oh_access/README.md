# Site access services

`thermite access stage` copies the program and every remote asset into a new
persistent private release, compiles authority from the validated house and
native providers, installs the hash-pinned site environment, and writes two
systemd units. All access units use `User=thermite`, `Group=thermite` and
`UMask=0077`; arbitrary service account overrides are refused. It never
installs or starts those units. No service path points
back into the source checkout. Use `--wheelhouse` for an offline installation.

Use a service-accessible root outside native runtime state and private operator
home directories: the installer uses `access/` in the installation root,
`/srv/thermite/access` by default. After the installer has created the
`thermite` account and group and the root-owned installation root, the owner
prepares its staging parents as the installer does:

```sh
ACCESS_ROOT=/srv/thermite/access
sudo install -d -o "$(id -un)" -g "$(id -gn)" -m 0755 "$ACCESS_ROOT" "$ACCESS_ROOT/releases"
```

Stage as the operator, with `--destination "$ACCESS_ROOT/releases/first"` and
`--state "$ACCESS_ROOT/state"`. Explicit TLS/key/token inputs remain in the
operator's custody; staging copies them into the new release. The receipt's
`ownership_commands` invokes the operator-owned custody helper for the finite
release and state manifest, followed separately by its
`verification_command`. Normal output is a concise summary; `--json` returns
the full receipt for automation. `--port`, the HTTPS listener, defaults to the
selected site's `gateway.port` in `instance/host.yaml` (13601 without one);
install and upgrade always take it from there, never from the release they
replace, so an upgrade can move it.

The required `--plan-directory` saves an operator-owned receipt, `handoff.sh`
and `activate.sh`
outside the release and state. Its parent must already belong to the operator
with mode 0700; the [installation guide](../../docs/installation.md#set-up-https-and-accounts)
creates that parent explicitly. Review the script, then run it with `sh` as
the operator. Only its exact administrative commands use sudo. The script
validates every planned path without following symlinks, applies permissions
through opened file descriptors, and verifies custody as thermite, stopping
on failure. Its private operator-owned copy remains readable for replay after
partial or complete handoff. Keep its helper and manifest in that trusted
operator directory; never execute a privileged helper from the service-owned
release. The plan also retains protected unit copies for the privileged
installation step; never install units from the service-owned release.
Bootstrap and activation remain separate. Release and state ancestors must
also be owned by root or the operator and protected from group/world writes;
root-owned sticky directories are allowed because they protect each trusted
child from replacement. Group- or world-writable access parents, such as an
older `2770` one, must be tightened before staging. Staging as the service
account is refused.

Python environment installation writes a private `environment-install.log`
inside the operator's plan. On environment or dependency failure, staging
removes the partial release and retains only the diagnostic plan, without
changing existing authority. Review that log locally and retry with a new
plan directory; diagnostic contents are never printed or copied into receipts.

No ownership changes or privileged commands run during staging. Once handed
over, release and state directories belong to
`thermite:thermite` with mode 0700, ordinary files are 0600, and release program
and environment executables are 0700. The two staging parents retain their
operator/group ownership so the operator can prepare the next release.

Pass a separate persistent `--state` directory and reuse it for every release
of the same site. Releases share that directory's site identity, signing key,
users, policies, durable claims and locks. Staging validates custody but does
not migrate SQLite or initialize authority. Bootstrap only a genuinely new
site; existing-site schema migrations require an explicit backed-up upgrade.

Staging accepts an existing `thermite:thermite` mode-0700 state root without
opening its contents. It validates new release inputs against a temporary empty
state directory, then removes that directory. Existing authority is unchanged.
`access verify` runs read-only as thermite, verifies complete release/state
ownership, group, modes, bounded internal environment links and TLS/configuration,
and refuses missing state instead of initializing it.

For a new site, the owner runs verification and then the receipt's
`bootstrap_command`, replacing `<admin-user>` with the first administrator name:

```sh
RELEASE="$ACCESS_ROOT/releases/first"
sudo -u thermite -g thermite "$RELEASE/env/bin/python" "$RELEASE/program/bin/thermite" access verify --config "$RELEASE/config.json"
sudo -u thermite -g thermite "$RELEASE/env/bin/python" "$RELEASE/program/bin/thermite" access bootstrap --config "$RELEASE/config.json" --user admin
```

Both password prompts are hidden and require a terminal;
there is no default password. Existing identities cannot
be replaced by running bootstrap again. The state directory is mode 0700;
the database, secret, identity, runtime bearer and configuration are mode 0600.
Bootstrap, service and worker CLI operations require the thermite account and
effective primary group. Personal terminal login/logout credentials remain
operator-owned.

To create the first administrator on the web Security page instead, replace
the bootstrap command with:

```sh
sudo -u thermite -g thermite "$RELEASE/env/bin/python" "$RELEASE/program/bin/thermite" access setup --config "$RELEASE/config.json"
```

This initializes a fresh schema with authentication off and prints a one-use
code to the interactive terminal. After completing the activation boundary
checks below and starting the gateway, open `/security/`, enable authentication,
and enter the code plus the first administrator's username and password. The
code expires after 15 minutes; run setup again for a replacement. It cannot
replace existing accounts. Keep the code out of logs, URLs and shared messages.

The Security toggle changes private identity state; it does not regenerate
inventory, restart services, or switch clients to native REST. Guests can use
ordinary controls in unlocked rooms through the gateway in either mode; camera
media needs an account while authentication is on.
Account management, pairing, lock changes and schedules require authentication.
With authentication off, locks and schedules are suspended. Accounts and
policies survive toggles, but every prior
browser session and terminal token is revoked. Subsequent toggles require an
active administrator's current password. Use the Account page to switch to an
Admin, or have an Admin change your role in Users and sign in again. Terminal
elevation uses `thermite access login --url https://… --user …` and reopening
the remote. No application role grants host sudo privileges.

`thermite upgrade` sets the entire persistent state directory aside after
stopping both access services and before migrating, and puts it back if the
upgrade rolls back (docs/installation.md). By hand, before upgrading an
existing identity schema, stop both access services, copy the entire
persistent state directory aside privately, and use the new release's
interpreter and program:

```sh
sudo systemctl stop thermite-access.service thermite-access-worker.service
sudo -u thermite -g thermite "$RELEASE/env/bin/python" "$RELEASE/program/bin/thermite" access migrate --config "$RELEASE/config.json"
```

Copy the state after stopping services and before the migration command.
Then activate the new release using its reviewed plan. `serve` and `worker`
refuse pending migrations; they never silently upgrade existing authority.
The security-state migration preserves existing accounts as authentication
enabled. The principals migration replaces roles and room grants with one
`admin` flag: accounts holding the reserved Admin role become admins and all
others become Members; custom roles, Viewer and room grants are dropped.
For rollback across a schema change, stop both services and restore the whole
matching copy before activating the prior release. Do not mix an older
database with newer signing keys or a different site identity.

`serve --config …` loads a private compiled contract and explicit host list,
validates the TLS certificate/key, and runs one direct-TLS ASGI process on
Hypercorn. TLS 1.2 or later offers HTTP/2 before HTTP/1.1, so one browser's
pages share a connection. At most 100 requests run at once, each HTTP/2 stream
counting alone, and more are answered 503; WebSocket handshakes are refused.
Shutdown gives requests 15 seconds, then cancels the rest. It
does not trust proxy headers, environment HTTP proxies, redirects, cookies
from the native runtime, or arbitrary caller-supplied upstream URLs. The
separate `worker --config …` uses the same SQLite database and sorted
`room:<id>` / `device:<id>` file locks. One process lease prevents a second
worker. No database transaction spans device I/O.

The worker observes only exact native `ItemStateUpdatedEvent` topics for
compiled route fields. It starts Unknown, clears observations after any
stream disconnect, and never obtains observation timestamps from REST reads
or command acknowledgements. Apple TV power additionally requires its
provider's acquisition timestamp before the paired power update. A missing,
stale or invalid timestamp stays Unknown; native drift can trigger only the
engine's bounded mismatch repairs. A reading is fresh for 30 seconds after its
update (`Field.max_age`), and only a fresh mismatch is repaired: one Item per
pass, in route order, once that Item's backoff allows. An Item's repeated
repairs wait 2, 5, 10, then 20 seconds (`BACKOFF`), each inside those 30
seconds, so a mismatch a device reports during a wait is still fresh when it
ends; any two repairs in a room are at least 2 seconds apart. Only repair
writes count, never reads, wake-ups or Unknown passes, and a room that
verifies between two repairs does not reset it: an Item starts again at 2
seconds after 60 seconds without a repair (`SETTLED`). A device that reports
only when it changes is therefore Unknown 30 seconds after each change and is
repaired whenever it reports drift; one already wrong when the worker starts
or its feed reconnects is repaired once it next reports. Source boundary
commands remain durably claimed once, with uncertain outcomes preserved after
transport failure. A
catch-up start reads the room's Source Item first and sends nothing when it
already equals the target; that read is never an observation. Stream failures
are logged at WARNING at most every five minutes, naming only the exception
class and how many failures were not logged.

Every command the worker sends has a `cmd` line in the command trail
(`origin=schedule` or `origin=repair`; see `oh_identity`). The worker also
logs, at INFO, one `boundary` line per boundary outcome (key, room, revision,
event, target, status, reason, command ID), one `repair` line per repair write
(room, item, value, revision, command ID, outcome, and attempt: that Item's
repairs since it last settled) and one `phase`
line when a room's phase or policy revision changes. The 10-second heartbeat
logs nothing.

`thermite access audit --config FILE [--since 7d|2026-11-04T18:00]
[--action login] [--json]`, run as the service account like `prune`, prints
the security audit oldest first: time, action, outcome, principal, actor,
target and the first 8 characters of the peer digest, as the journal's
`audit` lines do. `--action token` selects `token.create` and `token.revoke`.
It reads only.

A policy row for a room absent from the compiled house is ignored; remove such
rows with `thermite access prune --config …` as the service account. A
present room's policy that no longer validates (for example, a retired
source) stops enforcement only in rooms that share a device with that room;
unrelated rooms stay enforced. The worker also prunes expired identity
records once a day.

Terminal clients need no login for ordinary Guest controls. To elevate, use
`access login --url https://… --user …`. `access status` reports the
`/api/v1/me` principal (`id`, `username`, `active`, `admin`, `is_guest`,
`auth_enabled`, `can_edit`, `site`, `permissions`). The personal
token is stored atomically in an owned mode-0600 regular file under
`${XDG_CONFIG_HOME:-~/.config}/thermite/sites/<sha256-origin>.json`. Its five
fields are `version`, `origin`, `token`, `id`, and `expires_at`. A private CA
path, when supplied, lives separately in the matching `.ca.json`; the remote
launcher gives Node that trust path before startup. Tokens and passwords are
never command arguments, environment values or normal output. `logout`
revokes first and removes only the unchanged credential file; a credential the
site already rejects (401) is removed as already signed out.

## Metrics

No access service opens a metrics port, and the gateway never serves
`/metrics` on its LAN-facing TLS listener. Each service keeps its own
`prometheus_client` registry (`oh_access.metrics`, hash-pinned with the
release) and, every 10 seconds, writes it to
`/srv/thermite/runtime/textfile/<unit>.prom`: a new mode-0640 file renamed over
the old, so a scrape never reads half a file. The staged units name that
directory in `THERMITE_TEXTFILE_DIR` and may write only there; node_exporter's
textfile collector serves the files, and `node_textfile_mtime_seconds` shows a
writer that stopped. Without the variable nothing is written.

- The gateway, wrapped around admission so its 503s count too: requests by
  listener (`https` or `guest`), route class and status class; request and
  runtime-request seconds; problem answers by code; open event streams by view
  and HTTP version, the most any one peer holds, refusals by the limit that was
  full and lifetimes by how they ended (an `oh_identity.hub` observer); the
  shared runtime subscription's state, connections, failures and resyncs, and
  the events, frames and overflow resyncs it served (`EventHub.status()`); and
  what clients reported through `/api/v1/telemetry`.
- The policy worker: when it last finished a pass, rooms by policy state,
  schedule boundaries by outcome and their lag, repairs, the oldest locked-room
  check, and errors by stage.
- The callback relay: callbacks by outcome and when it last forwarded one.

Every label value is finite: route and status classes, problem codes,
outcomes and stages named in `oh_access.metrics`, and room IDs from the
compiled house. No path, peer address, credential or free text becomes a
label.

## Optional device callbacks

`stage --callbacks <private-file>` validates and copies an explicit callback
configuration and stages a third unit. Without it, the release still has only
the HTTPS service and policy worker. No unit is installed or started.
`thermite access callbacks --config <private-file>` runs that separate legacy
plaintext UPnP ingress; it has no credential or site-state input.

The mode-0600 JSON has exactly `version: 1`, `routes`, `upstream`, `hosts`,
`bind_host` and `port`. `routes` maps each verified native callback path to
its publishing Sonos peer IPv4 address. The bind address is one explicit LAN
IPv4 address; `hosts` contains exactly that address and port. The upstream
must match the site's fixed loopback native HTTP origin. There are no wildcard
paths, browser endpoints, REST proxying or automatic device grants.

The relay accepts only bounded, validated NOTIFY events. It rejects cookies,
authorization, redirects, ambiguous framing and DTD/entity declarations;
it forwards no upstream response content or credentials. Its pinned h11
server bounds connections, initial-header time, body time and upstream time.
The native runtime retains subscription SID/sequence authority.

Before activation, verify the actual native SUBSCRIBE callback address,
port and path, then a notification that changes the native Item. jUPnP's
`httpProxyPort` changes the advertised port; it does not create this listener.
openHAB gets it as it starts, from `conf/services/upnp.cfg`, which `thermite
generate` writes for a house with Sonos devices (the devices commissioning
maps). Commissioning only reads it back and fails if openHAB does not use the
relay's port: changed while openHAB runs, jUPnP restarts and openHAB answers
every NOTIFY with HTTP 501 until it restarts.
An HTTP 200 alone does not prove a state update or callback preservation.

## Optional Guest listener

`guest_http` in `instance/host.yaml` (validated by `oh_access.guest`, staged
into `config.json` version 2) adds plain-HTTP Hypercorn listeners to the HTTPS
service itself, one per `listen` address; no unit is added. Their sockets
close a connection whose peer lies outside `clients` as it is accepted,
before the server reads it. They serve the same application and share the
HTTPS listener's 100-request budget, wrapped by `guest.mark`, which sets the
listener in the ASGI scope. A listener that cannot bind stops the service's
startup. `oh_identity.guest` then answers those requests only as a Guest. See
[Guest access over plain HTTP](../../docs/installation.md#guest-access-over-plain-http).

## Activation boundary

The staged receipt explicitly says `security_boundary_active: false`.
Before owner activation, seal every bypassing native HTTP, HTTPS, websocket
and alternate UI listener, disable implicit roles and trusted-network grants,
and verify client TLS trust. Preserve Sonos/UPnP callbacks during this change:
loopback binding alone can break their LAN callback URLs. This package does
not enable an unverified callback relay. The owner must validate the exact
native callback path and either its supported listener/proxy setting or an
independently allowlisted relay before changing listeners.

Only after custody verification and that checklist is satisfied should the owner
review and run `sh "$PLAN/activate.sh"`. It verifies custody again before
stopping installed access services, installs the protected unit copies,
reloads systemd and enables/starts the new release. It checks each active
MainPID's working directory against the staged release, with a bounded retry;
the owner then checks HTTPS login, controls and callbacks. A callback unit
omitted from the new release is stopped and disabled. Replaying a plan whose
units and running release already match does not restart services.

Each upgrade uses a new release and plan with the same persistent state and
no bootstrap. If cutover fails, fix the cause and rerun the saved activation
script; services may be stopped. Keep the old release and plan for rollback
after checking shared-state compatibility. The
[installation guide](../../docs/installation.md#update-the-access-release)
gives the full upgrade and recovery procedure. `activation_commands` in the
JSON receipt invokes the saved script; `unit_install_commands` records its
protected unit-copy operations for review.

Staging, a responsive login page,
or a working authenticated client alone does not establish that direct native
access has been sealed. Existing hardware remotes and device protocols can
still cause drift; room locks provide bounded reconciliation, not a physical
interlock. Passive endpoints remain explicitly unverifiable.
