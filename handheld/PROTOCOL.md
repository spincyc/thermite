# Handheld remote protocol, version 1

Normative for `handheld/link.js`, the bridge, and the firmware under
`handheld/firmware/`. [vectors.json](vectors.json) holds byte-exact frames
both implementations must reproduce. Background and hardware choices are in
[design/handheld-remote.md](../design/handheld-remote.md).

```text
remote ──ESP-NOW──▶ base ──USB serial lines──▶ bridge ──HTTPS──▶ gateway
       ◀─────────        ◀──────────────────          ◀──────
         sealed frames      the same frames,            RemoteSession on the
         (AES-256-CCM)      base64 in text lines        shared RemoteBackend
```

The remote is a thin client. The bridge runs one `RemoteSession`
(`web/remote/session.js`) per enrolled remote, on the house model, layouts and
backend the web and terminal remotes use, and sends each remote a screen model
derived from it. The remote draws that model and reports input. The base relays
frames without reading them; it holds no keys.

## Radio frames

Each ESP-NOW payload is one frame, at most 1470 bytes (ESP-NOW v2). All
integers are little-endian.

| Offset | Size | Field | Meaning |
| --- | --- | --- | --- |
| 0 | 1 | `version` | 1 |
| 1 | 1 | `type` | 1 HELLO, 2 SYNC, 3 DATA |
| 2 | 1 | `message` | DATA message number within the session, wrapping; 0 otherwise |
| 3 | 1 | `part` | High nibble: this part's index. Low nibble: part count − 1 |
| 4 | 8 | `session` | See [Sessions](#sessions) |
| 12 | 4 | `counter` | Strictly increasing per session and direction |
| 16 | n | ciphertext | AES-256-CCM of the part's plaintext |
| 16 + n | 16 | tag | CCM authentication tag |

- Key: the remote's 32-byte key, unique per remote, created at enrolment.
- Nonce, 13 bytes: direction byte (`0x01` remote to bridge, `0x02` bridge
  to remote), then `session` (8 bytes), then `counter` (4 bytes), as encoded
  in the header.
- Additional authenticated data: the 16 header bytes.
- A part carries at most 1438 bytes of plaintext. A message has at most 16
  parts. Receivers reject any message over 8192 bytes.

A receiver drops, silently, a frame that is shorter than 33 bytes, has an
unknown version or type, fails authentication, or whose counter is not
greater than the last one accepted for that session and direction.

## Sessions

A session is a random, nonzero 64-bit number the bridge chooses. Counters start
at 1 in each direction for each session, so no nonce repeats under a key while
session numbers are unique.

1. **HELLO**, remote to bridge, one part. `session` is a fresh random
   number of the remote's (the *hello nonce*), `counter` is 0. The plaintext
   is a status object (below).
2. **SYNC**, bridge to remote, one part. `session` is the new session,
   `counter` is 0. The plaintext is `{"hello":"<hello nonce, 16 lower-case hex
   digits>"}`, the hello nonce printed as a 16-digit hexadecimal number. The remote adopts the session only if it echoes the HELLO it
   is waiting on; it then resets both counters.
3. **DATA** in either direction, under the adopted session, counters from 1.

The bridge forgets sessions when it restarts. The remote keeps its session
in memory that survives deep sleep (RTC memory), so waking needs no handshake.
A remote whose operation is not acknowledged in time sends HELLO and repeats
the operation in the new session (see [Operations](#operations)).

Replay protection needs no durable writes. Old frames carry an old session or
a counter already passed. A replayed HELLO only makes the bridge start a
session the real remote never adopts. That costs one retry.

## Messages

Every DATA message is one UTF-8 JSON object. Field names follow the session's
own names (`inUse`, `short`, `led`).

### Remote to bridge

Every message carries `op`, an unsigned 32-bit operation number the remote
increments for each new operation and keeps across deep sleep. Any message may
also carry status fields.

| Field | Value | Bridge action |
| --- | --- | --- |
| `key` | Array of button ids, first choice first | `session.pressKey(key)` |
| `bubble` | Bubble id | `session.tapBubble(...)` for that bubble |
| `room` | Room id | `session.selectRoom(room)` |
| `allOff` | `true` | `session.allOff()`: the first press arms it, the second sends |
| `input` | Input value | `session.chooseInput(input)` |
| `sleep` | `true` | Stop sending until the remote speaks again |
| *(none)* | | A wake or status report |

Status fields: `have` (the screen `id` the remote shows, or `""`),
`battery` (0–100 or `null`), `charging` (boolean), `board`, `fw`, and `w`,
`h` (display size in pixels). HELLO always carries all of them. After that a
remote sends `have` when it wakes, and `battery` and `charging` when they
change; operations that follow a HELLO do not repeat what it said.

### Operations

The bridge answers every operation with `ack`, `sleep` included. It applies
each operation number at most once per remote while it runs. A repeat with an
applied number is acknowledged again but not applied. An operation that names
nothing the session offers (an unknown room, bubble or input value, a key list
longer than 8) is acknowledged and not applied. Until the bridge has read the
house, it acknowledges nothing; the remote's retries carry the operation
across that wait.

The remote resends an unacknowledged operation after 250 ms, with the same
`op`. After two silent attempts it sends HELLO, then resends once in the new
session, then gives up and shows *Not connected*.

### Bridge to remote

One message may hold any of these fields:

| Field | Value |
| --- | --- |
| `ack` | The `op` acknowledged |
| `screen` | The whole screen (below); replaces the previous one |
| `audio` | The volume line (below); replaces the previous one |
| `toast` | `{"title", "body", "tone"}`; tone is `""`, `"muted"` or `"bad"` |
| `inputs` | `{"title", "options": [{"value", "label", "inUse"}]}`: show the input list once |

After any remote message whose `have` differs from the current screen `id`, the
bridge sends `screen` and `audio`. Otherwise it sends each when it changes.
It sends nothing to a remote that said `sleep` or whose last frame was not
delivered (`sent … fail`, or no `sent` within 5 seconds), until that remote
speaks again; it then sends `screen` and `audio` unless `have` matches.
A message over 8192 bytes is split so that `screen` travels alone.

### Screen

```json
{
  "id": "1c2a6b0e9d3f4a57",
  "room": "west", "label": "Example West",
  "rooms": [{"id": "west", "label": "Example West", "led": "ok", "short": "Apple TV"}],
  "led": "ok", "short": "Apple TV", "problem": "", "notice": "", "locked": false,
  "bubbles": [{"id": "off", "label": "Off", "state": "idle", "lock": false, "inert": false}],
  "selected": "apple_tv",
  "hidden": false,
  "remote": {"label": "Siri Remote", "note": ""},
  "pad": {"corners": ["power", null, "back", "tv"]},
  "rows": [{"label": "", "keys": ["input"], "columns": 0}],
  "beside": ["play_pause"],
  "channel": false,
  "buttons": {"power": {"label": "Power", "icon": "power"}, "up": {"label": "Up", "icon": "up", "repeat": true}},
  "allOff": false
}
```

- `id`: the bridge's digest of the other fields. Remotes compare it and
  echo it as `have`; they never compute it.
- `rooms`: every room, with `session.view()`'s `led` and `short`.
- `led`, `short`, `problem`: `session.view()` for this room. `notice` is
  `session.lockNotice()`; `locked` is `session.roomLocked()`.
- `bubbles`: `session.bubbles()` in order. `state` is `bubbleState()`;
  `lock` and `inert` are `bubbleLock()`. `selected` is `selectedBubble()`.
- `hidden`: `session.remoteHidden()`. When true, `remote` and `pad` are
  `null`, `rows` and `beside` are empty and `channel` is false.
- `remote`: the layout on show (`remoteOf(target)`): its `label`, and its
  `note` with `{device}` replaced, else `""`. `null` when nothing is on show.
- `pad`: present when the layout has a D-pad. `corners` has four ids or
  `null`s: top left, top right, bottom left, bottom right.
- `rows`: in order: `top`; `playback` when it has more than three keys or
  there is no D-pad; then `keyGroups` (or one unlabelled row of `keys`). `columns`
  is the group's `columns`, or 0 for automatic.
- `beside`: the playback keys that sit beside Mute instead: up to three, when
  the layout has a D-pad. The terminal remote places them the same way.
- `channel`: the layout has a CH rocker.
- `buttons`: the `REMOTE_LAYOUTS` spec (`label`, `icon`, `text`, `repeat`)
  of every id referenced above, of `up`, `down`, `left`, `right` and `ok`
  when `pad` is present, of `volume_up`, `volume_down` and `mute`, and of
  `channel_up` and `channel_down` when `channel` is true. An absent `icon` or
  `text` is none and an absent `repeat` is false.
- `allOff`: All off is armed and waits for its second press.

### Audio

```json
{"short": "Receiver", "value": 35, "min": 0, "max": 98, "step": 1,
 "muted": false, "disabled": false, "text": "35", "caption": "Receiver · 35"}
```

The fields of the same names from `session.audioView()`. `muted` is `null`
when the device has no mute. While the screen is `hidden`, `audio` is `null`.

## Base station lines

The base speaks newline-terminated ASCII lines over its native USB
serial port (USB-Serial-JTAG). A receiver ignores a carriage return before the
newline. Lines that do not start with `thermite ` are logs; both sides ignore
them. Base64 is standard with padding. MACs are
lower-case and colon-separated.

| Direction | Line | Meaning |
| --- | --- | --- |
| host to base | `thermite hello` | Identify |
| base to host | `thermite base 1 <fw> <mac> <channel>` | Protocol 1, firmware version, base MAC, Wi-Fi channel. Sent at boot and answering `hello` or `channel` |
| host to base | `thermite channel <1-13>` | Use and keep this channel |
| host to base | `thermite send <id> <mac> <base64>` | Send one frame. `id` is a decimal 1–65535 |
| base to host | `thermite sent <id> ok` or `thermite sent <id> fail` | Whether the remote's radio acknowledged it |
| base to host | `thermite rx <mac> <rssi> <base64>` | A frame received |
| base to host | `thermite error <words>` | A malformed or refused line |

Sends are queued in order. The base answers each `send` with exactly one
`sent`.

## Remote console lines

A remote connected by USB answers the same `thermite` lines on its console.
Other console output continues.

| Direction | Line | Meaning |
| --- | --- | --- |
| host to remote | `thermite hello` | Identify |
| remote to host | `thermite remote 1 <fw> <mac> <board> <enrolled 0 or 1>` | |
| host to remote | `thermite enroll <key, 64 hex digits> <base mac> <channel>` | Store and use |
| remote to host | `thermite enrolled` | Stored |
| host to remote | `thermite forget` | Erase the enrolment |
| remote to host | `thermite forgotten` | Erased |
| remote to host | `thermite error <words>` | Refused |

The key never leaves the bridge's state file except over this cable.

## Radio channel

The base uses one 2.4 GHz channel, 1 by default. A remote first tries the
channel it was enrolled with. When a HELLO gets no SYNC there, it tries
channels 1 to 13 in turn and keeps the one that answers.

## Hard keys

Boards with keys send these `key` arrays.

| Key | `key` |
| --- | --- |
| Up, Down, Left, Right, OK, Back | `["up"]`, `["down"]`, `["left"]`, `["right"]`, `["ok"]`, `["back"]` |
| Volume +, Volume −, Mute | `["volume_up"]`, `["volume_down"]`, `["mute"]` |
| Channel +, Channel − | `["channel_up"]`, `["channel_down"]` |
| Play | `["play_pause", "play"]` |
| Stop | `["stop"]` |
| Rewind, Forward | `["rewind", "previous"]`, `["fast_forward", "next"]` |
| Power | `["power"]` |
| Home | `["home", "menu"]` |
| Settings | `["settings", "menu"]` |
| Info | `["info", "guide"]` |
| Source | `["input"]` |
| Red, Green, Yellow, Blue | `["red"]`, `["green"]`, `["yellow"]`, `["blue"]` |

A key whose button `repeat`s presses again 420 ms after it went down, then
every 150 ms, each press a new operation, as the web remote's held keys do. A
key with no entry (OMOTE's Record) does nothing.
