# Web remote

Run `thermite generate` from your private site after editing its house file.
Home is served at `/`; Remotes at `/remotes/`.

Guests can view remotes and control unlocked rooms without signing in, whether
authentication is enabled or disabled. A room that is Off selects its Off
bubble and shows no remote until a device bubble is tapped for its buttons. In
a locked room the bubble for what it shows, Off included, carries a padlock and
every other bubble is greyed out; tapping the padlocked bubble says why it is
locked and, for a schedule, until when.

When authentication is enabled, administrators can use **Lock room**, under
Info, to hold
the current reported source or Off indefinitely. **Unlock room** releases that
manual hold; an active schedule can still keep the room locked. Manual locks
survive restarts, preserve schedules, and take precedence until released.
Right-click or long-press a source bubble, or Off, for a menu that locks the
room on it: a different source is chosen first and locked once the room
reports it.

Source previews use the fictional sample. Add `?backend=mock` to simulate
controls without sending commands.
