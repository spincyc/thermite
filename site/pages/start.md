# Get started

thermite runs on one Linux machine on your home network, next to openHAB. You describe the house, thermite does the rest, and you check each step before it touches anything.

## What you need

- A Linux machine that stays on: a small server or a mini PC.
- Podman, Python 3.11 or later and Git. Node.js for the terminal remote.
- Your devices on the same network, each with a fixed address.

## 1. Describe the house

The public checkout includes a fictional example. Keep real inventory and host settings in a separate private site checkout. Deployment installs committed files: generate and privately commit `conf/`, `metrics/` and `deploy/` before installing. These outputs are ignored in the public checkout; keep your house configuration out of the public repository.

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

## 4. Pair your devices

Some devices need a one-time handshake: they show a code on screen, which you type into Remotes → Pair devices. On a compatible Sony TV, set IP control's Authentication to Normal and turn on Remote Start; the menu path varies by model. Pairing availability depends on the device's firmware, and thermite reports failures on its pairing page. A Sony TV can also be paired from a terminal:

```sh
thermite pair living_room_tv
```

## 5. Use it

- On a phone or a computer, open `http://<thermite's address>:8080/static/remote/home.html`.
- In a terminal, run `thermite remote`. Both start with Home, with Remotes, Security, Cameras and Site views.

## When something goes wrong

- `thermite health --production` lists every device that isn't online.
- `thermite rollback` puts the previously deployed version back; `thermite history` lists them.
- Every command explains itself: `thermite help <command>`. The [commands page](commands.html) has them all.
- `thermite version` shows which thermite and which openHAB you run, and `thermite completion bash` (or `zsh`, `fish`) gives your shell tab completion.
