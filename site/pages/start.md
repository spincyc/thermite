# Get started

thermite runs on a Linux machine on your home network. Choose where it will live, or try the fictional example first. These are development builds, version `0.1.0.dev0`.

## Choose where to run it

| Your setup | Installation path |
|---|---|
| Stock Arch Linux, or an existing Ubuntu Server | [Install thermite on your server](installation.html). Keep your existing operating system and create a private site. |
| A dedicated machine | [Build the standalone ISO](iso.html). Arch is the default base; Ubuntu is optional. Boot it to install the operating system and thermite on that machine. |

Review the [system requirements](requirements.html) and [network ports](ports.html) before installing; [music](music.html) adds Music Assistant for TIDAL and your music libraries. Administer Thermite with your own login; application services use a separate [service account](service-account.html).

## Try the example

You can generate configuration and use the terminal remote in demo mode
before connecting hardware. On stock Arch, prepare the development tools:

```sh
sudo pacman -Syu --needed python python-pip git nodejs npm
```

On Ubuntu Server, use `sudo apt-get update` followed by
`sudo apt-get install python3-venv git nodejs npm` instead. From a reviewed
source checkout, create a separate application environment:

```sh
python3 -m venv ~/.venvs/thermite
~/.venvs/thermite/bin/python -m pip install .
. ~/.venvs/thermite/bin/activate
thermite init ~/my-thermite-site
cd ~/my-thermite-site
thermite generate
thermite validate
thermite remote --mock
```

The initial inventory is fictional. Generating and validating files sends no device commands; the mock remote simulates controls. Its first run downloads its locked terminal dependency with npm. Podman is needed when you move on to an openHAB test runtime or server installation.

The application stays in its Python environment. `init` creates a separate private site; it starts no services and creates no Git repository.

## Describe your house

Edit `instance/devices.yaml` and `instance/host.yaml` in the private site. Replace the example inventory with your own rooms and devices, using fixed network addresses. [The house file](house-file.html) lists every key `devices.yaml` may have. A TV entry looks like this:

```yaml
rooms:
  living_room: {label: Living Room}

devices:
  - id: living_room_tv
    room: living_room
    kind: tv
    make: Sony
    model: Example TV
    name: Living Room TV
    ip: 192.0.2.20
    controls:
      observe: [sony_rest]
      control: [sony_ip_control]
```

`observe` lists interfaces thermite may read. `control` lists interfaces it may command. The sample address above is reserved for documentation; use the TV's actual address in your private site. `mac` is optional except for Sonos devices, and `notes` and `control4` are accepted for your own documentation. `monitor: optional` keeps a device you know is broken out of control health and alerts; see [dashboards](dashboards.html). Unknown keys are refused with a "did you mean" hint; an invalid or missing file exits with status 2 and names the file, line and column. The file is YAML (JSON is YAML too), and thermite takes only what means one thing in it: unquoted text that the versions of YAML read differently (`on`, `no`, `010`, `12:30`, a hardware address made of digits only, a date) and a key written twice in one mapping are refused the same way, with the line and what to write, which is usually the same text in quotes. `instance/host.yaml` is read the same way.

Create a private Git repository for that site. Commit real inventory, host settings, native version pins and `thermite-site.json`. Generated `conf/`, `metrics/` and `deploy/`, credentials and runtime state stay outside Git. The public application repository should contain only fictional examples.

## Check and deploy

After editing the house file:

```sh
thermite generate
thermite validate
thermite test boot
```

`thermite validate` runs every check `thermite generate` runs. It also names, in a note that is no problem, a device nothing watches: one an interface commands with no reading of whether it answers, so no alert could say it stopped, or one that has an address and no reading at all, which `network_ping` under its `observe` gives it. Both verbs name, in a note too, an interface a device lists that thermite does not have yet: the name is accepted and nothing is generated for it. `thermite generate --prune` removes files an earlier generate wrote that nothing generates now. The boot check loads the generated configuration in an isolated openHAB runtime and reports what loads. Follow the [server installation guide](installation.html) to prepare the host, commit your private site and deploy it. `thermite install --dry-run` shows the planned installation steps. `thermite status` reports the running services and their addresses.

## Connect and use it

Installation prints your HTTPS address and the certificate authority file each
browser or terminal trusts once. Ordinary controls work before any account exists. Use the Security page to
create the first administrator with owner proof. The local terminal uses the
installed site's verified endpoint and certificate automatically:

```sh
thermite remote --config /path/to/private-site
```

To sign the terminal in to an account, first run `thermite access login --config
/path/to/private-site --user admin`. Any other computer with thermite
installed runs `thermite tui`, which needs no private site and opens
`https://thermite:13601` (`--url` names another server, and `HOST:PORT` a
gateway moved off 13601); pass a copy of that
authority file once with `--ca-file`. Use the same `--url` and `--ca-file`
with `thermite access login` to sign in there. Guests control
unlocked rooms; signed-in accounts add camera snapshots and live video;
administrators manage locks, schedules, users and pairing. Choose a room and source
in Remotes; use Pair devices for equipment that needs an on-screen code.
Cameras and Site show cameras and connections. Alarm-device status requires
an alarm integration.

## Keep it running

- `thermite upgrade --config DIR` updates the application and all managed services.
- `thermite health --production` reports device health.
- `thermite device ID` shows everything about one device: what it is, what watches it and what holds now.
- `thermite history` lists deployments; `thermite rollback` restores the previous one.
- `thermite version` shows application and runtime versions.
- [Application releases and private sites](packaging.html) explains application upgrades and cached releases for rollback.
- `thermite help <command>` explains any command, with examples.
