# Get started

thermite runs on one Linux machine on your home network, next to openHAB. You describe the house, generate its configuration and check each step before installing it. Current application builds are development version `0.1.0.dev0`.

## What you need

- A Linux machine that stays on: a small server or a mini PC.
- Podman, Python 3.11 or later and Git. Node.js for the terminal remote.
- Your devices on the same network, each with a fixed address.

## Install the application

From the reviewed public source checkout, install thermite in a dedicated Python environment:

```sh
python3 -m venv ~/.venvs/thermite
~/.venvs/thermite/bin/python -m pip install .
. ~/.venvs/thermite/bin/activate
thermite init ~/my-thermite-site
cd ~/my-thermite-site
```

You can also install a reviewed wheel with `python -m pip install /path/to/thermite_app-0.1.0.dev0-py3-none-any.whl`. PyYAML is the application's configuration dependency; runtime downloads, the terminal's locked Node package and optional service dependencies have their own explicit setup steps.

The installed program stays separate from your private site. `init` writes a fictional inventory, sample host settings, runtime pins and a private launcher. It does not create a Git repository, contact devices, install services or activate accounts. Commands recognize the site in the current directory; use `thermite --site /path/to/site ...` elsewhere.

## 1. Describe the house

Edit `instance/devices.yaml` and `instance/host.yaml` in your new private site. Its initial devices are fictional; replace them before commissioning real hardware. Create a private Git repository for this site. Deployment installs committed files: generate and privately commit the site manifest, configuration, launcher, pins, `conf/`, `metrics/` and `deploy/` before installing. Never publish house facts or generated private outputs to the public thermite repository.

The house file lists the rooms and the devices in them. A room with a TV looks like this:

```yaml
rooms:
  living_room: {label: Living Room}

devices:
  - id: living_room_tv
    room: living_room
    kind: tv
    make: Sony
    model: KD-55X85K
    name: Living Room TV
    ip: 192.0.2.20
    controls:
      observe: [network_ping, sony_rest]
      control: [sony_ip_control]
```

`controls` says how thermite may reach a device: *observe* only reads it, *control* sends it commands, best first.

## 2. Generate and try

```sh
thermite generate        # write openHAB's files from the house file
thermite validate        # check inventory and generated files
thermite test boot       # boot a throwaway openHAB on them and report what loads
thermite sandbox start   # or keep one running, to click around in
```

## 3. Install

```sh
thermite install --dry-run   # each step it would take
thermite install             # take them; it asks before anything that needs sudo
thermite deploy              # put the tested configuration in place
thermite status              # what runs, and whether it matches
thermite upgrade --dry-run   # later: what moving to newer pinned versions would do
```

The existing complete source-checkout workflow remains available. An installed private site additionally pins an exact application version and release digest. To change the application, install its reviewed package, then run `thermite init --upgrade /path/to/site`; configuration, state and cached older releases are preserved. Generate, validate and privately commit the result before deployment. Runtime pins and application upgrades are separate. Keep `var/application-releases/` for rollback: a missing or modified release is refused, not downloaded or silently replaced.

## 4. Set up site access

Authenticated access is the fictional site's default. The web and terminal clients share site accounts once the owner explicitly commissions that boundary. Admins manage users, roles and room schedules; ordinary users control unlocked rooms, and viewers have read-only access. A scheduled lock has an expected room state that thermite reconciles while the lock is active. Installing the application does not activate this service or protect an existing native listener.

Run `thermite access stage --help` to prepare a private, persistent site release. Staging needs a trusted TLS certificate, a service-owned native API credential, the generated remote assets, an explicit site hostname and a separate persistent `--state` directory. Reuse that state across releases to preserve users and policies. Staging writes the service units and an activation checklist; it does not start them or migrate the database.

Bootstrap the first admin locally from that release with `thermite access bootstrap --config <release>/config.json --user admin`. Password prompts are hidden. Before activating, follow the checklist to close direct native HTTP, HTTPS and websocket access, disable implicit grants, and preserve device callbacks such as Sonos. Accounts do not protect a separate native listener left open.

After activation, admins can use Settings and Users in either client. Other users can see room settings but cannot change them. Locks reconcile supported device reports; passive equipment and stale readings remain explicitly unverified.

## 5. Pair your devices

Some devices need a one-time handshake: they show a code on screen, which you type into Remotes → Pair devices. On a compatible Sony TV, set IP control's Authentication to Normal and turn on Remote Start; the menu path varies by model. Pairing availability depends on the device's firmware, and thermite reports failures on its pairing page. A Sony TV can also be paired from a terminal:

```sh
thermite pair living_room_tv
```

## 6. Use it

- On a phone or a computer, open `https://<site hostname>:9443/static/remote/home.html` and sign in.
- In a terminal, run `thermite access login --url https://<site hostname>:9443 --user <username>`, then `thermite remote --url https://<site hostname>:9443`. Both clients start with Home, with Remotes, Security, Cameras and Site views.

Use `--ca-file` at login when your site uses a private CA. The terminal stores its revocable credential in a private file. Native runtime access is an explicit owner/testing option (`thermite remote --native`); authentication failures never select it automatically.

## When something goes wrong

- `thermite health --production` lists every device that isn't online.
- `thermite rollback` puts the previously deployed version back; `thermite history` lists them.
- Every command explains itself: `thermite help <command>`. The [commands page](commands.html) has them all.
- `thermite version` shows which thermite and which openHAB you run, and `thermite completion bash` (or `zsh`, `fish`) gives your shell tab completion.
