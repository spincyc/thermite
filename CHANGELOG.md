# Changelog

## 0.1.0.dev0 — Unreleased

The portable application remains a development build. Its version and verified
file manifest identify an exact release. Private sites pin that release;
`init`, explicit upgrades and retained releases preserve configuration and
rollback. The unified deployment workflow uses a separate committed private site.

- `thermite upgrade` installs the new version as one install transaction. It
  prepares packages, the boot test and the account, pulls every pinned image
  and shows the room it needs while Thermite keeps running; too little room
  refuses the upgrade before anything stops. Then the install engine, run as
  root from a copy pinned at begin, guards the boot, stops Thermite, sets the
  installed version aside under `/srv/thermite/.transaction/<id>/`, and the
  new version is switched in, checked by `thermite health --production
  --installation` and committed to `/etc/thermite/installed.json`. Any
  failure from begin on rolls back in the same run: the previous version runs
  again, the site's pin commit is undone, `thermite history` shows "rolled
  back", and the owner command is switched only after a commit. Exit statuses
  say how it ended: 0 committed, 1 refused, 2 not started (Thermite's files
  without a manifest, nothing to roll back or revert to, invalid
  arguments), 3 rolled back, 4 rolled back but unhealthy, 5 rollback stuck,
  130 interrupted. Any command that runs a transaction first finishes or
  rolls back an unfinished one, then stops and says so; `thermite status`
  reports one and names the command. The first transaction describes an
  installation from before the engine from `/etc/thermite/units.json`;
  Thermite's files without either manifest are refused, before anything
  stops, with the recovery steps (docs/installation.md#a-missing-manifest).
  A rollback stuck after the engine's commands were run by hand (`rollback
  --print`) is closed by rerunning the command, which checks what is back.
- The first upgrade runs under the owner command from before the install
  engine. When it stops or rolls back, that command says "A completed site
  release commit is retained so the same command can resume": unless the
  transaction is still open (exit status 5, or 130 after it began), the pin
  commit was undone instead, as the upgrade says just before, also when it
  stops before the transaction begins. After
  that first transaction rolls back, the old version's own `thermite
  install` refuses while `/srv/thermite/.transaction/` holds it: run
  `thermite upgrade` again, or remove that directory as root first
  (docs/installation.md#a-missing-manifest). The rollback says so.
- `thermite install` is the same install transaction, and so is `install
  --clean`: after you type the state path, it moves the state trees under
  `runtime/` aside whole and clears the deploy records and local credential
  copies inside the transaction, and deletes them only at the commit; a
  failure puts them back. A gateway whose callback relay was deferred
  activates it in one more transaction in the same run. A fresh install that
  rolls back leaves nothing installed.
- `thermite uninstall` is an install transaction of its own: the engine sets
  aside the units, links, native-port filter and AppArmor policy, leaves the
  state in place and records the installation as not installed, so a later
  install finds it. It used to keep the gateway's units and the filter and
  print how to remove them. `--purge` asks you to type the installation root,
  moves `runtime/` and `access/` aside, and its commit removes them, the
  manifest, the images and every transaction, including the site's
  `var/transaction/`.
- `thermite rollback [COMMIT]` installs an earlier site commit as an install
  transaction of that commit, with its boot test, units, images, filter and
  gateway, committed only once its health passes; the site's branch does not
  move. It used to redeploy the configuration alone. `--force` and `--state`
  are gone; a commit pinned by another application release is refused.
- New `thermite revert` puts back the version the last install or upgrade set
  aside, with its state as it was then, as its own transaction. It says that
  everything written since is lost and asks you to type the installation
  root. Inside the transaction the site's branch goes back to the commit
  before the upgrade's pin commit, so the previous version checks its health
  with its own application (a site still pinning the new release would have
  run the new release's health instead); a rollback puts the pin commit
  back. Once it commits, `~/.local/bin/thermite` selects the previous
  application, whichever run finishes the commit. Likewise a purge's commit
  removes the site's `var/transaction/` and every commit prunes the older
  gateway releases even when a rollback or the next run finishes it.
- Each commit keeps only the current and the previous gateway release,
  application environment and container images, and removes older ones.
  Gateway releases staged before the install engine are removed at the first
  commit, except the one the old version ran. `runtime/backups/` is never
  pruned.
- Upgrades no longer take tar backups: Thermite's state backups under
  `runtime/backups/` (`*-openhab-*.tar.gz`) and the gateway's identity tars
  under `access/backups/` are no longer written or read, and the printed
  restore advice is gone. Existing archives stay where they are; delete them
  when you no longer need them. The set-aside version is the way back.
- The gateway's served TLS certificate, key and callback map live at fixed
  paths in `access/inputs/`, and each run stages a release named after its
  transaction, `access/releases/<id>`. The gateway keeps no resume journal of
  its own.
- The installer ISO's first-boot guide asks for your equipment and time zone
  in `instance/host.yaml`, not a state directory, which the fixed
  `/srv/thermite` layout removed. The Arch image's login profile puts
  `~/.local/bin` first on PATH, so after `thermite install` switches
  `~/.local/bin/thermite`, `thermite` runs that verified application rather
  than the image's frozen `/usr/local/bin/thermite`, and install no longer
  warns about the launcher on PATH. Its live preflight checks exactly the
  packages archinstall will install, read from `arch-config.json` (it missed
  runc and util-linux), and the guide lists the same ones. The README
  `thermite init` writes into a new site says `thermite install` deploys the
  native services and HTTPS gateway in one run, not that accounts and TLS
  need separate commissioning.
- Every install transaction stops all of Thermite, `systemctl stop
  thermite.target thermite.slice`, before the engine sets anything aside,
  checks that nothing in the slice still runs, and then starts every service
  again. A rerun with nothing to change only checks health and stops
  nothing. This replaces restarting only the services whose units changed,
  and the listener record: nothing reads the site's `var/listeners.json` any
  more; delete it. openHAB's offline preparation no longer stops services
  itself, and Grafana stays stopped until monitoring starts. A failure after
  the stop rolls the transaction back; nothing else starts Thermite again,
  and the native-port boundary keeps no `var/native-boundary/previous.nft`
  journal of its own any more (delete it). A test holds every unit
  Thermite installs to `PartOf=thermite.target`, `Slice=thermite.slice` and
  being wanted by the target alone; the native-port boundary, which only
  loads the firewall rules, stays outside both.
- The gateway's allowed hosts, listening address and timezone come from the
  host and `instance/host.yaml` on every run. An upgrade adopted them from the
  release it replaced, so a renamed or readdressed host kept its first names
  and a changed timezone never reached the gateway. When the host's name or
  LAN address no longer matches the served certificate, the run reissues it
  from the same private authority, and clients need no change.
- Sonos events keep reaching openHAB after an install or upgrade. jUPnP's
  callback proxy port, the relay's 13615, is now generated configuration:
  `conf/services/upnp.cfg` sets it for a house with Sonos devices, so
  openHAB starts with it. Commissioning set it through REST while openHAB
  ran, which restarted jUPnP, and openHAB then answered every forwarded
  NOTIFY with HTTP 501 until it was restarted. Commissioning now only reads
  the port back and fails, naming the file, if openHAB does not use it.
  The live sandbox, which has no relay, leaves the file out.
- The plain-HTTP Guest port is 13600, the HTTPS application 13601, and
  every other listener follows them, one port up from before: native
  HTTP/HTTPS 13602/13603, Grafana 13611, the Sonos callback relay 13615
  (docs/ports.md). A site's `instance/host.yaml`
  sets `http_port: 13602` and `https_port: 13603` before upgrading. When any
  listener port changes, install and upgrade stop all of Thermite before
  installing the new units, so none starts on a port another still holds. The
  managed firewall's rule file is recognized by the ports it lists, so one
  an earlier allocation wrote is replaced, and an adopted Sonos callback map
  keeps its verified routes on the new callback port and native upstream.
  Bookmarks, home-screen apps and terminals move to https://HOST:13601.
- An upgrade adopts a gateway whose unit repeats a list directive, as
  every unit staged since the gateway wrote its metrics files does (two
  `ReadWritePaths=` lines); it refused such a unit as ambiguous. Only
  `User`, `Group` and `WorkingDirectory`, which identify the release, must
  appear once.

- `thermite handheld` serves battery handheld remotes: ESP32-S3 boards on
  their own ESP-NOW link to a USB base station, each a thin client of a
  remote session the bridge runs on the house model, layouts and backend the
  web and terminal remotes use. `serve` runs the bridge (`--mock`, and
  `--listen` for the desktop simulator), `enroll` gives a remote its own key
  over USB, `list` and `forget` manage them. Frames are AES-256-CCM sealed
  end to end; the base holds no keys (handheld/PROTOCOL.md). The firmware for
  the Waveshare ESP32-S3-Touch-LCD-2.8, M5Stack StickS3, OMOTE Rev5 and
  OpenRemote Rev6, the base's, and the simulator are in handheld/firmware,
  outside releases; none has yet run on hardware. A `qemu` board runs the
  real firmware in Espressif's QEMU against the bridge, enrolled and driven
  over its console (tests/test_handheld_qemu.py, opt-in).
- A terminal remote's telemetry reports its kind, `tui`, as the gateway
  requires; it sent its own label (`tui/<host>`), and the gateway refused
  every report.

- `thermite test boot` reaches no device: its copy of conf/ addresses every
  device and the Apple TV helper at the limited broadcast address, which
  the kernel refuses before a packet leaves, gives every Sonos Thing a
  UDN no player has, and leaves UPnP no network. It still proves conf/
  loads and every Thing gets a handler; `--require-online`, which could no
  longer pass, is gone (`thermite health` checks devices). The sandbox
  still reads the matrices and the devices' HTTP APIs but holds no HEOS,
  Cast, Android TV remote or Sonos session, and runs without UPnP.

- A Denon or Marantz receiver is polled every 15 s instead of every 5 s,
  and its signal and volume-display readings every 30 s instead of 10 s;
  the receiver closes every HTTP connection, so each poll's requests were
  about 107 new connections a minute between two receivers, now about 35.
  A change made on its front panel or from Control4 shows within 15 s.
  The routing lifecycle still reads its power and input back right after
  switching them. A volume or mute it was sent is read back only at its
  next poll, up to 18 s later (its control's readBack), so the remote steps
  from and shows the value it sent for 19 s instead of for 3 s and 6 s.
  routing.js holds a choice's Source until the slowest receiver or display
  can have read it back (20 s with such a receiver, else 15 s; without the
  lifecycle that is the binding's poll), and the remote waits a second
  longer.
- A WiiM is polled every 30 s instead of every 10 s, each poll a new TLS
  connection; a command is still read back a second later. The metrics no
  longer probe its HTTPS port either.
- Monitoring readiness and `thermite health` no longer count a device port
  Prometheus has not probed yet since it started as down.

- The metrics probe each device's control ports every 2 minutes instead
  of every 30 s; each probe is a new connection to a device its integration
  already polls. They no longer probe an Android TV's remote port (6466) or
  a Cast receiver's control port (8009): each probe cost the device a TLS
  handshake it never got, and the Thing's status already shows it reachable.

- A Sony projector on ADCP gets one connection, kept open, instead of a new
  one every 15 s and another per command. Its state is read on it every
  15 s, well inside its 60 s connection time-out, and commands are sent on
  it. A connection it closed or lost while idle (its time-out, a standby, a
  power cycle) is replaced at once, and a first request it leaves
  unanswered while waking from low-power standby is asked again. A projector that stops
  answering, or closes the connection while answering, is UNDEF and
  reconnected after 15 and 30 s, then every minute; a command connects at
  once. Nothing else connects to it: the sandbox and the boot check no
  longer read it beside the runtime, and the metrics no longer probe its
  port. It answers one request at a time, across controllers too.

- An AVPro matrix gets one telnet connection, kept open as AVPro's own
  control drivers keep theirs, instead of a new one every 5 s. Its routing
  is read on it every 10 s and switches are sent on it. A matrix that stops
  answering, or closes the connection, is UNDEF and reconnected after 10,
  20 and 40 s, then every minute; a switch connects at once. Nothing else
  connects to it: the sandbox and the boot check no longer read it beside
  the runtime, and the metrics no longer probe its port. The matrix serves
  few telnet sessions, and Control4 may keep one.

- Volume is each audio device's own number on its own scale, from its minimum
  to the maximum the device itself allows, in its own steps and unit: 0-98 in
  0.5 steps on a Denon or Marantz receiver (or dB as its display is set),
  0-100 in 1s on a Sonos, WiiM or Sony TV, and the binding's 0-100 % on an
  Android TV. The web and terminal remotes show that number and step by it (a
  WiiM's keys now move 1, not 2); the gateway accepts exactly that scale and
  refuses anything else, and openHAB's profiles and rules drop an off-scale
  value instead of clamping it. Nothing is normalized across rooms or devices.
  The `volume_max` device key is removed: a house file that still sets it is
  refused as an unknown key; a limit set on the device itself is the device's.

- Remotes provide an administrator-only manual room lock that captures the
  current reported source or Off and persists until unlocked. Manual holds
  survive restarts, preserve schedules, and take precedence while active.
  Guests can use unlocked remotes without signing in in either authentication
  mode. Locked rooms keep the normal layout with a visible lock indication
  and disabled controls; management, camera snapshots and live video still
  require a signed-in account while authentication is on.

- Security-relevant change: with Guests able to control every unlocked room in
  both authentication modes, roles, Viewer, custom roles and per-account room
  grants restricted nothing and are removed. Accounts are either ordinary
  accounts, which add camera media while authentication is on, or
  administrators, who manage locks, schedules, users, pairing and the system.
  The upgrade keeps existing administrators and makes every other account an
  ordinary one; the roles API and role editor are gone.
- Unlocking a room always succeeds; a schedule that no longer validates is kept
  disabled, and unlocking into an active window applies it once unless the room
  already shows its source. One unreadable policy no longer stops enforcement in
  unrelated rooms, and `thermite access prune` removes policies for rooms no
  longer in the house.
- The gateway throttles sign-in per user and device pair and delays, rather than
  locks out, a username; room commands no longer evict the security audit;
  server and worker errors are logged; event streams are capped; responses carry
  a Content-Security-Policy, `Allow` and `Retry-After`; SQLite runs in WAL mode.
- The HTTPS gateway serves HTTP/2, so several open remote pages no longer
  starve each other. Over HTTP/1.1 a browser shares six connections per origin
  between its tabs, and two visible remotes' event streams held all six, so
  policy reads, commands and assets queued until controls paused. The gateway
  now runs on Hypercorn instead of Uvicorn, with the same certificate, 100
  requests in flight (each HTTP/2 stream counting alone), 5 s keep-alive and a
  15 s shutdown deadline; WebSocket handshakes are refused.
- The web and terminal remotes no longer pause controls while a room's lock
  status is unknown. Commands are sent unless the room is known to be locked,
  and the server still refuses them in a locked room. After a refusal the
  remote reads the lock again. If the lock can't be read, the remote shows
  "Room lock status unknown" instead of "Control is paused".
- The gateway caches its web files. Generated pages already name each script,
  style and icon with a hash of its contents; the gateway now serves those URLs
  as immutable for a year when the hash matches and otherwise asks the browser
  to revalidate. Shared files carry an ETag and answer unchanged requests with
  304, and static text is gzipped. Pages that need a signed-in principal, house
  data and administrative files are still never stored or compressed. The
  gateway no longer logs Django's synchronous-iterator warning for every file
  it serves.
- The HDMI and USB matrices, the VRROOM and the Dr. HDMI are polled every 10 s
  instead of every 2 s, with a 2 s timeout instead of 3 s. A switch made at a
  front panel or a vendor web page can take up to 10 s to show; choices made in
  Thermite are still read back about a second after they are sent. One poll no
  longer overlaps the next on the matrices' one-at-a-time web API, and a matrix
  that sends headers but no answer shows as offline rather than online with
  unknown state. These Things send about a fifth of the events they did.
- The room policy worker compiles each room's route for a choice once and
  reuses it, instead of compiling it again on every step and boundary check.
- An openHAB-only change repairs ownership of only openHAB's trees; a missing
  service-account receipt, account or state directory still repairs them all.
  openHAB's unit wants the native-port boundary instead of
  requiring it, so reapplying the boundary no longer restarts openHAB; openHAB
  still loads the same rules before every start and never starts without them.
  The metrics exporter and notifications run under an init (`catatonit`, now
  installed on Ubuntu) and stop on SIGTERM instead of being killed.
- openHAB's Java heap is capped at 1.5 GiB, and a heap that still runs out
  exits so systemd restarts openHAB; the sandbox uses the same options.
- Quieter logs: Grafana logs warnings, keeps its search-manifest watcher to
  errors and no longer loads the 13 bundled data source plugins Thermite does
  not use; ntfy logs warnings; `thermite.slice` drops the deprecated
  `CPUAccounting=` (cgroup v2 always accounts CPU). The thingHandler telemetry
  rule changes its observed time once per 15 s sample instead of twice, about
  5,800 fewer events.log lines a day.
- Commands are traceable. Every command from Remotes, the terminal, Guests,
  All off and the policy worker gets an 8-character ID and one journal line
  naming its origin, the client's own label, the account and credential, the
  request, room, item, value, outcome (the lock that refused it, or openHAB's
  HTTP status or exception class) and duration. openHAB records the ID as the
  command's event source, and the routing rule logs each device step and
  missed deadline under it. Audit rows also go to the journal, and
  `thermite access audit` reads them; requests that change something or fail,
  event streams, worker boundaries, repairs and phase changes are logged, and
  every response carries `X-Request-Id`. Addresses appear only as a keyed
  digest and pairing values never. Token sign-ins are audited as password
  sign-ins with their peer, lock and Security changes name their peer, a
  terminal's token is named after its host, and a denied sign-in records an
  existing account's ID instead of the typed name. openHAB's own log
  (openhab.log, with the pairing filter) also reaches the journal under
  `thermite-openhab.service`; events.log stays a file. Each deploy writes a
  start and an end line (`journalctl -t thermite-deploy`), and `thermite
  history --details` shows the paths it changed, what became of each reload
  and restart, the upgrade's state backup and the gateway release it
  activated, with the release it replaced and its identity backup. Every
  unit logs under its own name instead of `env` or `systemctl`. "Trace a
  command" in the installation guide lists the searches. The next install
  or upgrade reruns native preparation once to route the log, restarting
  openHAB.
- openHAB's logs leave its private userdata for `runtime/logs/`
  (`/srv/thermite/runtime/logs`): directory 2750 and its logs 0640, group
  `thermite`, so the group reads openhab.log, events.log and their rotations
  without sudo, and nothing else in `runtime/` gains access. Every Log4j2 file
  appender sets `filePermissions`, which also covers rotations, and the
  pairing filter still drops pairing codes on every logger. The next install
  or upgrade reruns native preparation once to apply it, restarting
  openHAB; logs written before it stay where they were (see the installation
  guide's host layout). Upgrade backups no longer include the logs.
- Live updates: each page holds one event stream, `/api/v1/events/remote`,
  `/home` or `/pair`, whose first message is a snapshot of what the page
  shows, so opening or reconnecting a page no longer reads every room, control
  and problem one request at a time. The gateway holds one openHAB
  subscription for all of them, wildcard by verb, and keeps each Item's
  latest value; Home and Topology follow only room sources. A slow client is
  resynchronized, never buffered without bound; while openHAB is unavailable
  pages say so and resync when it returns. `/rest/events` is gone from the
  gateway, and a browser or terminal may hold 8 streams (was 12) and the
  gateway 48 (was 64). The policy worker subscribes with one wildcard too.
- Monitoring answers "is the house OK?" first. House health, the new Grafana
  home page, shows six tiles (control, rooms ready, remotes, alerts, phone
  delivery, host), what needs attention now in plain words, every room's
  source through the day and switching since yesterday evening; Switching &
  remotes, Devices (one row per device), openHAB runtime, Host and Usage
  follow. Colour means one thing everywhere, and state timelines show their
  words again instead of "9+". Control health is now derived from one
  `thermite:check` series per room, device and check, and alerts form a fixed
  catalogue: a failing check alerts per room after five minutes, and new
  alerts cover the gateway (probed over HTTPS with its own authority), starved
  or timing-out remotes, quarantined routing, a stalled policy worker, down or
  flapping services, broken monitoring and btrfs space, with critical and
  warning severities and inhibition behind a runtime or gateway outage. The
  gateway, policy worker and callback relay write their own metrics to
  node_exporter textfiles (never `/metrics` on the LAN listener), remotes
  report what they gave up waiting for through `POST /api/v1/telemetry`, and
  the exporter adds the routing executor's switch times, outcomes, failures
  and resends. `monitor: optional` keeps a device you know is broken out of
  control health. Phone delivery is unchanged.
- A locked room no longer blocks shared sources in other rooms: another room is
  refused only what would change the locked room's route, such as driving a
  device it is showing. Lock and Unlock live under Info on Remotes; the remote
  itself shows only the lock state.
- Web layout: below 1180 pixels one bar holds the menu, the page title, the
  page's actions and the mark at the right. On Remotes the room name with ▾
  replaces "Remotes" and Info and Log leave the room row for the bar; the
  light/dark switch moves into the menu, and phones no longer repeat the title,
  or Home's mark and tagline, under it. The desktop sidebar is 128 pixels without
  the wordmark, and a 60-pixel rail until Remotes shows its right panels at 1200;
  without those panels the rooms list keeps a panel's width. A phone on its side
  gets the whole remote, keys beside volume. Installed iPhone apps draw every
  page under the translucent status bar with the safe-area inset in the bar, not
  below it, and the remote clears the home indicator.
- Host layout: everything Thermite keeps under `/srv` lives in one root-owned
  installation root, `/srv/thermite`, a Btrfs subvolume where its parent is
  Btrfs: `runtime/` holds the service's state (and is the `thermite` account's
  home), `access/` the HTTPS gateway. `instance/host.yaml` no longer takes
  `state_root`; an optional `root` moves the whole installation. The gateway
  root is no longer derived by appending `-access`. `uninstall --purge` removes
  only `runtime/`, and install and upgrade refuse an existing root holding
  anything else. Existing installs are moved by hand before upgrading; there is
  no automatic migration.
- Remotes: a locked room shows a padlock on the bubble for what it shows, Off
  included, greys out every other bubble, and explains the lock (with a
  schedule's end) when the padlocked bubble is selected; the separate lock line
  is gone. Administrators can right-click or long-press a source or Off on the
  web, or right-click it or press `o` in the terminal, to lock the room on it:
  another source is chosen first and the room is locked only once it reports
  that source, and the lock API's optional `desired` refuses with 409
  `room_state_changed` if the room no longer shows it. While a room is locked,
  its keys and controls are greyed too. A room that is Off now selects Off and
  shows no remote until a device bubble is chosen.
- Room lists show a padlock after a locked room's source, and their labels say
  how it is locked: Home, and the Remotes room list in the browser and the
  terminal. The gateway's new `GET /api/v1/locks` reports every room's lock in
  one read, which clients poll only while a room list is on show.
- Security-relevant, opt-in: `guest_http` in `instance/host.yaml` opens a
  plain-HTTP Guest port on the gateway (13626 by default, extending the
  allocation to 13600–13626) for phones on a guest network. It binds only the
  listed addresses, closes connections from outside `clients`, and serves Home
  and Remotes as a Guest only: no sign-in, cookies, credentials, cameras,
  pairing or administration, with Origin and JSON checks on controls. Locks
  apply as over HTTPS. `thermite status` lists its addresses and install
  checks them. See [Guest access over plain HTTP](docs/installation.md#guest-access-over-plain-http)
  for the trade-offs.
- Remotes: `routing.rooms.<id>.audio` names the device a room's Volume and
  Mute drive, such as the streaming amplifier beside a passive desk monitor or
  a soundbar in a room nested under a Building. Validation requires one with a
  volume thermite controls, in a room the room covers or its own receiver or
  display. Since the remote model came from the house file alone, such rooms
  showed "No audio", though the gateway accepted the device's volume. Without
  `audio`, a receiver on a zone of its own no longer gives its room the main
  zone's volume; its display's is used. A room under a Building lists, and its
  gateway accepts, the devices in rooms nested in it. The Sonos volume and
  access callback tests the private site dropped are restored, with fictional
  devices.
- `gateway: {port: N}` in `instance/host.yaml` moves the HTTPS application off
  13600, the default. A moved gateway frees 13600 for `guest_http.port`; every
  other collision is still refused. Install and upgrade take the port from the
  host file, never from the release being replaced, so an upgrade closes the
  old listener, binds the new one and checks HTTPS readiness there; the Guest
  page's sign-in link, `thermite access stage`, `thermite remote`'s fallback
  and `thermite status` use it too. `thermite tui` and its bare `--url HOST`
  follow the selected site's port (13600 without a site), and say so plainly
  when a port answers plain HTTP. Bookmarks, home-screen apps and saved
  terminal logins are per address: see
  [moving the HTTPS application](docs/ports.md#moving-the-https-application).
- Routing: rooms on different zones of one receiver can share a source cabled
  to the receiver itself: list it under each room's `receiver.sources` on its
  input. Each room switches and powers only its own zone, so either can turn
  off without taking the other's picture; the source is never synced, never one
  room's sleep source, and under `idle: standby` sleeps only once no room uses
  it. Validation refuses rooms on one receiver that disagree about which input
  such a source is on, or that put another signal on it.
- Routing: a receiver zone can follow another room's zone of the same
  receiver (`receiver.sync_with: ROOM`). While both rooms show the same matrix
  source, the follower's zone takes the leader's input, so both screens show
  one picture. The follower falls back to its own matrix output before the
  leader changes (break before) and syncs only once the leader's output shows
  the source (make after); a follower turned off or switched elsewhere at its
  own devices is left alone. Under the lifecycle planner a synced route leases
  the leader's output, so it is neither moved nor parked while either room
  uses it. Room locks accept the synced input.
- `thermite tui` runs the terminal remote on any computer with thermite
  installed, with no private site: it reads the house from the gateway, opens
  `https://thermite:13600` by default, and takes `--url` as `HOST`,
  `HOST:PORT` or an https URL. `--ca-file` trusts a copy of the server's
  `tls-cert.pem` once and is remembered for that address; on the server, the
  installed gateway's authority is used automatically. An unreachable,
  unresolvable or untrusted gateway gets a two-line explanation.
- Remotes: the terminal locks and unlocks a room with `K`; web keyboard
  shortcuts obey locks; the lock control states its reason and last error;
  schedule locks show their end time; one failed status refresh no longer pauses
  controls; a Guest asking for an account-only page is not signed out; browser
  live updates reconnect after a gateway restart; source bubbles are announced
  as buttons rather than tabs.
- Installs and upgrades share one native readiness allowance
  (`--native-timeout`) across startup, a settle before gateway commissioning,
  commissioning and final readiness. Dropped native reads retry until the
  deadline and writes never repeat; bridged and initializing Things are
  pending, only configuration errors fail fast, and Things still initializing
  at the final check are warnings. A failed or crash-looping unit ends a wait.
- A run that fails after stopping services restarts them; Ctrl-C lets the host
  phase clean up. Unit ownership is recorded in `/etc/thermite/units.json`, so
  timezone and template changes no longer block upgrades. After a failed
  upgrade the installed command runs the pinned release, the Resume line names
  the fetched commit, and re-pinning an older release needs `--allow-downgrade`.
  Application environments install from a hashed lock and are rebuilt after a
  Python upgrade. Legacy unit namespaces, site launchers and renamed-verb
  aliases are removed.
- Root helpers refuse symlinked or hard-linked native configuration and never
  write through it. The HTTPS gateway uses a private certificate authority with
  an 825-day server certificate that renews automatically; a legacy self-signed
  certificate is replaced once, with a notice to trust the new authority on each
  client. `host.yaml` is validated before any unit is generated, and
  interrupted deploys stay tracked for cleanup.
- House files are validated strictly: unknown or mistyped keys get "did you
  mean" hints, and missing, unparsable or invalid house and host files exit 2
  with `file:line:col`. `mac` is optional except for Sonos; `notes` and
  `control4` are accepted documentation keys. `validate` runs every generate
  check, `generate --prune` removes outdated generated files, and `status`,
  `discover` and `ir-profile` have real `--json` output and exit codes.
  `docs export` no longer deletes the Markdown it builds from.
- The sandbox's read-only configuration fails closed: every Item on a
  commanding channel is guarded whatever its binding, and a rule without a SEND
  declaration is refused.
- Routing quarantines a device only when a command may have been sent without
  an answer; an unreachable or refusing device fails only that route, and
  quarantine clears itself after a fresh read. HEOS reads the receiver in its
  verified AppCommand format, and Android TV pairing no longer loops on a failed
  restore or reports a transient PIN state as unpaired.
- Vendor facts (probe and discovery ports, remote layouts, readback inputs,
  device keys, Control4 validation, the Android TV handler pool) live in their
  integration modules; generated output is unchanged. Denon and Marantz
  receivers report their selected input in room diagnostics.
- The build backend reads `[project]` and builds sdists; CI runs the checks on
  Python 3.11-3.14 for pushes and pull requests, and docs publish only after it
  passes. The Ubuntu ISO installs `python3-venv`.

- `thermite install --config DIR` deploys the complete stack and `thermite
  upgrade --config DIR` updates the application, native services, monitoring,
  HTTPS gateway and identity schema in one approved run. The stable command
  switches only after readiness; failed runs retain their candidate and backups.
- Installation checks firewall access, policy ownership and kernel rule support
  before stopping services or replacing units. Failed firewall inspection now
  shows its diagnostic and explains recovery when kernel modules are missing.
- Gateway commissioning writes the callback port in jUPnP's required integer
  string format and repairs an earlier decimal-string value on retry. Timeout
  errors identify the stalled operation; failed runs only claim an identity
  backup when that backup has actually been recorded.
- Runtime, native add-on and monitoring readiness use the successful polling
  result to complete their step. A redundant request can no longer turn a
  successful wait into an immediate failure during startup; later checks still
  query the running services.
- Final native readiness now follows gateway commissioning and service
  activation, then requires ten seconds of complete, stable initialization.
  Readiness and final health share the same Thing policy. Brief REST and Sonos
  handler reloads caused by commissioning are polled within their deadlines;
  authentication and configuration failures still stop deployment. A real
  isolated openHAB/jUPnP lifecycle regression covers configuration readback,
  handler recovery and unchanged credentials without restarting the JVM.
- Gateway deployment adopts existing authority and TLS, backs up stopped identity
  state before migrations, verifies callback routes and seals native ingress.
  Local terminal clients discover the verified HTTPS endpoint and certificate;
  installation grants no household role. New sites start with Security off.

- Thermite is the canonical application repository. New private sites track only
  configuration and an exact release pin; deployment derives runtime files from
  committed inputs using that release, including rollback.
- Denon/Marantz routing status requests use the receiver's accepted XML wire
  format, restoring power and input probes before source changes and Off.
- Root and `/overview/` open Thermite Home while native sign-in callbacks and
  deliberate runtime administration remain available. The web and terminal
  share navigation and a System view with explicit Advanced tools access;
  local monitoring interfaces retain their loopback bindings. System metadata
  is administrator-only in authenticated mode and loads without polling.

- Fresh installation provisions private internal runtime access automatically,
  with durable interrupted-setup recovery and no native account/token pages.
  Existing credentials are retained; advanced token import is explicit.
- Installation validates configuration without rerunning the developer test
  suite. Deployment authenticates before writing and distinguishes failed
  administrative checks from missing pairing-log protection.
- Installation completion reports device pairing/connectivity warnings while
  still requiring healthy runtime handlers, configuration and monitoring.
  Ordinary health checks continue to fail for unavailable devices.
- Daemons and containers use the `thermite-` namespace, with a shared
  `thermite.target` and `thermite.slice`. Legacy registrations migrate only
  after ownership checks; independent application accounts are never renamed.
- Installation uses concise status and confirmation output by default.
  Noninteractive administrative commands resume progress animation after
  authentication; `--verbose` keeps command details and routine output.
- Access release and state parents must be protected from service-account
  replacement before staging executes or writes application files. Existing
  shared parents need the documented permission migration.
- Access releases include a protected activation script for first installation
  and upgrades, with an explicit service cutover and running-release check.
- Upgrade backups are published only after successful archive creation, with
  completion evidence required for reuse. Interrupted and legacy unverified
  archives are retained without authorizing a migration.
- Upgrade planning includes missing prerequisites, and approved upgrades carry
  their approval through native startup recovery.
- Startup HTTP reads enforce the remaining deadline through response headers
  and bodies, including token validation.
- Arch Linux is the primary server example and default installer ISO base,
  using native guided disk and account setup. Ubuntu Server remains an
  alternative. Both ISO variants bundle the application.
- Published CPU, RAM, storage and network planning requirements for the full
  stack, with a repeatable fictional-house acceptance recipe.
- Installation checks generated files and private Git before changing the
  host. Cold boot checks share one startup budget across cache preparation,
  console readiness, authentication and graceful cache shutdown.
- Missing container images download before service startup, with visible
  progress, so first-time transfers do not consume systemd's launch timeout.
- Fresh native userdata builds its initial cache without add-ons and waits
  for the priming JVM to exit. Authenticated readiness checks add-ons, Thing
  handlers and metrics before proceeding; incomplete cache preparation fails
  closed and preserves its diagnostic marker.
- Failed access staging removes its partial release while preserving previous
  releases and persistent state. Environment failures retain a private diagnostic
  log; retry with a new plan directory after correcting the cause.
- Access staging saves a private, reviewable ownership script that the owner
  can resume, followed by service-user verification before activation.
- Access staging requires a trusted operator plan. Its custody helper rejects
  replaced paths and applies ownership through file descriptors, keeping
  handoff replay inside the reviewed release and state manifest.
- Access service units now use directive-specific systemd path escaping,
  including literal spaces, quotes and environment-variable characters.
- The policy worker configures its identity database before importing models
  in a fresh service process.
- Gateway shutdown drains active requests and closes event streams within a
  bounded cleanup window before systemd's stop deadline.
- Installation waits for native authentication during startup before offering
  to replace a saved API token; successful retries preserve the credential.
- Boot report collection retries temporary REST registration outages within
  its startup deadline and reports terminal query failures without a traceback.
  [Installation handoff](docs/installation-handoff.md) records artifact-specific
  guest coverage and remaining verification limits.
- A contiguous listener allocation at 13600–13625, starting with the site's
  HTTPS service and including each runtime's loopback shutdown control.
  Existing installations need the documented port migration.
- Updated documentation with screenshots of the web and terminal remotes,
  captured from the fictional example.
- Application services run as `thermite:thermite`, including native container
  startup. Existing Thermite IDs and data are preserved; conflicting legacy
  account names require administrator review before migration. Deployed files
  receive explicit ownership and permissions.
- Compact shared web navigation and improved Sony standby and command-error
  handling.
- Ordered control and error snapshots: older reads cannot overwrite newer
  native events, including unchanged-value events and reconnects.
- Apple TV commands are attempted once after dispatch; a lost acknowledgement
  reports uncertainty instead of automatically repeating a toggle or skip.
- Native HEOS receiver playback, gated by a fresh main-zone power/input read.
- Sixteen finite source CEC requests through an explicitly wired BK-808, plus
  optional model-verified Global Caché IR profiles for wider disc remote support.
  Physical player acceptance and learned IR codes remain commissioning steps.
- Handler-pool telemetry, actionable Prometheus rules, Alertmanager, a finite
  notification adapter and self-hosted ntfy. New services require owner
  installation; phone delivery is a separate endpoint/subscription setup.
- A reproducible, pinned Android TV connection lifecycle repair, with bounded
  connection opening and cancellation. Ordinary deployment does not activate
  this native binding replacement; it requires controlled owner maintenance.
- A reproducible, pinned HEOS binding reconnect repair for openHAB 5.2.3. Each
  reconnect closes the previous telnet sockets and their reader threads, and a
  quiet event stream is no longer reset on every heartbeat after two hours.
  `make check` fails when the openHAB pin moves, with instructions to retire or
  rebase the patch. Activation is a controlled owner step, as for Android TV.
- Explicit route ownership with optional unused-USB parking and last-consumer
  source standby. Independent device actors execute concurrently, while shared
  sources, AVR zones and manual controls retain protection. Route readiness,
  endpoint deactivation and cleanup are reported separately; event-driven
  reconciliation adds no polling loop.
- Label network reachability as Reachable / No response in the activity feed,
  keeping ping observations distinct from a device's reported power.
- Clear inherited directory set-ID bits during installation so private-state
  permission repair completes on existing installations.
- Keep the native HEOS bridge's optional-account diagnostic visible as a
  warning for online, account-free local transport, rather than blocking boot.
- Ask once per install, upgrade or uninstall run, including nested deployment
  ownership handoff. Show the direct native token-creation page and distinguish
  retained administrator accounts from first-run setup.

Security activation remains an explicit site choice. Installing this release
neither enables accounts nor commissions physical devices.
