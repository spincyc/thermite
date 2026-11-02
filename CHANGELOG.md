# Changelog

## 0.1.0.dev0 — Unreleased

The portable application remains a development build. Its version and verified
file manifest identify an exact release. Private sites pin that release;
`init`, explicit upgrades and retained releases preserve configuration and
rollback. Existing complete Git checkouts retain their deployment behavior.

- Separate setup guides for an existing Linux server and a dedicated machine,
  with a bootable Ubuntu Server ISO builder and bundled application installer.
- A contiguous listener allocation at 13600–13622, starting with the site's
  HTTPS service. Existing installations need the documented port migration.
- Updated documentation with screenshots of the web and terminal remotes,
  captured from the fictional example.
- Application services run as `thermite:thermite`, including native container
  startup. Existing installations retain their numeric IDs and data during
  account migration; deployed files receive explicit ownership and permissions.
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

Security activation remains an explicit site choice. Installing this release
neither enables accounts nor commissions physical devices.
