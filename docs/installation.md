# Install on a Linux server

Thermite runs on one x86-64 Linux machine with systemd. Arch Linux is the
primary path; Ubuntu Server 24.04 LTS is the alternative shown below.
Start with the [system requirements](requirements.md): the full stack's
planning minimum is 2 cores, 4 GiB RAM and a 64 GB SSD, with 4 cores,
8 GiB RAM and 128 GB recommended. These estimates are not benchmarked limits.

These are development builds, currently `0.1.0.dev0`. A wheel installs the
application; a private site holds your configuration and state. Installing
the application starts no service. Commissioning the runtime is a separate,
interactive step. For a bare machine, the [installer ISO](iso.md) defaults
to Arch and also offers an Ubuntu base. After either ISO installation,
continue at “Create your private site.”

For a stock OS installation, first follow the official
[Arch installation guide](https://wiki.archlinux.org/title/Installation_guide)
(or its guided `archinstall` installer), or
[Ubuntu Server installation guide](https://ubuntu.com/server/docs/installation/).
Create your own login with sudo access and working networking, then reboot
into the installed system. The commands below run there, not in the live
installer environment.

## Install the application

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

Then use the same application setup on either distribution:

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
`thermite` to PATH. Skip the environment setup above on that machine.

## Create your private site

```sh
thermite init "$HOME/thermite-site"
cd "$HOME/thermite-site"
```

Edit `instance/devices.yaml` and `instance/host.yaml` for your own equipment,
time zone and state directory. Init's inventory is fictional: use it only
for the mock example, then replace its devices before commissioning real
equipment. Keep the default authenticated access mode
unless you explicitly intend unrestricted native access on your trusted LAN.
Port assignments are listed in [ports](ports.md).

```sh
thermite generate
thermite validate
git init --initial-branch=main
git add .
git commit -m 'Initialize private Thermite site'
```

Configure your Git author name and email if Git requests them. Keep this
repository private. Commit generated `conf/`, `deploy/`, `metrics/`, the site
manifest and configuration. Credentials, caches and runtime state stay
outside Git. Init supplies the ignore rules. It creates no Git repository or
commit automatically.

Use commands from inside the site, or pass `thermite --site /path/to/site`.
The private site's `bin/thermite` also selects its application environment.
The installed application remains separate from mutable site files.

## Commission the runtime

Use a terminal on the server, as the owner account, with Internet access:

```sh
thermite install --dry-run
thermite install
```

Install, upgrade and uninstall show compact stage status, with checkmarks
and a spinner on a terminal. Use `thermite -v install` (or `upgrade` or
`uninstall`) for stage details and commands. Failed checks print their
diagnostics. Package transactions, sudo and other interactive commands keep
the terminal while the spinner pauses; `--dry-run` lists the planned
commands without running them.

Read the plan first. The installer checks architecture, kernel support,
ports and IDs; installs prerequisites; creates the service account and state
directories; boots the configuration on a test runtime; deploys the committed
site; installs systemd Quadlets; starts services; and requests the runtime API
token. It asks once to approve the whole run, including root commands and the
deployment ownership handoff. It can be rerun after a failure or a declined
step. Do not run the whole CLI as root.

Application services run as the non-login user and group `thermite`, UID/GID
9001, and deployed files belong to that account. Your login keeps ownership
of the private site, launcher and Git repository. The host's systemd manager
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

Existing installations retain their numeric IDs and data paths when the old
`openhab` account is renamed; see
[service ownership and migration](service-account.md).

The native boot test downloads its pinned runtime and JRE. Before starting
or restarting a service, the installer downloads any missing pinned container
image with visible progress. Downloads finish before the service startup
timer begins. This can take several minutes. Follow the
runtime UI link printed by the installer to create its administrative login
and API token. The token prompt hides the paste and saves it privately.
Log out and back in after the installer adds you to the service group.

For newly initialized native userdata, preparation builds the OSGi cache
with empty add-on configuration and waits for that temporary JVM to exit
before starting the real runtime. This avoids the upstream first-start
cache race. Existing userdata without a cache uses normal startup, so
offline preparation does not run its managed rules or Things.

After validating the API token, installation waits for the required add-ons,
Thing handlers and authenticated native metrics. Unchanged known first-start
handler symptoms can prompt one reviewed restart of `openhab.service` within
the startup budget. Configuration and authentication errors require fixing
their reported cause. A persistent failure stops installation and retains
userdata and credentials. Failed fresh cache priming also retains its
`.thermite-prime-incomplete` marker and logs; inspect them before explicit
recovery. Preparation and normal startup refuse that incomplete cache.

On a slow machine or under software CPU emulation, give the boot check a
longer startup budget, then rerun the installer:

```sh
thermite test boot --timeout 1800
thermite install
```

A failed check does not approve a deployment. The longer check must pass
against the same committed configuration before installation continues.
If a system update replaced the running kernel's modules, reboot before
commissioning so Podman can use overlayfs.

Native runtime installation is followed by the HTTPS and account setup below.
The ISO and application installation do not create credentials, provision
TLS or start the authenticated frontend for you.

```sh
thermite status
thermite health --production
thermite access --help
```

Health reports real device availability; fictional example addresses will
not establish a healthy house. Pair and verify equipment before relying on
automation; `thermite pair --help` lists supported pairing steps.

## Set up HTTPS and accounts

Choose a hostname that your clients resolve to this server and provide a TLS
certificate valid for that name. The certificate must be trusted by every
browser and terminal client. Keep its private key in an owned mode-0600 file.
A certificate from your existing local CA or public CA works; certificate
issuance and DNS administration depend on your network.

The application setup above provides Python virtual environments on either
distribution. Run these commands as the same owner who installed the private
site, after
logging back in for membership in group `thermite`. Adjust
the hostname, certificate paths and native state directory to match your
installation; `/srv/thermite` is the sample `state_root`. Enter your private
site after logging in. Its launcher selects the application's Python
environment, including when your previous shell's activation has ended:

```sh
cd "$HOME/thermite-site"
HOST_ID=$(python3 -c 'import socket; print(socket.gethostname().split(".", 1)[0])')
ACCESS_ROOT=/srv/thermite-access
SITE_HOST=thermite.home.arpa
RUNTIME_STATE=/srv/thermite
TLS_CERT="$HOME/.config/thermite/tls/fullchain.pem"
TLS_KEY="$HOME/.config/thermite/tls/key.pem"
PLAN_ROOT="$HOME/.thermite-access-plans"
install -d -m 0700 "$PLAN_ROOT"
PLAN="$PLAN_ROOT/first"
sudo install -d -o "$(id -un)" -g thermite -m 2750 \
  "$ACCESS_ROOT" "$ACCESS_ROOT/releases"
bin/thermite access stage \
  --destination "$ACCESS_ROOT/releases/first" \
  --state "$ACCESS_ROOT/state" \
  --assets "$RUNTIME_STATE/conf/html/remote" \
  --runtime-url http://127.0.0.1:13601 \
  --runtime-token-file "$PWD/var/$HOST_ID/api-token" \
  --tls-cert "$TLS_CERT" --tls-key "$TLS_KEY" \
  --allowed-host "$SITE_HOST" --port 13600 --timezone Etc/UTC \
  --plan-directory "$PLAN"
```

Keep the access root outside the native `state_root`, so runtime permission
maintenance cannot change its operator-owned release parents. All parent
directories must allow `thermite` to traverse them, but must be owned by root
or the operator and protected from group/world replacement. The `2750`
parents above let the operator stage releases without letting the service
replace a directory while staging executes its interpreter. For older `2770`
access parents, rerun the preparation command above to tighten both parents;
do not change the service-owned release or state directories. The stage
command rejects a release hidden behind a private home directory. It copies
the program, generated assets and explicit TLS/token inputs into a new
release, installs hash-pinned dependencies and writes units for
`User=thermite` and `Group=thermite`. It starts no service and runs no
privileged command.

Keep every plan ancestor owned by root or the operator and free of group/world
write permission. The home-based example avoids inheriting a shared site
directory's permissions; choose another protected parent if your home is shared.

The required plan directory holds a private receipt, custody helper,
`handoff.sh` and `activate.sh` in the operator-owned plan
directory. Review the script, then run it as your ordinary account:

```sh
more "$PLAN/handoff.sh"
sh "$PLAN/handoff.sh"
```

Only its explicit administrative commands use sudo. The helper validates
the finite manifest without following symlinks and changes opened files
through their descriptors, so replay cannot follow a replaced path outside
the release. The script gives the
listed release and new-state files to `thermite:thermite`, with directories
and executables at 0700 and other files at 0600, then verifies custody as the
service user. It stops on failure; fix the reported cause and rerun the same
script. Your copy remains readable after ownership changes. It does not
bootstrap accounts or activate services. Use `--json` when a tool needs the
full stage receipt.

Environment setup writes its diagnostics to the operator-owned mode-0600
`$PLAN/environment-install.log`. If Python environment creation or pinned
dependency installation fails, staging removes the partial release and keeps
only that protected diagnostic plan. Review the log locally; it can contain
private package-index details. Existing site state is unchanged. Correct the
cause and retry with a new plan directory; the same release destination can
be reused after its partial directory has been removed. Staging never
overwrites an existing plan or prints diagnostic contents into the receipt.

After verification, create the first administrator for a new site:

```sh
sudo -u thermite -g thermite "$ACCESS_ROOT/releases/first/env/bin/python" \
  "$ACCESS_ROOT/releases/first/program/bin/thermite" access bootstrap \
  --config "$ACCESS_ROOT/releases/first/config.json" --user admin
```

Bootstrap prompts privately for a new administrator password of at least
12 characters. Run it only once for a new site, under the service account;
do not bootstrap an existing site again. Keep the same `--state` directory
across later releases to retain accounts, policies and locks. For older access
state inside a private home, follow the
[one-time state move](service-account.md#move-existing-access-state) before
staging the replacement. The native runtime's `state_root` stays in place.

Before activation, close direct native HTTP, HTTPS, websocket and language
server access from household clients, and turn off the runtime's implicit
user role and trusted-network grants. In native Main UI these are under
Settings → API Security. Keep native loopback HTTP available to the gateway
and metrics. Sonos/UPnP installations also need a verified callback path:
binding native HTTP to loopback can otherwise break device updates. The
optional `thermite access stage --callbacks FILE` stages an explicit relay;
its exact peers and paths must be verified for your devices.

This native-listener cutover remains a site-specific administrator step.
The installer does not apply firewall rules or invent callback maps. Until
that cutover has been verified, the HTTPS login does not enforce access
control over the whole installation. The
[access service reference](https://github.com/spincyc/thermite/blob/main/lib/oh_access/README.md) describes the callback
configuration and activation checks; the
[upstream security guide](https://www.openhab.org/docs/installation/security.html)
explains native access settings.

After resolving the activation requirements printed by stage (also saved in
`$PLAN/stage.json`), review and run the saved activation script as the operator.
It verifies custody before stopping any existing access services, installs the
protected unit copies, reloads systemd, enables/starts the staged services and
checks that each active process runs from this release. Only its explicit
administrative commands use sudo. It also stops and disables an old callback
unit when the new release omits callbacks. The script does not perform the
native-listener cutover or create accounts:

```sh
more "$PLAN/activate.sh"
sh "$PLAN/activate.sh"
bin/thermite access login --url "https://$SITE_HOST:13600" --user admin
bin/thermite remote --url "https://$SITE_HOST:13600"
```

With a private CA, add `--ca-file /path/to/ca.pem` at terminal login and
install that CA in each browser's trust store. Open
`https://<your-hostname>:13600/static/remote/home.html` in the browser.
Use its Users screen to create household accounts and room grants. Verify
that anonymous requests fail and that each role can access only its granted
rooms. Do not expose this service directly to the Internet; remote network
access can use your existing VPN.

## Update or remove

Keep the private site's verified application release cache under
`var/application-releases/` for rollback. Application updates and runtime
updates are separate: install a new reviewed application artifact, run
`thermite init --upgrade /path/to/site`, generate, validate and commit the
site, then review `thermite upgrade --dry-run`. See [packaging](packaging.md).

### Update the access release

The HTTPS gateway and worker use their own copied release. Updating the
application or native runtime does not switch those running processes.
Keep the current access release and its protected plan for recovery, and keep
a backup of its persistent state. Choose new release and plan names, then
repeat the stage command above with those paths, the original TLS/token
inputs, and the **same** `--state` directory:

```sh
RELEASE="$ACCESS_ROOT/releases/second"
PLAN="$PLAN_ROOT/second"
bin/thermite access stage \
  --destination "$RELEASE" --plan-directory "$PLAN" \
  --state "$ACCESS_ROOT/state" \
  --assets "$RUNTIME_STATE/conf/html/remote" \
  --runtime-url http://127.0.0.1:13601 \
  --runtime-token-file "$PWD/var/$HOST_ID/api-token" \
  --tls-cert "$TLS_CERT" --tls-key "$TLS_KEY" \
  --allowed-host "$SITE_HOST" --port 13600 --timezone Etc/UTC
more "$PLAN/handoff.sh"
sh "$PLAN/handoff.sh"
```

Staging and handoff preserve the existing authority and do not migrate its
database. Do not bootstrap again or replace the existing CLI login. Retain
`--callbacks` and its verified map if the site uses the relay; omitting it
intentionally retires that listener during activation. Complete the TLS,
native-listener and callback checklist, then review and run the new
`activate.sh` as above. It stops old access processes before installing the
new units, so a successful cutover cannot leave the previous release running.
Afterward, check HTTPS login, room controls and any device callbacks. Replaying
the same already-active plan verifies its processes and unit contents without
restarting them.

If activation fails, access services may be stopped. Fix the reported cause
and rerun the same protected `activate.sh`; it can resume a partial unit
installation. To roll back, first confirm that the prior release remains
compatible with the unchanged shared state, then review and run that prior
plan's `activate.sh`. If the old plan predates this script, restage the prior
application with a new plan and the same state. Never restore or initialize
authority merely to retry a cutover. A schema-changing upgrade needs its own
explicit backed-up migration and recovery procedure.

`thermite uninstall --dry-run` shows service removal. State is retained by
default; review the explicit purge option separately. Back up private
configuration, runtime data, credentials and release artifacts before
deleting an installation.
