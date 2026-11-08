# Device behaviour contract and room model

Proposal, 2026-11-07. Owner direction: Thermite is a platform for any house
and any device. A November 2026 cold review of 70b5836 found that the core
encodes one house's devices and room shapes, and that its state handling
breaks at device transitions. This note sets out the contract that replaces
those assumptions, and the order in which it lands.

The status of each stage and every open item is in [handoff.md](handoff.md).

## Problems this replaces

| Area | Today | Consequence |
|---|---|---|
| Device timing | One global settle window (4 re-reads, 8 s) and one 30 s route deadline | A slow device's accepted command reads as "not applied", so the next route trusts a stale reading (the projector Off race) |
| Transitions | Each integration folds warm-up, cooling and boot into ON/OFF | Commands sent mid-transition are refused or ignored, and never retried |
| Feedback | Each integration decides whether to post on poll or on change; the room policy assumes 30 s freshness; one failed poll sets an Item UNDEF | Locked rooms go "unknown" while nothing is wrong; push-only and feedback-less devices cannot be locked; a room on a flaky matrix output flickers between Off and unavailable |
| Report vocabulary | `allowed` lists only commandable values; anything else is dropped | A TV's own `tv` input, an app, or a warm-up state defeats a lock |
| Integration knowledge | `lifecycle_io.py` names every integration; Denon code sits in the generic HTTP actor; `plan.py` names pyatv | Each new device type edits the generic core |
| Room shape | A room is a receiver, then a display, plus special cases (split audio, one HDMI switch, zone sync) | Soundbars, a TV as its own receiver, cascaded matrices and two displays cannot be expressed; 4–5 layers each work out the wiring and disagree |
| Status | The lifecycle publishes point-in-time evidence; clients read only the Source Item | Refused, unconfirmed and stalled routes are invisible; "Off" shows while a display is on |

## Contract terms

Each integration declares its fields through one hook,
`fields(device) -> {item: Field}`. The routing executor, the planner, the
room policy and the clients read only these declarations. Generic code names
no integration.

| Term | Values | Meaning |
|---|---|---|
| `values` | list | What can be commanded |
| `reports` | raw → {value, transition} | Every value the device can report, normalized once. A value outside `values` is a definite mismatch, not unknown |
| `feedback` | `poll(interval)`, `push`, `none` | How readings arrive. Freshness derives from it: a poll is fresh for 2 × interval plus a margin; push is fresh until the connection drops and is re-read on reconnect; none is "assumed" |
| `settle` | {value: ms} | How long an accepted command may take to show. The planner keeps the write *pending* until it shows or the window ends |
| `transitions` | raw states + eta | Warm-up, cooling and boot. A transitional reading is neither verified nor a reason to write; the planner reads again later |
| `busy` | outcome + `retryAfterMs` | The device declined because it was busy; retried within the route's deadline |
| `power` | `absolute`, `toggle`, `wake-only` | Toggles and wake-only power go only through an actor that reads first |
| `independent` | bool | Whether one field's unknown outcome quarantines only that field (matrix outputs) or the whole device |
| `recovery` | detection + action | A failure the integration can recognise and undo without power. Example: a hung HDFury VRROOM resets every HTTP request, but `set reboot` on its telnet API brings it back in about 15 s |

A house may override timing per device (for example `settle` for a slower
projector model). Integrations ship the defaults.

## Shared control (owner decision, 2026-11-07)

Thermite never assumes it has exclusive control of a device. Any device may
be changed at any time by another controller (a Control4 system, a
universal remote, CEC, a person at the device), and no house file needs to
name that controller. Thermite learns every device's state by polling or
from its reports, and reports it accurately whoever changed it.

- A change Thermite did not make is an observation. It updates the room's
  shown state and never counts as an error. When a route rests on the
  property, it triggers a re-read.
- A locked room wins. Whatever changed a locked room's device, the room
  policy repairs it back to the lock's target, with per-device back-off.
- In an unlocked room, Thermite adopts what it observes and does not undo
  it.

## Planner and executor semantics

- Outcomes: `unsent{transient}`, `refused`, `busy{retryAfterMs}`,
  `applied{sample?}`, `unknown`. Transient and busy outcomes are retried
  within the deadline. One table of reason codes is shared by the planner,
  routing.js and the status Items.
- An applied write without a matching readback becomes
  `pending{value, until}`. It is journaled and survives reloads.
- An Item observation that differs from the planner's evidence triggers one
  bounded re-read. It never strands a route.
- A route's deadline covers its devices' declared settle and transition
  times.
- Each route publishes its outcome: `running`, `done`, `failed{reasons}` or
  `yielded`, with what it is waiting for. The policy worker and clients read
  this. The worker does not repair a room while its route is running, and the
  planner recognises the worker's repairs as enforcement, not a takeover.

## Room model

A room is an ordered list of typed hops from source to screen and speakers:
matrix output, switch, AVR zone, display, soundbar. Each hop names its power
and input fields. One function derives, from that list:

- the power, input and after steps;
- the planner's requirements and releases;
- the room policy's fields;
- the readback that turns device state into the room's shown state: a
  source, `off`, `partial` (some devices on, path incomplete) or `unknown`;
- the routing check's expectations, so the check no longer re-implements
  routing in Python.

Zone sync, split audio and HDMI switches become hop types and relations, not
special cases. Execution under the planner is chosen per field from its
declarations, not by a house-wide flag.

## Order of work

| Stage | Content | Status |
|---|---|---|
| 1 | Fixes for this house: synced EDID; post every poll; out-of-set values are mismatches; quarantine reason table; observation re-read; AVPro refusals; projector settle and transitions (the first `settle`/`transitions` terms) | Done |
| 2a | The `fields(device)` hook; move Denon into its integration; remove the integration chain from `lifecycle_io.py` and pyatv from `plan.py` | Done |
| 2b | `busy`/`transient` outcomes and retries; deadlines from declarations; route outcome published; the planner's own quarantine and every quarantine reason exported as metrics; a manual command to a settling property is held, not dropped, until the planner reads it settled (legacy proposals get their own executor slot that planner reads never replace; routing takes a hold that blocks route writes but allows reads; bounded by the settle cap and journaled) | |
| 2c | `feedback` and freshness drive the room policy; `reports` normalization; `power` kinds (SHIELD wake); `recovery`; availability from `feedback`, so a script-driven device that stops answering fails its check, not only goes Unknown (today an AVPro matrix has no check at all, and an Apple TV or projector only a power check). The VRROOM is the first integration on the full contract: control over telnet (push with `ipinterrupt`, acknowledged `set insel`, `set reboot` as recovery), HTTP kept only for the diagnostics telnet lacks (+5 V, hot-plug, eARC) | |
| 3a | The hop room model and its single derivation; the routing check uses it | |
| 3b | Policy fence and command origin; clients show route outcome, `partial`, and a live-connection watchdog | |

Each stage ships on its own: tested and released, with no compatibility layer
for configuration it replaces.
