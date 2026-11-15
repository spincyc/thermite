# thermite identity core

This package is a server boundary, not an assertion that the current native
runtime listener is sealed. It contains no production account or credential.
Python 3.11 or newer is required. Install `lib/requirements-site.txt` with
`pip --require-hashes` in the service's isolated environment.

The Security page controls a persistent authentication property. New identity
databases start with authentication off; existing databases with accounts
migrate with authentication on. The gateway stays in place in both modes.

There are three principals. A **Guest** (no credential) reads the site and
uses ordinary controls in unlocked rooms in either authentication mode, and
sees camera media (`/unifi/media/…` snapshots and live video) only while
authentication is off. A **Member** is any signed-in non-admin account and
adds camera media. An **Admin** account (`admin: true`) adds account
administration, pairing, room locks and schedules. Their canonical
permissions, as `/api/v1/me` reports them:

| Principal | Permissions |
|---|---|
| Guest | `site.read`, `room.control`, plus `camera.view` while authentication is off |
| Member | `site.read`, `room.control`, `camera.view` |
| Admin | Member plus `settings.manage`, `users.manage`, `pairing.manage` |

There are no roles or per-account room grants: Guests already control every
unlocked room, so neither could restrict anything. Account administration,
pairing, room locks and schedules are disabled by the backend when the switch
is off, and the worker cannot enforce saved policies. Saved accounts and
policies remain in private storage. Native testing mode is a separate
direct-runtime path, not this property or an authentication fallback.

For first activation, the owner runs `thermite access setup --config …` on
the service host. It prints a one-use code to an interactive terminal only.
The database stores its digest and 15-minute expiry. Enter the code and a new
administrator username/password on the HTTPS Security page. Account creation,
code consumption and enabling authentication commit atomically. An unarmed,
expired, repeated or competing bootstrap cannot claim ownership. Alternatively,
the local CLI calls `composition.initialize(state_directory, username,
password, allowed_hosts)`, supplying the password from a hidden terminal
prompt, to create the first Admin and enable authentication immediately.
Further starts call
`composition.setup(...)`, install a `runtime.RuntimeAdapter` through
`install_adapter()`, then run `oh_identity.asgi:application` with a production
ASGI server. No default state path, account, password or upstream token exists.

GET `/api/v1/security` exposes only mode, revision, setup availability,
`guest_text` and CSRF data. POST uses `{enabled,revision,password}`; first activation additionally
requires `username` and `setup_code`. Re-enabling an existing site requires
an active Admin's `username` and current password. Disabling requires the
current Admin's password. Both browser writes and terminal bearer writes retain
their existing CSRF/channel protections. Mode changes revoke prior sessions
and tokens, fence queued commands and streams, and revise retained schedules
so old worker receipts cannot execute after a transition. Clients can continue
as Guests after enabling; privileged use requires a new browser sign-in or
`thermite access login` in the terminal. An explicitly supplied expired or
invalid session/token is rejected rather than downgraded to Guest; sign out
to return to anonymous controls.

`guest_text` says whether Guests see free-text Items: the activity feed's
titles, artists, apps, playback and the like (contract states marked
`free_text`). It is on until an Admin turns it off with PUT
`/api/v1/security/guests` `{"guest_text": bool}` (the Security page; a Guest
gets 401, a Member 403; audited as `security.guest_text_on`/`_off`), and binds
only while authentication is on, since with it off every client is a Guest.
While it is off, a Guest's stream joins its view's `:without_text` variant,
which carries neither those Items' values nor their events, and a Guest's GET
of one's state is 404. A Guest stream opened under the other choice ends at
its next check and reopens under this one; accounts' streams go on.

Privilege elevation is an account operation: sign out and sign in as an Admin,
or have an Admin set `admin` on Users. Any account change revokes that
account's sessions and tokens; a new sign-in is required. Admins still obey
active room policies and receive no operating-system privileges, shell
execution or arbitrary native REST access.

The service-owned state directory is 0700; database, site UUID and secret key
are 0600, with umask 077 also protecting SQLite side files. An immutable site
UUID makes session and CSRF cookie names distinct between deployments on one
hostname. Do not share one identity database between sites. Use validated TLS,
an explicit host allowlist, and a trusted local proxy configuration. Disable
access/request body logging; authentication and pairing secrets never belong
in URLs or logs. No CORS or arbitrary proxy endpoint is provided. SQLite runs
in WAL mode (`synchronous=NORMAL`), so status reads proceed beside worker
writes; the `-wal`/`-shm` side files are private like the database.

Server errors (`django.request` 5xx) and `oh_identity`, `oh_access` and
`oh_room_policy` warnings go to stderr, which the units send to the journal.
Records carry the path, traceback frames and exception class only: never
request objects, headers, bodies, query strings or exception messages.

The command trail (`oh_identity.trail`, INFO) adds one key=value line per
command, All off batch, security event, event stream and gateway request.
Every command gets an 8-character ID: its `cmd` line names the origin (`web`,
`tui`, `guest`, `api` for a client that names none, `all_off`, `schedule`,
`repair`), the client's own `X-Thermite-Client` label (`web/<tab>` or
`tui/<host>`, a label and never authority), the account, its credential kind,
a token's first 8 ID characters, the request ID, room, action, item, value,
outcome and milliseconds. A refusal names its lock (`lock=manual` or
`lock=schedule`, and `lock_room`); an openHAB failure its HTTP status or
exception class. openHAB receives the ID as the command's event source,
`X-OpenHAB-Source: thermite.<origin>:<id>`. Pairing values are `<private>`;
addresses appear only as `peer=`, the first 8 characters of the audit's keyed
peer digest. Every audit row is mirrored as an `audit` line once committed.
Each response carries `X-Request-Id`; requests that are not GET or HEAD, and
responses that are not 2xx, get a `req` line, so successful polls and assets
are not logged. Event streams log `stream open` and `stream close` with their
duration and the peer's and gateway's stream counts. A saved policy that
cannot be evaluated logs `policy_unavailable` at WARNING, at most every five
minutes per room. Nothing of the trail is stored beyond the journal. Every
response carries
`Content-Security-Policy: default-src 'self'; img-src 'self' blob:;
object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action
'self'` (camera snapshots display through `blob:` URLs; WebRTC is not governed
by CSP). There is deliberately no HSTS: households reach the gateway through a
self-signed certificate exception that HSTS would forbid.

The optional plain-HTTP Guest listener (`oh_access.guest`, configured by
`guest_http` in `instance/host.yaml`) marks each request it accepts in the
ASGI scope; no header can. `oh_identity.guest` answers such a request only as
a Guest: its `Host` must name a listener address or name with the port; a
site session or CSRF cookie or an `Authorization` header is refused with 403
`secure_connection_required`; only the routes in `ROUTES` (Home, Remotes and
their files, `me`, `security`, item state, the `remote` and `home` event
streams (not `pair`), room policy status,
`locks`, room commands, All Off, and music with its event stream but not
TIDAL's sign-in) are found; and a POST needs one of the
listener's own origins and a JSON body in place of the CSRF cookie. Other
cookies are dropped unread and no response sets one. Commands keep every room
check, locks included. The page's access configuration names the HTTPS site
for signing in. Responses keep the headers above except
`Cross-Origin-Opener-Policy`, which browsers ignore without TLS, and are
never redirected to HTTPS.

Browser login: GET `/api/v1/session` returns a CSRF token and secure CSRF cookie;
POST the same endpoint with JSON `{username,password}` and `X-CSRFToken` from
the same origin. It creates a Secure/HttpOnly/SameSite session. POST logout is
CSRF protected. For TUI, POST JSON `/api/v1/token-login` with
`{username,password,name,days}` without Cookie or Origin headers; this distinct
password-authenticated channel issues a personal token and creates no browser
cookie. It rejects browser Origin/cookie use and non-JSON bodies. Use
the `Authorization` header with the `Bearer` scheme (case-insensitive) for subsequent TUI requests, including SSE;
never put the token in a query string. A token cannot mint another token.
POST `/api/v1/logout` with an already invalid bearer token succeeds without
effect. A session whose account changed is refused once (401) and flushed, so
the next request continues as a Guest, as after a deleted session.

Password attempts are throttled by the direct peer only (forwarded headers are
ignored); an IPv6 peer counts as its /64 and an IPv4-mapped address as IPv4.
Only failures count. Five failures for one username from one peer lock that
pair for five minutes. A username failing from many peers is slowed, never
locked: after ten failures each attempt waits 1, 2, 4… up to 60 seconds after
the last one. One peer is refused after 30 failures across usernames. Refusals
answer 429 `login_throttled` with `Retry-After`. Two password verifications run
at once at most. Unknown usernames create no account bucket, so throttle rows
are bounded per peer and expire. A malformed body (missing field, wrong type,
over-length password) is 400 `invalid_login`, not a failed guess.

GET `/api/v1/me` returns `{id, username, active, admin, is_guest,
auth_enabled, can_edit, site, permissions}`; a Guest is id 0, username
`Guest`. The token-login and session-login `user` members use the same shape.
Admins use GET/POST `/api/v1/users` (`{users:[{id, username, active,
admin}]}`; POST `{username, password, admin?}` answers 201) and PATCH/DELETE
`/api/v1/users/<id>` (`{username?, password?, active?, admin?}`; DELETE
deactivates). Unknown fields and non-boolean flags are 400. Every account
change revokes that account's prior authorization revision. Deactivating or
demoting the last active Admin is 409 `last_admin`; concurrent attempts are
serialized by SQLite IMMEDIATE transactions. A wrong method answers 405 with
an `Allow` header naming the route's methods.

Personal token GET/POST `/api/v1/tokens`, DELETE `/api/v1/tokens/<uuid>` expose
metadata only except the raw token's one issuance response. Tokens expire in
1–90 days, store a digest, and are checked against the current active user and
authorization revision. Token logout revokes that token. The list omits
revoked tokens and those that expired over a week ago.

Each page holds one event stream, GET `/api/v1/events/<view>` with no query:
`remote` (the remote page and the terminal remote: room Sources as
`stateupdated`, controls, problems, route problems and the activity feed's
Items as `statechanged`, and the feed's commands), `home` (Home and Topology:
room Sources as `statechanged`) or `pair` (Admin only: pairing status and
result Items as `stateupdated`). `oh_site_contract.views` names each view's
Items; the gateway keeps only public topics of them, and an Item a view takes
as `stateupdated` is never also sent as `statechanged`. The first frame is a
snapshot, `{"type":"snapshot","view":...,"states":{item: value}}`, of the
view's projected values; events follow in openHAB's own shape. While openHAB
is not answering, the stream stays open and sends `{"type":"unavailable"}`,
then a fresh snapshot once it is back. A client replaces what it knew with
each snapshot: an Item a snapshot lacks reads as not known, never as its
value before the gap. A stream quiet for 5 s sends a heartbeat,
`{"type":"heartbeat"}`, as a data frame (an `EventSource` hides comments);
the clients' watchdog (`web/remote/access.js`) reopens a stream that shows
no frame for three heartbeats while its page is shown, and checks at once
when a page is shown again. A stream checks the user,
session/token existence, expiry and revision (a Guest's, whether it sees
free text) when it opens, at most once per
second while events flow, and at every quiet-stream heartbeat (5 s), and ends
when one fails. Each peer bucket may hold 8 streams (a page each, a terminal
two while it pairs, and a reload's old stream until its next heartbeat) and
the gateway 48, under half of its 100 in-flight requests; more answer 429
`too_many_streams` with `Retry-After`. Observers in `oh_identity.hub`
(`observers`) see each stream open, close (with its seconds and reason) and
refusal, keyed by view, origin (`https` or the `guest` listener), principal
kind, HTTP version and peer digest; `EventHub.status()` counts streams per
view and the upstream's connections, failures and resyncs.

POST `/api/v1/telemetry` takes what clients saw but the gateway could not: the
give-ups and stream failures of requests that never reached it. Its schema is
finite and carries no free text: at most 2 KB of `{"client": "web"|"tui",
"protocol", "events": {<kind>: {"count", "waits"}}}` with fixed kinds, counts
of 1–1000 and at most 16 waits of 0–600 s. Guests report too, so it needs no
identity; a report can go out before its page holds a CSRF token, so it is
CSRF-exempt, but it changes only counters, accepts only `application/json`
and refuses a foreign Origin (on a Guest listener, that listener's origins).
Each peer bucket may report 6 times a minute and all peers together 120;
more answer 429 `telemetry_throttled` with `Retry-After`. An accepted report
answers 204 and becomes `thermite_client_*` series and one journal line naming
the client, protocol, a keyed digest of the peer bucket and the counts, never
the address.
Every problem code the app answers with is in `auth.PROBLEMS`; the gateway
counts each by code, and anything else as `other`.

Security events are audited with the principal kind (`guest`, `session`,
`token`, `password`, `local` or `anonymous`) and a keyed digest of the peer's
address bucket, never the address. Ordinary room commands and All Off are not
security events and are not audited (their trail is the journal's `cmd`
lines). A denied sign-in records the account's ID when the name exists and
never the attempted name; a token's audit target is its ID's first 8
characters and its name, and a terminal names its token after its host.
`thermite access audit --config FILE [--since 7d] [--action login]`, run as
the service account, reads the table. Denied sign-ins keep their own
1000-record cap so they cannot evict the 5000 other records. The worker prunes
daily: boundary receipts older than three days (the worker looks back two),
expired sessions, revoked tokens, tokens expired over a week, and expired
throttle rows.
Commands and admin mutations revalidate the originating session/PAT after lock
or transaction waits, so a queued request cannot outlive credential revocation.

The cold review for optional authentication identified the old client-only mode,
unfenced worker effects, first-visitor ownership and stale credential elevation
as the transition risks addressed above. It also found that password creation
accepted lengths the login endpoint rejected; both now enforce the same
1024-character maximum. Native-listener isolation remains a deployment
requirement, not something a database toggle can establish.

GET/PUT `/api/v1/rooms/<id>/policy` uses `{revision,config}`. Room IDs are
inventory snake_case (`[a-z][a-z0-9_]{0,99}`), enforced by the URL converter,
the runtime contract and the terminal client. Guests may read policy status
while authentication is enabled; `can_edit` and `can_lock` are true for an
Admin. PUT is Admin-only (a Guest gets 401 `login_required`, a Member 403),
CAS protected and delegates validation to the policy engine; a refusal is
`{"error":"invalid_policy","reason":code,"field":path}` with the engine's fixed
code and field path when it has them. Room, timezone and new revision are
supplied by the server; a room's first row continues after any revision its
pruned predecessor left in receipts or status. Missing engine with a saved
active policy denies control; it never silently unlocks. A policy is active
when it is enabled or holds a manual lock (`oh_room_policy.active`); a
disabled schedule without a hold is inert everywhere, even if it no longer
validates. A stored hold naming a source the house no longer offers is
released by a schedule edit. Policies for rooms no longer in the compiled
house are ignored by the worker; `thermite access prune --config …` deletes
them. The separate worker uses `policy_store.Store`
for revision-fenced durable boundary claims and canonical reconcile status.
Claims happen before external effects; uncertain dispatch is not replayed as a
new boundary action.

PUT `/api/v1/rooms/<id>/lock` uses `{revision,locked}` and requires an
authenticated Admin. Enabling captures the server's current reported room
source or Off; callers cannot supply the target. Unavailable or unknown
readback is rejected. A lock may add `desired` (Off or one of the room's
sources; only with `locked: true`, otherwise `invalid_lock`): the source the
caller saw. When the readback, or an existing hold, differs, the answer is 409
`room_state_changed` and nothing is stored or audited, so a remote that chose a
source before locking never captures one the room changed to since. The
persisted `manual_lock` contains `desired` and
`started_at`; status exposes it with `can_lock`, and an active manual hold has
`effective.reason=manual_lock` and no end time. It survives restarts, overrides
scheduled actions, and remains active when the schedule's `enabled` flag is
false. Schedule edits preserve the manual lock. Disabling authentication
suspends both manual and scheduled enforcement without deleting them.
Unlocking always succeeds and releases only the manual hold: a remaining
schedule that no longer validates (for example, it names a retired source) is
kept but disabled for review. Unlocking leaves the current source in place
unless a current schedule applies, and does not replay past daily-off actions.
A lock window already active when a revision takes effect (unlocking into it,
re-enabling authentication, any edit mid-window, a timezone rebase) gets one
catch-up start; the worker sends nothing when the room's Source Item already
equals the target, so a current source's lifecycle actions are not replayed.
Manual locks use the same configured route enforcement and physical-device
limitations as scheduled locks; they do not snapshot arbitrary playback or
volume settings. All ordinary commands, including Admin commands, obey locks.

GET `/api/v1/locks` returns every room's lock in one read, for the room lists:
`{"rooms":{id:{locked,manual,until,reason}}}`, one entry per room of the
compiled house. `until` is a scheduled lock's end, else null; `reason` is
`unlocked`, `room_locked`, `manual_lock` or `policy_unavailable`. One query
reads every room's policy, and each is evaluated as policy status evaluates
`effective`, so a saved active policy that cannot be evaluated is locked with
`policy_unavailable`. Guests, Members and Admins read the same answer; while
authentication is off every room is unlocked. Clients read it only while a
room list is on show, at the policy status interval.

GET `/api/v1/history/rooms` returns each room's confirmed source over the
last 24 hours, the House board's "Rooms through the day" for clients that
cannot reach Grafana: `{"version":1,"start","end","step":60,"labels":{value:
label},"rooms":[{room,label,"segments":[[from,to,value]]}]}`, in Unix
seconds. `end` is the start of the current minute. Each segment is a run of
one value: a source, `off`, or null for Unknown (a room whose devices gave no
readback). A minute in no segment has no data. The compiled contract
(`history`, made by `oh_site_contract.history` from
`oh_metrics.dashboards.room_history`) names the board's own queries, what each
number means and the loopback port of the house's Prometheus (13607, or
`metrics: prometheus_port` in `instance/host.yaml`). No client chooses a
query, range or upstream. One range query reads every room, at most once a minute; clients
asking within the same minute share its answer. The gateway waits 5 s and
reads at most 8 MiB, and a number the contract does not name is Unknown.
Guests, Members and Admins read the same answer, as they read the same rooms'
sources from the event streams. The plain-HTTP Guest listener does not serve
it. When Prometheus does not answer, or the contract has no history, the
answer is 503 `history_unavailable`.

POST `/api/v1/rooms/<id>/commands` takes `{action,value}`. The immutable server
contract resolves the action to a known Item and validates enum/range/step.
A volume's range and step are its device's own scale, as its integration
declares it (`VOLUME`): every value from the device's minimum to its maximum
on a step is accepted and anything else refused; no room, policy or house
setting narrows it, and readback is projected on the same scale.
The commanded room's lock refuses every ordinary command (`room_locked`). A
locked room the contract names as affected refuses only what would disturb
the route it holds (`device_locked`): a source route setting a field that
route needs to another value, or any command to a device it uses. Choosing a
shared source the locked room also shows remains allowed. Another routed room
that owns the commanded device (`own_rooms`: the device is its `audio`, or
its soundbar, streaming amp, audio streamer or TV in its own room,
`oh_features.routing.targets`)
refuses it outright while locked (`device_locked`); a zone over a Building
only shares the rooms' sound inside it.
POST `/api/v1/devices/<id>/commands` takes `{action,value}` from a device's
audio page: `action` is a point of its remote (`volume`, `mute`, `input`,
`key`, `power`) or the Item of one of its page's settings, each validated as
a room's command is (the compiled `devices` contract). The rooms whose
remotes command the device guard it: each that owns it refuses every command
while locked, and each that shares it (a source it shows, a stage) only what
would disturb its held route; both refusals are `device_locked`. Like a
room's commands it needs no account, and the Guest listener serves it. Its
trail line has `room=-` and names `device=<id>`.
A fresh principal/revision is checked immediately before transport.
POST `/api/v1/all-off` with `{}` affects only unlocked authorized rooms and
returns accepted/skipped IDs. It cannot create an override. No public API
allows arbitrary Item names, state PUT, runtime settings/scripts or upstream
URLs. The runtime transport takes a service-owned 0600 bearer file and explicit
loopback origin, disables redirects and environment proxy discovery, and never
returns upstream error bodies to a client.

Read compatibility is limited to known `/rest/items/<item>/state`; there is
no `/rest/events` route. Unknown/private/credential Items are filtered before
payload parsing or output. Pairing statuses/results are reconstructed from
known fields. Free String values fail closed. Site house.js/JSON assets remain
available to Guests through the same finite projection; administrative assets
still require management authority even when marked public in the asset declaration;
all filesystem assets are exact basenames under a configured asset root.
Private pages (every page but Account), house.js, JSON and administrative
assets are `no-store`. Every other file carries an ETag and answers
`If-None-Match` with 304. Generated pages name each non-page file as
`/static/remote/<name>?v=<hash>`; only a request whose sole query is the served
bytes' current hash is `public, max-age=31536000, immutable`, and any other URL,
a stale hash included, is `no-cache`. Those static files, never a no-store
asset or API answer, are gzipped for a client that accepts it.
Pairing metadata comes only from the compiled `pairings` map; its command
Item state is always NULL and is never fetched. Status/result reads are
projected. Only Admin can submit REQUEST/AGAIN/CANCEL or a metadata-validated
code through that finite map; authorization and validation happen before any
room lock is taken (a Guest gets 401, a Member 403). No response/audit echoes
the code. First activation likewise checks the setup code before taking room
locks, and again inside them. Camera
status uses a compiled Group/member allowlist and one bounded Group GET.
Media uses exact compiled native paths with snapshot/SDP kinds; unknown paths,
query strings, outbound media offers and upstream response headers are denied.
Snapshot responses are bounded image bytes, SDP is bounded video-recvonly offer
and answer, and all are no-store. There is no catch-all native REST/media proxy.

Diagnostics states use `kind:json` plus a finite nested `fields` schema
(`object`, `number`, `boolean`, `enum`, bounded `text` with explicit pattern).
Unknown fields, sensitive key/text, controls and malformed values are omitted;
native acquisition timestamps are preserved only when present, never invented.
Explicit nullable fields preserve null; other malformed/missing fields remain
absent. Approved public activity Strings without a semantic recipe are
bounded `text` (192 printable characters with no sensitive word; anything else
is `UNDEF`) marked `free_text`, which Guests may be kept from (`guest_text`).
Use simple bounded character classes generated from provider contracts, not
user-supplied regular expressions. A plain text state likewise requires an
explicit bounded recipe; it is not a raw String passthrough.

Command specs may include `affected_by_value:{value:[roomID,...]}` rather than
a conservative `affected_rooms` union. The compiler supplies `resources` such
as `device:<id>`. Inject `command_lock(rawRoomIDs, prefixedResources)` using
the same lock authority as the worker; wrap raw IDs as `room:<id>`. Dispatch
rechecks user and lock state after acquiring it, and policy PUT acquires the
room lock before its DB transaction. The default no-op is for isolated tests
and uncomposed development only; production composition must supply it.
When the configured site timezone changes, `serve` and `worker` each call
`policy_store.rebase_timezone()` under every room lock before evaluating a
schedule; it revises/retimestamps policies so the worker fences old
occurrences and does not replay prior boundaries.

Every client stream is served from one upstream subscription per gateway
process (`oh_identity.hub`), opened with the first stream and closed a minute
after the last. openHAB 5.2 tests every filter of every subscriber against
each event as a regular expression, so the upstream asks for one wildcard per
verb, `openhab/items/*/<verb>`, instead of hundreds of exact topics; the
gateway discards every topic outside the views before parsing its payload,
projects the rest once, keeps each Item's latest value and fans frames out in
process. Each (re)connection reads every viewed Item's state in one GET
`/rest/items?fields=name,state` once the subscription is open; an event during
that read wins. Reconnection backs off from 1 to 30 seconds. A client that
falls 256 frames behind loses them and is sent a fresh snapshot instead, so
no client grows the gateway's memory; there is no event history. The policy
worker is a separate process and holds its own single wildcard subscription
(`oh_access.worker.OBSERVED`), keeping only its observed Items. Set
connection/concurrency limits at the ASGI/TLS service boundary.

Production cutover still requires owner integration: native REST/HTTPS/ws and
alternate UIs must not bypass this boundary, implicit user role must be off,
trusted networks must not grant anonymous access, and internal helpers must use
their own credentials. **Preserve actual Sonos/UPnP callback delivery.** Binding
the native server to loopback alone may break callbacks advertised on LAN:13602.
The deployment owner must verify the pinned callback route/port and retain an
exact method/device-origin callback relay or supported separate listener.
Host networking alone does not establish this. The identity core implements
no unverified callback route, firewall, root service or native listener change.

## Music

A host with `music:` in `instance/host.yaml` (`oh_music`) runs a pinned
Music Assistant that plays TIDAL and the host's music libraries on the
house's Sonos and WiiM speakers; the gateway is its only client and serves
it to thermite's pages as a small typed API. The contract's `music` names
Music Assistant's loopback `port`, the `libraries` and the `speakers`: each
Sonos with a `sonos_udn` and each WiiM under `wiim_http` control with a MAC
or an address, as `{label, room, room_label, provider, udn | mac, ip,
rooms}`, where `rooms` are the routed rooms that own or share it, as for a
device's commands. A speaker is matched to Music Assistant's player by its
UDN (Sonos) or the MAC the WiiM reports, else its address; any other player,
and a WiiM's hidden AirPlay protocol player, is never shown or commanded. A
host without music answers 404 `music_not_configured`.

The policy worker holds the claim: once Music Assistant reports running and
has no user, it stores a random password in `music-token.json` (mode 0600,
written atomically, its directory synced) in the gateway's private state,
creates the admin `thermite` with it, and stores the setup session it is
answered with beside it: a pending claim that a later pass resumes from
however this one ends. When the answer to the setup was lost, Music
Assistant has a user and thermite its password, so a later pass signs in
with it rather than calling the server someone else's. With the session it
mints a long-lived token (a year), which replaces the session and the
password in the file, and revokes the session; within 30 days of a token's
end it mints, stores and switches to the next and revokes the old one.
Serving only reads that file. The worker never gives up a stored token or
session because Music Assistant refuses it, as Music Assistant does for a
moment while it starts, stops or upgrades: it keeps it, counts the passes in
a row that reached Music Assistant and were refused (one that finds it down
counts none, and the clock plays no part), and tries it again on every pass,
every 30 seconds, while also claiming Music Assistant afresh should it have
no user, or signing in with a pending claim's password. The first pass that
works clears the count, and music is served again. After a day's refused
passes the gateway answers `music_unclaimed`; until then
`music_unavailable`. A Music Assistant that had another user before
thermite claimed it is `music_unclaimed` at once. Either recovers when the
owner resets Music Assistant (stop it, delete its data directory and start
it again): the worker claims it afresh within 30 seconds. A claim file the
worker cannot trust (not the service account's, or not mode 0600) is
reported, never replaced, and the gateway answers `music_unavailable` until
it is repaired. After
each claim, whenever Music Assistant comes back with a new server ID (its
state was reset) and every 15 minutes, the worker also brings Music
Assistant's configuration to what thermite needs: the
`sonos`, `wiim` and `airplay` player providers, with the speakers' addresses
for discovery; one `filesystem_local` instance per library on
`/media/<name>`, named `thermite <name>` (Music Assistant does not report an
instance's path), and, once each library has its instance, no other; none
of the providers that find other speaker families or act on their own
(`chromecast`, `dlna`, `heos`, `bluesound`, `party`, `smart_fades`,
`lastfm_recommendations`, `ambient_sounds`); remote access off; and
onboarding complete. It never renames or disables a player. Each of those
parts is applied alone: one that Music Assistant fails is logged and tried
again at the next reconcile, 15 minutes on, and the rest still apply. The
worker names each library's new instance, then records its ID in
`music-libraries.json` (mode 0600), so that an instance whose naming failed
is named again, and one whose record was not written is found by its name,
rather than removed, added again and rescanned. That record is trusted as
the claim file is: while it is not the service account's, or not mode
0600, the worker warns once and leaves the libraries as they are, adding
and removing none.

Routes (`<speaker>` is a contract speaker ID):

| Route | Answer |
|---|---|
| GET `/api/v1/music` | `{version: 1, available, problem, sources: {tidal, libraries: {name: bool}}, speakers: [speaker]}` |
| GET `/api/v1/music/search?q=&kind=&source=` | `{query, kind, source, artists, albums, tracks, playlists}`: items, 25 a kind and source |
| GET `/api/v1/music/browse?ref=&offset=&limit=` | `{ref, offset, limit, total, items}`; without `ref`, each library's and TIDAL's root folder |
| GET `/api/v1/music/<speaker>/queue?offset=&limit=` | `{speaker, queue, offset, limit, total, current, items}` |
| POST `/api/v1/music/<speaker>/play` `{ref, option?}` | 202 `{accepted, speaker, action, queue}` |
| POST `/api/v1/music/<speaker>/transport` `{action, value?}` | 202 `{accepted, speaker, action, queue}` |
| POST `/api/v1/music/<speaker>/volume` `{level}` or `{group_level}` | 202 `{accepted, speaker, action, level}` |
| POST `/api/v1/music/<speaker>/group` `{members}` | 200 `{accepted, speaker, joined, refused}` |
| POST `/api/v1/music/<speaker>/ungroup` `{}` | 202 `{accepted, speaker, action}` |
| GET `/api/v1/music/art/<id>?size=` | the cover as JPEG or PNG |
| GET `/api/v1/music/tidal`, POST `…/tidal/start`, `…/tidal/finish` `{}` | Admin: `{signed_in, sign_in}` |
| GET `/api/v1/events/music` | the music event stream |

A speaker is `{id, label, room, room_label, provider, available, state,
volume, muted, group, now_playing, shuffle, repeat}`: `provider` is `sonos`
or `wiim` (a page explains by it a grouping Music Assistant refused);
`state` is `playing`, `paused`, `idle` or `unavailable`; `volume` 0–100;
`group` null or `{leader, members, volume}` (Music Assistant reports a
group's loudest member);
`now_playing` null or `{duration, elapsed, at, title, artist, album, art}`,
where `elapsed` seconds were measured at epoch `at`; `repeat` is `off`,
`one` or `all`. An item is `{ref, kind, name, source, library?, artists,
album, year, duration, art}` (a folder carries only the first five);
`kind` is `track`, `album`, `artist`, `playlist` or `folder`, and a queue's
items add their `index`. `ref` is the reference to play or browse, a
Music Assistant URI of TIDAL's, a library's or Music Assistant's own
library (`<provider>://<kind>/<id>`, at most 1024 printable characters),
whose ID is one that provider gives: a number for Music Assistant's
library; letters, digits, `-` and `_` for TIDAL (its folders, up to eight
such words joined by `/`); a path relative to the library, without an
empty, `.` or `..` part, for a library. Any other, or a provider instance
Music Assistant does not have, is 400 `invalid_ref`. `art` is a 64-hex image ID; `size` is 80,
160, 256 (the default) or 512, and the image is cached privately for a day.
`option` is `replace` (the default), `next` or `add`; `action` is `play`,
`pause`, `stop`, `next`, `previous`, `seek` (`value`: whole seconds),
`shuffle` (`value`: boolean) or `repeat` (`value`: `off`, `one` or `all`).
`q` is cut to 100 characters without controls; `kind` is one item kind,
`source` `library` or `tidal`; a page is at most 200 items (100 by default)
from an offset of at most 100000. Unknown fields or query parameters are 400
`invalid_command` or `invalid_query`.

A group member plays its leader's queue (Music Assistant would take a member
it plays to out of its group), so play, transport and queue on a member
reach its leader's queue, which `queue` names. Grouping asks Music Assistant
to join the members and reads the group back for up to 3 seconds: Music
Assistant drops a member it cannot group (a WiiM without AirPlay with a
Sonos) without an error, so `refused` lists it, as it does a speaker with no
available player.

Commands keep a device command's rules (`RuntimeAdapter.device_guard`,
which `dispatch_device` uses too): any principal sends them; each routed
room that owns an involved speaker refuses them while locked, and a room
that shares one refuses what disturbs its held route, both 423
`device_locked`. Playing to, transport on or the group volume of a group
involves each of its speakers; grouping, every speaker whose group it
changes: the speaker's group, each member, and each member's group, which
it leaves. The gateway reads the groups, takes the involved rooms' locks,
then reads the groups again: when a group changed meanwhile to involve
another speaker, it takes the locks again for both. Each command whose body
is valid is one `cmd` trail line however it ends (a refusal, Music
Assistant down or failing), `device=<speaker> action=music.<action>` with
the option, the transport value, the level or the members as its value,
never a reference, a query or a title; one refused for its body (400, 404)
is none, a play whose reference names a provider instance Music Assistant
does not have included. Music Assistant has 5 seconds to answer a command or a read and
20 for a search or a browse; reading a group back takes 3 seconds at most,
its reads included. Titles, names and art are free text: a Guest who may
not see it gets speakers without them and 401 `login_required` for search,
browse, queues and art.

The music event stream is admitted, revalidated and heartbeated as the other
streams. Its first frame is `{"type":"snapshot","view":"music","music":
...}`, GET `/api/v1/music`'s answer; then `{"type":"speaker","speaker":
...}` whenever a speaker a client sees changes, a fresh snapshot when Music
Assistant's providers change, and `{"type":"unavailable"}` when the
connection to Music Assistant fails (it is down, starting, not claimed, or
refuses the token), then a snapshot once it serves again. A stream that
joins while it is down, or that is still waiting for its first snapshot
when it fails, gets `{"type":"unavailable"}` at once. One Music Assistant
websocket per gateway process (`oh_identity.music_hub`) serves every music
stream, on `oh_identity.hub`'s terms.

TIDAL signs in with its device flow: POST `/api/v1/music/tidal/start` (or
`thermite access music-tidal --config FILE`, run as the service account)
answers `sign_in: {state: "waiting", code, url, expires_at}`; the owner
approves the code at `https://link.tidal.com/` from any device, and GET
`/api/v1/music/tidal` follows it (`waiting` with a fresh code once one
expires, then `done` or `failed`). POST `…/tidal/finish` ends it, cancelling
a sign-in still waiting; without one it is 409 `tidal_not_started`. Signing
in again re-authenticates the same TIDAL instance.

| Problem | Status | Meaning |
|---|---|---|
| `music_not_configured` | 404 | The host has no music. |
| `unknown_speaker` | 404 | No such speaker in the contract. |
| `invalid_ref`, `invalid_query`, `invalid_command` | 400 | A reference, query or body out of bounds. |
| `device_locked` | 423 | A locked room owns, or holds a route through, an involved speaker. |
| `music_failed` | 502 | Music Assistant refused or failed it, or has no available player for the speaker. |
| `music_unavailable` | 503 | Music Assistant is down, starting, not claimed yet, or refuses the token. |
| `music_unclaimed` | 503 | Music Assistant belongs to another user, or has refused thermite's token for a day's passes: reset it. |
| `tidal_not_started` | 409 | No TIDAL sign-in to finish. |

GET `/api/v1/music` answers 200 with `available: false` and the problem
(`music_unavailable`, `music_unclaimed` or `music_failed`) instead of
failing, so a page can show its speakers.

## Testing

Run `make check` from the repository root for the portable application tests.
The identity suite additionally exercises real Django, SQLite and Argon2 with
synthetic accounts and in-memory runtime adapters. Install the hash-locked
`lib/requirements-site.txt` in a development virtual environment, then run:

```sh
mkdir -p .scratch/identity-tests
chmod 700 .scratch/identity-tests
PYTHONPATH=lib DJANGO_SETTINGS_MODULE=oh_identity.test_settings \
  THERMITE_IDENTITY_STATE="$PWD/.scratch/identity-tests" \
  python -B -m django test oh_identity --noinput
```

Use a fresh scratch directory for this test state. The suite never uses a
production identity database or contacts a native runtime or device.
