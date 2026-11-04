# Changelog

## 0.1.0.dev0 — Unreleased

The portable application remains a development build. Its version and verified
file manifest identify an exact release. Private sites pin that release;
`init`, explicit upgrades and retained releases preserve configuration and
rollback. The unified deployment workflow uses a separate committed private site.

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
- A locked room no longer blocks shared sources in other rooms: another room is
  refused only what would change the locked room's route, such as driving a
  device it is showing. Lock and Unlock live under Info on Remotes; the remote
  itself shows only the lock state.
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
- Volume controls default to each integration's full supported range. Explicit
  per-device `volume_max` limits still apply consistently to commands and clients.
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
