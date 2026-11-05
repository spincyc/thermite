# Web remote

Run `thermite generate` from your private site after editing its house file.
Home is served at `/`; Remotes at `/remotes/`.

Guests can view remotes and control unlocked rooms without signing in, whether
authentication is enabled or disabled. A room that is Off selects its Off
bubble and shows no remote until a device bubble is tapped for its buttons.
Volume and Mute drive the room's `audio` device when the house file names
one (`routing.rooms.<id>.audio`, such as the streaming amplifier beside a
passive desk monitor); otherwise its receiver, unless that is on a zone of its
own, else its display. In
a locked room the bubble for what it shows, Off included, carries a padlock and
every other bubble is greyed out; tapping the padlocked bubble says why it is
locked and, for a schedule, until when. The room lists, on Home and in Remotes'
sidebar and room sheet, show a padlock after a locked room's source, and a
screen reader hears how it is locked. Only a lock the remote has read greys
its controls. If a room's lock cannot be read, the remote says “Room lock
status unknown” and still sends commands: the server checks each one and
refuses it in a locked room, and the remote then says why and reads the lock
again.

When authentication is enabled, administrators can use **Lock room**, under
Info, to hold
the current reported source or Off indefinitely. **Unlock room** releases that
manual hold; an active schedule can still keep the room locked. Manual locks
survive restarts, preserve schedules, and take precedence until released.
Right-click or long-press a source bubble, or Off, for a menu that locks the
room on it: a different source is chosen first and locked once the room
reports it.

Each page keeps one live connection to thermite while it is shown: Remotes
for rooms, devices and the activity log, Home and Topology for room sources
only, Pairing for its devices. The page fills from the first message and
needs no further reads; a page in the background closes its connection and
catches up when shown again. When thermite loses openHAB the page says the
live reports are unavailable and refreshes itself once they return.

Volume is each audio device's own number on its own scale: 38 on a WiiM or a
Sonos, 69.5 on a Denon or Marantz receiver, or −10.5 dB when the receiver's
own display is set to show dB. The slider runs from the device's minimum to
its maximum, and each press of + or − moves one of the device's own steps
(0.5 on a receiver, 1 elsewhere). Nothing caps or rescales it; a limit set on
the device itself is the device's to apply.

Source previews use the fictional sample. Add `?backend=mock` to simulate
controls without sending commands.

When a page gives up waiting on the gateway (a lock read, a room command, an
event stream or its first snapshot), it counts that and reports the counts, never
their content, to the gateway after its next answered request, or as the page
is hidden. Pages opened with `?backend=mock` or in native mode report nothing.

Below 1180 pixels a bar runs across the top: ☰ opens every view and the
light/dark switch, followed by the page title, the page's actions and the
thermite mark. On Remotes the title is the room, which opens the room list, and
the actions are Info and Log. From 1180 pixels a narrow sidebar lists the views
with the theme switch at its foot; until Remotes shows its right-hand panels at
1200 pixels it is a rail whose ☰ opens the same list. A phone on its side, 500
pixels tall or less, shows the remote across the whole width with its keys
beside the volume.

Added to an iPhone home screen, every page draws under the translucent status
bar: the bar (or sidebar) pads itself by the safe-area inset and the remote
keeps clear of the home indicator.
