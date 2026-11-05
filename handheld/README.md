# Handheld remote

A third client for the house remote: a battery ESP32-S3 handheld with a
touch screen and keys. It is a thin client. A computer near the house runs
the bridge, `thermite handheld serve`, which runs one remote session per
handheld on the same house model, layouts, backend and gateway the web and
terminal remotes use, and sends each handheld the screen its session shows.
The handheld draws that screen and reports its presses.

```text
handheld ──ESP-NOW──▶ base station ──USB serial──▶ bridge ──HTTPS──▶ gateway
```

The base station is a bare ESP32-S3 on the bridge's USB port. It relays
sealed radio frames as text lines and holds no keys. Each handheld has its
own key, made when it is enrolled; only the bridge's state file and the
handheld keep it.

| File | What it is |
| --- | --- |
| [PROTOCOL.md](PROTOCOL.md) | The protocol: frames, sessions, messages, the screen and audio models, the base's and the console's lines |
| [link.js](link.js), [vectors.json](vectors.json) | Frames and sessions, and the byte-exact frames the firmware must reproduce |
| [bridge.js](bridge.js) | Sessions to screens, operations to session calls: acks, dedupe, sleep |
| [screen.js](screen.js) | The screen and audio models, placed as the terminal remote places its keys |
| [base.js](base.js) | The base's lines over its tty or a simulator's Unix socket, reconnecting |
| [state.js](state.js) | The private state file: remotes, keys, each remote's last room, the base |
| [main.js](main.js) | `thermite handheld serve`'s program |
| [firmware/](firmware/README.md) | The handheld's and the base's firmware |

## Commands

```sh
thermite handheld serve [--url URL] [--ca-file FILE] [--mock] [--port DEVICE | --listen PATH] [--state FILE]
thermite handheld enroll --name NAME [--port DEVICE] [--base-port DEVICE]
thermite handheld enroll --simulator --name NAME [--key-file FILE] [--mac MAC]
thermite handheld list [--json]
thermite handheld forget NAME [--port DEVICE]
```

`serve` finds the gateway and trusts it as `thermite tui` does: without
`--url` it opens `https://thermite:13601`. Guests control unlocked rooms
without a login; after `thermite access login` for the same address, that
account is used. `--mock` uses the remote page's mock house, which sends
nothing. Without `--port` it uses the Espressif USB port in
`/dev/serial/by-id` that answers `thermite hello` as a base, and opens it
again when the base is unplugged and plugged back in. It logs briefly on
stderr; `-v` adds what the mock would have sent.

The state file is `~/.local/state/thermite/handheld/state.json` (under
`$XDG_STATE_HOME`), mode 0600 in a 0700 directory. It holds each remote's
name, MAC, board and key, its last room and battery, and the base's MAC and
channel as `serve` last saw them. No command prints a key. `enroll` and
`forget` may run while `serve` does: it picks up the change within a second.

## Develop with the simulator

No hardware is needed. In one terminal:

```sh
thermite handheld enroll --simulator --name sim
thermite handheld serve --mock --listen "$XDG_RUNTIME_DIR/thermite-base.sock"
```

`enroll --simulator` prints where it wrote the remote's key (64 hex digits,
mode 0600). Start the simulator (see [firmware/README.md](firmware/README.md))
with that key file and socket: it connects as a base, says
`thermite base 1 sim <mac> 1`, and plays the remote. Drop `--mock` to drive
a real house through its gateway.

## With hardware

1. Flash the base and the handheld ([firmware/README.md](firmware/README.md)).
2. Plug the base in and run `thermite handheld serve` once: it records the
   base's MAC and channel. Leave it running.
3. Connect the handheld by USB and run
   `thermite handheld enroll --name lounge`. It gives the handheld a fresh
   key, the base's MAC and channel. Unplug it; `serve` already knows it.
4. `thermite handheld list` shows each remote's board, battery and room.

To retire a remote, `thermite handheld forget lounge --port DEVICE` erases
its enrolment on the device too; without `--port` only the bridge forgets it.

## How the screen is made

For each remote, the bridge reads its session as the terminal remote draws
it: the room's `view()`, `lockNotice()` and `roomLocked()`, every room's LED
and short line, the bubbles with `bubbleState()` and `bubbleLock()`, and the
layout of the remote on show (`remoteOf(activeTarget())`). The layout becomes
a D-pad's four corners; rows in the terminal's order (top, playback when it
has more than three keys or there is no D-pad, then the primary key groups,
then the others); up to three playback keys beside Mute; and the CH rocker.
`buttons` carries the spec of every key named, the D-pad's own keys and the
volume keys. The screen's `id` is the first 16 hex digits of the SHA-256 of
the other fields' JSON, so a restarted bridge knows a screen a remote
already shows. The bridge recomputes a remote's screen once per event-loop
turn in which its session changed, and sends it only when it differs from
what the remote said it shows (`have`).

## Tests

`make check` runs them; `tests/test_handheld.py` drives
`tests/handheld_bridge_harness.js` (the bridge on the page's mock backend
with simulated remotes) and `tests/handheld_sim_harness.js` (a simulated
base for `serve --listen`), and enrolls fake remotes on pseudo-terminals.
The firmware's own host tests, its fixture renders and the simulator build
with CMake: see [firmware/README.md](firmware/README.md).
