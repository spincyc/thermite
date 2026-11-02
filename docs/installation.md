# Install on a Linux server

Thermite runs on one x86-64 Linux machine with systemd, Podman 4.4 or later,
Python 3.11 or later, Git and Node.js. Ubuntu Server 24.04 LTS is the reference
path; Debian installations need a sufficiently recent Podman. Arch Linux is
also supported by the package-install step. Allow several GB for container
images and runtime data, and keep backups outside the machine.

These are development builds, currently `0.1.0.dev0`. A wheel installs the
application; a private site holds your configuration and state. Installing
the application starts no service. Commissioning the runtime is a separate,
interactive step. For a bare machine, use the [installer ISO](iso.md).

## Install the application

On Ubuntu, prepare a Python environment as your ordinary owner account:

```sh
sudo apt-get update
sudo apt-get install python3-venv git
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
time zone and state directory. Init's inventory is fictional: remove its
devices before commissioning. Keep the default authenticated access mode
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

Read the plan first. The installer checks architecture, kernel support,
ports and IDs; installs prerequisites; creates the service account and state
directories; boots the configuration on a test runtime; deploys the committed
site; installs systemd Quadlets; starts services; and requests the runtime API
token. It displays and asks before each root command. It can be rerun after a
failure or a declined step. Do not run the whole CLI as root.

Application services run as the non-login user and group `thermite`, UID/GID
9001, and deployed files belong to that account. Your login keeps ownership
of the private site, launcher and Git repository. The host's systemd manager
and Podman backend remain root-managed. Existing installations retain their
numeric IDs and data paths when the old `openhab` account is renamed; see
[service ownership and migration](service-account.md).

The native boot test downloads its pinned runtime and JRE; service startup
pulls the pinned container images. This can take several minutes. Follow the
runtime UI link printed by the installer to create its administrative login
and API token. The token prompt hides the paste and saves it privately.
Log out and back in after the installer adds you to the service group.

Native runtime installation is followed by the HTTPS and account setup below.
The ISO and application installation do not create credentials, provision
TLS or start the authenticated frontend for you.

```sh
thermite status
thermite health --production
thermite access --help
```

Health reports real device availability. Pair and verify equipment before
relying on automation; `thermite pair --help` lists supported pairing steps.

## Set up HTTPS and accounts

Choose a hostname that your clients resolve to this server and provide a TLS
certificate valid for that name. The certificate must be trusted by every
browser and terminal client. Keep its private key in an owned mode-0600 file.
A certificate from your existing local CA or public CA works; certificate
issuance and DNS administration depend on your network.

Run these commands as the same owner who installed the private site, after
logging back in for membership in group `thermite`. Adjust
the hostname, certificate paths and native state directory to match your
installation; `/srv/thermite` is the sample `state_root`:

```sh
sudo apt-get install python3-venv
ACCESS_ROOT=/srv/thermite-access
SITE_HOST=thermite.home.arpa
RUNTIME_STATE=/srv/thermite
TLS_CERT="$HOME/.config/thermite/tls/fullchain.pem"
TLS_KEY="$HOME/.config/thermite/tls/key.pem"
sudo install -d -o "$(id -un)" -g thermite -m 2770 \
  "$ACCESS_ROOT" "$ACCESS_ROOT/releases"
thermite access stage \
  --destination "$ACCESS_ROOT/releases/first" \
  --state "$ACCESS_ROOT/state" \
  --assets "$RUNTIME_STATE/conf/html/remote" \
  --runtime-url http://127.0.0.1:13601 \
  --runtime-token-file "$PWD/var/$(hostname -s)/api-token" \
  --tls-cert "$TLS_CERT" --tls-key "$TLS_KEY" \
  --allowed-host "$SITE_HOST" --port 13600 --timezone Etc/UTC
```

Keep the access root outside the native `state_root`, so runtime permission
maintenance cannot change its operator-owned release parents. All parent
directories must allow `thermite` to traverse them. The stage
command rejects a release hidden behind a private home directory. It copies
the program, generated assets and explicit TLS/token inputs into a new
release, installs hash-pinned dependencies and writes units for
`User=thermite` and `Group=thermite`. It starts no service and runs no
privileged command.

Review the stage output and run its `ownership_commands` in order. They name
the finite release and new-state files to give to `thermite:thermite`, with
directories and executables at 0700 and other files at 0600. Follow with the
service-user verification and first administrator bootstrap:

```sh
sudo -u thermite -g thermite "$ACCESS_ROOT/releases/first/env/bin/python" \
  "$ACCESS_ROOT/releases/first/program/bin/thermite" access verify \
  --config "$ACCESS_ROOT/releases/first/config.json"
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
the now service-owned `stage.json`), install and enable its two units as the
host administrator:

```sh
sudo install -o root -g root -m 0644 "$ACCESS_ROOT/releases/first/units/thermite-access.service" /etc/systemd/system/
sudo install -o root -g root -m 0644 "$ACCESS_ROOT/releases/first/units/thermite-access-worker.service" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now thermite-access.service thermite-access-worker.service
thermite access login --url "https://$SITE_HOST:13600" --user admin
thermite remote --url "https://$SITE_HOST:13600"
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

`thermite uninstall --dry-run` shows service removal. State is retained by
default; review the explicit purge option separately. Back up private
configuration, runtime data, credentials and release artifacts before
deleting an installation.
