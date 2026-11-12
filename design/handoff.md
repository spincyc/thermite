# Thermite work handoff

Updated 2026-11-08. CI is green on Python 3.11–3.14. The plan for what
follows is [device-contract.md](device-contract.md): stages 1 and 2a are
done; 2b, 2c and 3 are not started. House-specific work (devices to check in person, owner
settings, live tests) is in the private site's handoff, not here.

## Owner directions

- Thermite is a platform for every house and device. Fix this house's
  problems in ways that generalise; don't fit the core to one house.
- No compatibility layers: there is one install, so remove what a change
  makes meaningless.
- Shared control is the default. Other controllers (a Control4 system, a
  remote, CEC, a person) may change any device at any time, and no house
  file names them. Thermite polls, reports what it reads, and adopts it; a
  locked room wins and is put back.
- Each stage ships on its own, tested, with `make check` run on the exact
  commit before it is published.

## Shipped in this round (70b5836..fca2412)

- Routing planner: per-device settle declarations (`SETTLE`), a journaled
  `pending` wait that also survives reloads, overflows and descriptor
  changes, transitional samples (ADCP startup and cooling), a re-read when
  an Item contradicts what a route rests on, and one quarantine reason table.
  Fixes the projector Off race.
- Devices: the AVPro, Sony TV and ADCP integrations post every reading at
  each poll; an AVPro `CMD ERR` is refused, not applied.
- Room policy: a value no route uses (a TV's home screen) is a mismatch and
  is repaired; a synced follower writes no EDID.
- Metrics: per-zone checks for shared receivers; Unavailable rows; sources
  named as the remotes name them; the 12 fixes from the metrics review
  (zone ids everywhere, `thermite_zone_info` and `thermite_room_info`,
  pre-zeroed counters, the house's own deadline, new alerts
  ThermitePolicyUnenforced, ThermiteRoutingTelemetryUnavailable and
  ThermiteGatewayFeedDown); a house without an HDMI matrix generates.
- HEOS: one bridge per house, not one per receiver; `heos_bridge: true`
  picks the receiver it connects through.
- The handheld bridge's `--listen` socket is created owner-only, closing
  a race its test caught on CI.

## Stage 2a, done

- Each integration the routing executor drives declares its fields through
  `fields(device)`: `kind`, `values`, `thing`/`channel`, `ack`, `zone`/
  `point`, `settle` and `manual`. `lifecycle_io.py`, `lifecycle_io.js`,
  `lifecycle.py`, `routing.js` and `oh_room_policy/plan.py` name no
  integration, and a test keeps it so.
- The Denon/Marantz routing actor is the integration's own rule,
  `denonmarantz_http.js`; GAME→GAME1 exists once, in `INPUT_ALIASES`.
- The script integrations share `lib/oh_integrations/_device.js`: posting
  readings at every poll and their own state on change, down/up logging,
  backoff, kept connections, the command rule and routing samples.
- Left as they are, worth doing when the code is next touched:
  - `manual` is per device but declared on every field;
  - the actor still receives `values` under the key `allowed`;
  - `httpActor`'s `config()` repeats `http.thing()`, and
    `denonmarantz_http.js` repeats `get()`;
  - `configure()` in `lifecycle_io.py` looks a path up only to raise
    "adapter is absent"; raise directly;
  - androidtv_googletv, and the 6-line `drop()` in bk808_cec and
    globalcache_ir, are not on the shared helper.
- `tests/lifecycle_io/NativeLifecycleTest.java`, the only test that runs the
  Denon rule in GraalJS with the real XML parser, needs `THERMITE_JAVA`,
  `THERMITE_ECJ`, `THERMITE_RUNTIME` and `THERMITE_GRAAL` and was not run.
  The owner's-house controls check drove it end to end instead.

## Stage 2b: outcomes, retries, deadlines

Done:
- `busy{retryAfterMs}` and `unsent{transient}` outcomes, retried while the
  route is current and before its deadline. The Sony projector answers busy
  for an Off while it warms up, an On while it cools and an input while it
  warms up; a Sony TV for an input refused while its content API boots
  (30 s after it reads on). Busy retries skip the read before the write.
- Each route's deadline is 30 s plus the longest chain of declared settle
  times its writes may take, at most 300 s more. It leaves out the 61 s
  quarantine recovery on purpose: covering it would slow every failure.
- `independent` fields: a BK-808 output or EDID input, a UKM-404 port or an
  AVPro output quarantines alone on an unknown outcome, and a manual command
  to one holds only that port.
- A replacement actor ends the flight a dead one left; an old actor that
  finishes late changes nothing shared. One executor event that throws no
  longer stalls the others.

Not verified on hardware: the TV's 30 s boot window and its boot error code
(the rig uses 40005). No integration emits a transient `unsent` yet.

Still open:
- Publish each route's outcome (`running`, `done`, `failed{reasons}`,
  `yielded`). Today the status is point-in-time evidence: an incomplete
  route always shows `pathReady:false` plus a deadline failure, and no
  client reads it.
- Hold a manual command to a settling property until the planner reads it
  settled. Today it goes out at once; only its readback is ignored (see
  the design note's 2b row for the shape).
- Export the planner's own slot quarantine, with every reason, as metrics.
- routing.js's histogram buckets (1–60 s) are fixed; derive them from the
  deadline, which can now reach 330 s.
- Low: preserve takeover; `ownerRelease` reads a yielded room; a manual
  command of the same value replaces a planner write and shows
  not-admitted.

## Stage 2c: feedback, freshness, power kinds, recovery

- `feedback` (poll, push, none) and freshness drive the room policy. The
  denonmarantz binding reports only on change, so a locked room with a
  Marantz receiver reads Unknown 30 s after each change. Its lock is still
  held, and its reconcile age grows.
- Two writers for the receiver Items: the actor posts, and the binding
  notifies only when its cached value changes. The Item can show a stale
  input indefinitely.
- One failed poll sets a matrix output UNDEF, so a room on a flaky output
  flickers between Off and Unknown.
- Availability: a script-driven device that stops answering should fail its
  check. Today the AVPro matrix has no check at all, and the Apple TVs and
  the projector have only a power check, which goes Unknown.
- `reports` normalisation: the vocabulary of everything a device can
  report, including transitional values. A Sony TV briefly showing `tv`
  while waking now gets a backed-off repair.
- `power` kinds: the SHIELD wake is a KEY_POWER toggle outside any actor,
  so two locked rooms repairing it can toggle it off again.
- The HDFury VRROOM is controlled over telnet (`vrroom_telnet`), with push
  (`ipinterrupt`) and acknowledged `set insel`; `vrroom_http` only reads
  diagnostics, once a minute. Its replies' wording comes from strings in
  the firmware, not a recorded session: check the first live switches.
- `recovery`: `<id>_reboot` sends `set reboot`, but only when commanded.
  For the VRROOM the signal is its `vrroom_http` Thing OFFLINE with a
  connection reset on two 60 s polls in a row while the telnet input
  reading stays fresh (an unpowered switch fails both). Reboot at most once
  an hour and expect the Thing back within about 90 s.
- Low:
  - `sony_ip_control` registers WOL but never sends it;
  - the pyatv lifecycle budget includes the reconnect;
  - IR and CEC cover one Blu-ray model.

## Stage 3a: the room as typed hops

Model a room as typed hops (see the design note) and derive power, inputs,
readback, planner requirements and policy fields from that list with one
function. That replaces these, all found in the cold review:

- the layers disagree on which stages a route powers:
  - `power()` powers every stage;
  - `lifecycle.py` and `plan.py` power only the stages whose input is set;
  - `shown()` and `hold()` look at every stage;
- the `sleep_sources` ordering the house file promises is not implemented:
  the source sleeps in parallel with the TV's Off;
- with the planner on, `moved()` sends no unmanaged zone-input steps;
- suspected: a split audio/video room copies the TV's EDID for the receiver;
- the routing check (`lib/oh_testing/routing.py`) re-implements `shown()`
  and `syncs()` in Python, and assumes an HDMI matrix in about ten places;
- `lifecycle_enabled` (`usb_unused: park` or `idle: standby` anywhere)
  switches the whole house's execution model; choose it per field;
- minor:
  - `hold_ms` is house-wide;
  - the diagnostics regex drops SAT/CBL;
  - a single-number `usb_host` applies to every USB matrix.

## Stage 3b: policy fence and clients

- The policy worker repairs during a running route, with no route-in-flight
  fence, and the planner treats its repairs as manual takeovers.
- A lock toggle without `desired` captures a stale Source while another
  client's route is settling.
- `shown()` reports Off whenever any stage's power is OFF, so a failed
  choice, or a display that turned itself on, reads as Off. Show `partial`.
- Known and left unfixed in the clients: a newer `thermite tui` or handheld
  bridge against an older gateway, whose heartbeat is a comment, reconnects
  every 15 s (one install ships both, so no compatibility); and a command's
  15 s deadline runs while iOS suspends the app, so it can report
  unconfirmed on resume (its message says it may still take effect).

## Metrics left over

- The gateway exports `thermite_gateway_upstream_connects_total` and
  `thermite_gateway_upstream_resyncs_total` (`Hub.COUNTERS` in
  `lib/oh_access/metrics.py`, from `oh_identity.hub`), which nothing reads:
  opening the runtime feed is routine (the first remote page opens it) and
  a resync follows every connect; Switching & remotes shows the feed's
  failures. Stop exporting both, then drop them from `UNREAD` in
  `tests/test_metrics.py`.
- The notification adapter's `thermite_notification_delivered_total`,
  `_errors_total` and `_last_heartbeat_timestamp_seconds` are read nowhere.
  Alertmanager already counts a failed delivery to the adapter
  (ThermiteMonitoringBroken), and the heartbeat's age is its own series.
  `test_every_exporter_family_is_read` does not cover them yet.

## To verify on hardware

- The Sony VW915ES's real startup and cooling times, against the declared
  settle of 60 s on and 30 s off. Also whether ADCP reports other power
  statuses (for example `saving_*`), which neither table lists.
- What the projector does with a power Off while an accepted On is still
  pending.
- A synced follower reclaiming the source's EDID as its sync ends, just
  before the leader switches.
- A trade-off to watch: if a write the planner lost track of never applied,
  a later Off waits out its settle (up to 60 s) before it is sent.
- Not fuzzed: two rooms sharing the projector, with holds and manual
  commands.
- `thermite test routing` needs Java; run it on a host that has it.

## Upstream

- openHAB HEOS binding: thermite ships a patched 5.2.3 binding for the
  reconnect leak. The upstream fix for the reconnect loop is
  [openhab-addons#22234](https://github.com/openhab/openhab-addons/pull/22234),
  "[heos] Stop resetting a quiet event stream". Once a release carries it
  and #21781, the patched binding can go.
- Thermite used to render one HEOS bridge per receiver, against the
  binding's guidance that one bridge serves the whole HEOS network; it now
  renders one per house. The upstream reconnect-loop fix is still needed
  either way, because the loop hits any HEOS system that is idle for two
  hours.

## Development environment

- System `python3` lacks `prometheus_client` and Django. Run `make check
  PYTHON=<venv>` and the identity tests with a virtualenv that has both.
  The identity state directory must be created with mode 0700.
- Under a deep worktree the Unix socket path of the two
  `tests.test_handheld.Serve` tests is too long. Run `make check` with
  `TMPDIR` set to a short directory, such as one from `mktemp -d /tmp/t.XXXX`.
- `thermite test routing` and `controls` run on the owner's host beside the
  live runtime. Under load, routing can leave a port unconfirmed at a round
  check (or quarantine a matrix whose write timed out), and controls can
  see a Sonos press take about 1 s against its 0.5 s allowance. Unchanged
  `main` fails the same way at such times; compare with a run of `main`
  before blaming a change.
- Reviews and fixes ran as lanes in worktrees under `.scratch/`. Everything
  listed as shipped is on `main`.
