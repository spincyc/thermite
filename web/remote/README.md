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

Audio (`/audio/`) is a page for each speaker or streamer thermite controls
itself, a WiiM or a Sonos under `control` in the house file, whether or not
a room routes it. It shows what plays (title, artist and album, the service
and the stream's format), the device's transport keys, volume, mute and
input, then its settings by section: play mode, EQ, bass and treble,
subwoofer, surround, night and speech modes, the status light, as each device
has them. Below, folded, is what the device reports about itself, such as its
firmware or the TV audio format. A setting shows only while the device
reports it, and only where it applies (a WiiM's play mode on Wi-Fi). A change
shows at once and pulses until the device reads it back. A remote whose
volume or keys drive such a device names it as a link to its page. Guests
may use it without signing in. When a room that owns the device is locked,
the page is locked too. A room owns its `audio` device and the soundbars,
streaming amplifiers and audio streamers in its own room. The gateway
checks each command (`POST /api/v1/devices/<id>/commands`) against the
device's actions and those locks. A locked room that only shares the device refuses just what would
disturb what it shows.

Music (`/music/`, beside Audio under Remotes) plays TIDAL and the NAS libraries
on the house's Sonos and WiiM speakers through Music Assistant, on a host whose
site has `music:`. It lists the speakers, a group's together under what it
plays, and for the chosen one its cover, title, artist and album, where it is
in the track, its shuffle, previous, play, next and repeat keys, its volume and
its group. Search (all, or tracks, albums, artists or playlists; everywhere, in
the libraries or on TIDAL), Browse (each library's folders and TIDAL's) and
Queue (the chosen speaker's, or its group's) list what can play: a tap offers
Play now, which replaces the queue, Play next or Add to queue, and Open for an
album, artist, playlist or folder. Group adds speakers to the chosen one's
group, removes them or ends it; Sonos and WiiM speakers group only over
AirPlay, and the page says which could not join and why, and which Music
Assistant was asked to group but has not confirmed (not refused). A change
shows at once; until the speaker reads it back, shuffle, repeat and a new
position pulse and a volume dims. A speaker in a locked room is locked too; a
locked room of another speaker in its group holds what moves the group, not the
speaker's own volume. The gateway refuses their commands anyway. An
administrator signs Music Assistant into TIDAL on the page, which shows the
code to approve at link.tidal.com and follows the sign-in until it is done. A
Guest kept from free text (Security) sees whether each speaker plays, but no
titles, covers or library. Without music on the host, or while Music Assistant
is away, the page says so in one line. Its keys: Space or P plays and pauses, +
and − step the volume, < and > change the track, [ and ] choose the speaker, S
shuffles, R repeats and / searches.

Source previews use the fictional sample. Add `?backend=mock` to simulate
controls without sending commands.

A page that hears nothing on its event stream for 15 s (the gateway sends a
heartbeat every 5 s) reconnects it, and one shown again, such as the iPhone
app back from the background, reconnects at once if its stream went silent.
Each reconnection reads the house afresh: anything the gateway does not know
shows as unknown, not as it was. A command unanswered after 15 s (All off: 30
s) stops waiting and says it may still take effect.

When a page gives up waiting on the gateway (a lock read, a room command, an
event stream or its first snapshot), it counts that while it is shown and
reports the counts, never their content, to the gateway after its next
answered request, or as the page is hidden; it keeps them until a report is
answered. Pages opened with `?backend=mock` or in native mode report nothing.

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
