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

## 4. Set up site access

The web and terminal clients share site accounts. Admins manage users, roles and room schedules; ordinary users control unlocked rooms, and viewers have read-only access. A scheduled lock has an expected room state that thermite reconciles while the lock is active.

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
