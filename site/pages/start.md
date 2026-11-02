# Get started

thermite runs on a Linux machine on your home network. Choose where it will live, or try the fictional example first. These are development builds, version `0.1.0.dev0`.

## Choose where to run it

| Your setup | Installation path |
|---|---|
| Stock Arch Linux, or an existing Ubuntu Server | [Install thermite on your server](installation.html). Keep your existing operating system and create a private site. |
| A dedicated machine | [Build the standalone ISO](iso.html). Arch is the default base; Ubuntu is optional. Boot it to install the operating system and thermite on that machine. |

Check the [system requirements](requirements.html) first. Plan for 2 cores,
4 GiB RAM and a 64 GB SSD for light use, or 4 cores, 8 GiB and 128 GB for
more headroom. These full-stack estimates are not benchmarked capacity
limits. Each installation path uses the same application, house file and
remotes. The [ports guide](ports.html) lists the services and their exposure.

You administer Thermite with your own login. Its application services and deployed data use the non-login `thermite:thermite` account, UID/GID 9001. The [service account guide](service-account.html) covers permissions and upgrades from the old `openhab` account name.

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

The application stays in its Python environment. Your private site contains the inventory, host settings, generated configuration and runtime state. `init` creates files; it does not install services or create a Git repository.

## Describe your house

Edit `instance/devices.yaml` and `instance/host.yaml` in the private site. Replace the example inventory with your own rooms and devices, using fixed network addresses. A TV entry looks like this:

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

`observe` lists interfaces thermite may read. `control` lists interfaces it may command. The sample address above is reserved for documentation; use the TV's actual address in your private site.

Create a private Git repository for that site. Keep real inventory, host settings and generated `conf/`, `metrics/` and `deploy/` there. Credentials and runtime state stay outside Git. The public application repository should contain only fictional examples.

## Check and deploy

After editing the house file:

```sh
thermite generate
thermite validate
thermite test boot
```

The boot check loads the generated configuration in an isolated openHAB runtime and reports what loads. Follow the [server installation guide](installation.html) to prepare the host, commit your private site and deploy it. `thermite install --dry-run` shows the planned installation steps. `thermite status` reports the running services and their addresses.

## Connect and use it

The installation guide covers site access and the first account. After commissioning authenticated access, open your site's web address and sign in. In a terminal:

```sh
thermite access login --url https://thermite.example:13600 --user admin
thermite remote --url https://thermite.example:13600
```

Use `--ca-file` at login if your site uses a private certificate authority. Remotes lets you choose a room and source; Pair devices handles devices that need an on-screen code. Security shows Not connected until an alarm integration is configured. Cameras and Site show the camera catalogue and configured connections.

## Keep it running

- `thermite health --production` reports device health.
- `thermite history` lists deployments; `thermite rollback` restores the previous one.
- `thermite version` shows application and runtime versions.
- [Application releases and private sites](packaging.html) explains application upgrades and cached releases for rollback.
- `thermite help <command>` explains any command, with examples.
