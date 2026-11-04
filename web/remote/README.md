# Web remote

Run `thermite generate` from your private site after editing its house file.
Home is served at `/`; Remotes at `/remotes/`.

Guests can view remotes and control unlocked rooms without signing in, whether
authentication is enabled or disabled. Locked rooms retain the normal remote
layout, show a lock indicator, and disable ordinary controls.

When authentication is enabled, administrators can use **Lock room** to hold
the current reported source or Off indefinitely. **Unlock room** releases that
manual hold; an active schedule can still keep the room locked. Manual locks
survive restarts, preserve schedules, and take precedence until released.

Source previews use the fictional sample. Add `?backend=mock` to simulate
controls without sending commands.
