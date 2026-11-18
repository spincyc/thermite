# Web remote

Run `thermite generate` from your private site after editing its house file.
Home is served at `/`; Remotes at `/remotes/`.

Guests can view remotes and control unlocked rooms without signing in. A room that is Off selects its Off
bubble and shows no remote until a device bubble is tapped for its buttons;
where the remote will be, the page says that the room is off and what to tap.
Volume and Mute drive the room's `audio` device when the house file names
one (`routing.rooms.<id>.audio`, such as the streaming amplifier beside a
passive desk monitor); otherwise its receiver, unless that is on a zone of its
own, else its display. In
a locked room the bubble for what it shows, Off included, carries a padlock and
every other bubble is greyed out; tapping the padlocked bubble says why it is
locked and, for a schedule, until when. The room lists, on Home and in Remotes'
sidebar and room sheet, show a locked room's padlock in its own column, between
the room and its source, and a screen reader hears how it is locked. Each
room's light follows its source: in Remotes at the row's right edge, the source
against it, and what plays under the row; on Home it leads the room's status.
Only a lock the remote has read greys its controls. If a room's lock cannot be
read, the remote says “Room lock status unknown” and still sends commands: the
server checks each one and refuses it in a locked room, and the remote then
says why and reads the lock again.

Administrators can use **Lock room**, under
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

What has failed is part of what a room shows, for as long as it lasts, and
never only a passing message. While the house is not heard, Remotes' alert
line says “Not connected to thermite. Reconnecting…” and every light keeps
its place and loses its colour, since what it shows was read before; Home
shows no source and says the same above its rooms. A source that was chosen
and sent but never read back is named in the alert (“Player was not
confirmed”) until the room reads something else or another choice is made.
A device openHAB cannot drive is named there with why: its problem is one of
a few codes, and the page says the words the house's `activity.json` gives
that code (`items.<id>_problem.options`, the last one for a problem of any
other kind), as the activity log does. The terminal and the handheld remote
read all of it from the same place (`session.js` `view()`).

Home shows the house's firing alerts above its rooms, from the gateway's
feed (`GET /api/v1/alerts`), read when the page is shown and every 30 s
while it is. `alerts.js` is the one model of them: what is shown, in the
feed's order, with the room and the device named by their labels and the
check in words, and what the panel's one sentence says. “Nothing is
firing.” is said only of an answer in which monitoring is evaluating its
rules, counts nothing firing and lists nothing; when monitoring is stalled
or does not answer, or the gateway does not, the panel says that alerts
cannot be read. Alerts the viewer is not given are counted (“and 2 more
that are not shown here”), since the feed says how many fire in all. One
reading that is not “ok” is held back and read again after 16 s, so a
restart does not flash a warning. A room an alert is about carries a mark
beside its name. There is no toast and no sound. `?backend=mock&alerts=`
`firing`, `admin`, `hidden`, `house`, `stalled`, `silent`, `unreachable`
or `unanswered` shows each state on the sample house.

Volume is each audio device's own number on its own scale: 38 on a WiiM or a
Sonos, 69.5 on a Denon or Marantz receiver, or −10.5 dB when the receiver's
own display is set to show dB. The slider runs from the device's minimum to
its maximum, and each press of + or − moves one of the device's own steps
(0.5 on a receiver, 1 elsewhere). Nothing caps or rescales it; a limit set on
the device itself is the device's to apply.

Remotes watches and listens. Watch is the remote above, in the rooms with a
screen; Listen is the speakers', in the rooms whose speakers thermite controls.
A house with both shows a Watch | Listen switch, two icons (a screen and a
note), beside the room's name, and Home one over its rooms (Y toggles
either); it chooses the rooms the lists hold, and the choice is kept, the
same on both pages: each page opens in the last one made. Watch lists the
routed rooms with their sources; Listen the routed rooms with speakers, then
the rooms of speakers no routed room owns, each with what it plays. A room of
one kind alone, opened from a link, brings its kind. A routed room owns
its `audio` device and the soundbars, streaming amplifiers and audio streamers
in its own room; a WiiM or a Sonos under `control` in the house file is a
speaker. Watch's volume caption names the room's speaker as a button to
Listen to it. A Sonos Arc on its TV input reads as playing whatever its TV
sends; while its TV audio format is no signal or silence and it names no
track, Listen counts it idle, whatever Music Assistant says, and its Play
resumes Music Assistant's queue (without Music, Play is off).

Listen shows the room's speakers as bubbles (when it has more than one; 1 to
9 choose), then for the chosen one what plays: its cover, title, artist and
album, the service and the stream's format, where it is in the track, its
shuffle, previous, play, next and repeat keys, its volume (hold + or − to
repeat) and mute, and chips for its group, its input and the library. Info
holds its power, its settings by section (play mode, EQ and presets, bass and
treble, subwoofer, surround, night and speech modes, the status light, as
each device has them) and, under About this speaker, what it reports about
itself, such as its firmware or the TV audio format. A setting shows only
while the device reports it, and only where it applies (a WiiM's play mode on
Wi-Fi). A change shows at once and pulses until the device reads it back.

On a host whose site has `music:`, Music Assistant plays TIDAL and the NAS
libraries on the speakers, and Listen shows what it plays. The library
(a sheet on a phone; beside the remote from 1200 pixels, where Info is then a
tap on the Info button) has Search (all, or tracks, albums, artists or
playlists; everywhere, in the libraries or on TIDAL), Browse (each library's
folders and TIDAL's) and Queue (the chosen speaker's, or its group's). A tap
on a folder, album, artist or playlist opens its page over the tab, and Back
(named for where it returns) goes back a page; the tab tapped again returns
to its own list. An artist's page has its top tracks, albums, EPs and
singles, compilations and similar artists, a few of each and All the rest;
an album's or a playlist's has its tracks, an album's by number. Each has
Play, Shuffle and More (Play next, Add to queue) in its header, and an
album's artists open theirs. A tap on a track offers Play from here (on an
album or playlist), Play now, which replaces the queue, Play next or Add to
queue, and Go to its album or artists. Group adds speakers to the chosen
one's group, removes them or ends it, and a group's volume shows under the
speaker's; Sonos and WiiM speakers group only over AirPlay, and the sheet says
which could not join and why, and which Music Assistant was asked to group
but has not confirmed. An administrator signs Music Assistant into TIDAL from
the library. A Guest kept from free text (Security) sees whether a speaker
plays, but no titles, covers or library. Without music on the host, or while
Music Assistant is away, Listen plays what the speaker reports itself and
says why in one line.

A speaker in a locked room is locked as Watch is: its bubble carries the
padlock (or, with one speaker, its name does), its controls are disabled and
the alert says which room holds it. A locked room of another speaker in its
group holds what moves the group, not the speaker's own volume. The gateway
checks each command (`POST /api/v1/devices/<id>/commands` and the music API)
against the device's actions and those locks; a locked room that only shares
the device refuses just what would disturb what it shows. Guests may listen
without signing in. Listen's keys: Space or P plays and pauses, < and > change
the track, + and − step the volume, M mutes, S shuffles, R repeats, G opens the
group, / searches, 1 to 9 choose the speaker and [ and ] the room.

Source previews use the fictional sample. Add `?backend=mock` to simulate
controls without sending commands. There `&choose=<source>` opens the room on
that source, as the documentation's screenshot does; on a real house the page
ignores it, so no link can switch a room.

Everything the sample fakes is in files of its own, which a phone never
downloads: `backend-mock.js` (the house), `music-backend-mock.js` (speakers
and a library), `alerts-mock.js` (Home's alerts), `pair-mock.js` (pairing)
and `access-mock.js` (the demo account). A page names the ones it can use in
its head (`<link rel="thermite-mock" href="…">`, before `navigation.js`), and
`navigation.js` loads them only for `?backend=mock` or a page opened from
its sources. Each registers what it fakes; the script it stands in for
(`backend.js`, `music-backend.js`, `alerts.js`, `pair.js`, `access.js`) takes
it when asked for a mock, and says which file is missing when it is not
there. The terminal remote loads them for `--mock`
(`tui/remote/shared.js`). A new fake goes in a `*-mock.js` file, in `FILES`
(`lib/oh_features/remote.py`) and in the head of each page that uses it.

A page that hears nothing on its event stream for 15 s (the gateway sends a
heartbeat every 5 s) reconnects it, and one shown again, such as the iPhone
app back from the background, reconnects at once if its stream went silent.
Each reconnection reads the house afresh: anything the gateway does not know
shows as unknown, not as it was. A command unanswered after 15 s (All off: 30
s) stops waiting and says it may still take effect.

A page that stays open across an upgrade replaces itself. `thermite generate`
gives the files of `conf/html/remote` one build, a hash of them all, and
writes it into every page; the gateway repeats the build of the pages it
serves in each stream's snapshot. A page that hears another build than its
own reloads, once for each build, so the iPhone app needs no reload button.
If the reload brings the same page again, the page says once that it could
not load the new version, and asks to be closed and opened again. Pages
without a stream (Cameras, System and the account pages) are current
whenever they are opened; previews and native mode have no build to compare.

When a page gives up waiting on the gateway (a lock read, a room command, an
event stream or its first snapshot), it counts that while it is shown and
reports the counts, never their content, to the gateway after its next
answered request, or as the page is hidden; it keeps them until a report is
answered. Pages opened with `?backend=mock` or in native mode report nothing.

Below 1180 pixels a bar runs across the top: ☰ opens every view and the
light/dark switch, followed by the page title, the page's actions and the
thermite mark. On Remotes the title is the room, which opens the room list,
with the Watch | Listen switch beside it (the mark then gives way to the
name), and the actions are Info and Log. From 1180 pixels a narrow sidebar
lists the views with the theme switch at its foot; until Remotes shows its
right-hand panels at 1200 pixels it is a rail whose ☰ opens the same list. A
phone on its side, 500 pixels tall or less, shows the remote across the whole
width with its keys beside the volume.

On a touch screen every control is at least 44 pixels wide and tall. What
scrolls sideways says so: the row of bubbles, where a finger has no arrows to
press, and the Site page's tables end in a chevron on the side where more is
out of sight (the page keeps `data-more` on the scrolling box, `left`, `right`
or `both`, and the styles draw from it). On a phone the bar names the page, so
a heading that would only say it again is left to screen readers: `shell.js`
marks an `h1` equal to the bar's title with `data-thermite-titled`, and a page
whose heading says it in other words ("Site topology") marks its own.

`make check-browser` holds the pages to this in a real browser: each of the
example house's pages at a phone's and a desktop's size, behind the gateway's
Content-Security-Policy header (`tests/browser_smoke.py`). It also opens
each page without `?backend=mock` and fails if a `*-mock.js` file is
downloaded then. On the phone it opens Remotes' room information as
well and holds that sheet to the same sizes. The sample cannot show a
bubble's lock menu, the Lock room button or a room's own controls (its demo
account has no lock to manage, its house no room controls); a test of
`remote.css` holds their rules instead.

Added to an iPhone home screen, every page draws under the translucent status
bar: the bar (or sidebar) pads itself by the safe-area inset and the remote
keeps clear of the home indicator.
