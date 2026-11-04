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

GET `/api/v1/security` exposes only mode, revision, setup availability and CSRF
data. POST uses `{enabled,revision,password}`; first activation additionally
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
`locks`, room commands and All Off) are found; and a POST needs one of the
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
then a fresh snapshot once it is back. A stream checks the user,
session/token existence, expiry and revision when it opens, at most once per
second while events flow, and at every quiet-stream heartbeat (5 s), and ends
when one fails. Each peer bucket may hold 8 streams (a page each, a terminal
two while it pairs, and a reload's old stream until its next heartbeat) and
the gateway 48, under half of its 100 in-flight requests; more answer 429
`too_many_streams` with `Retry-After`. Observers in `oh_identity.hub`
(`observers`) see each stream open, close (with its seconds and reason) and
refusal, keyed by view, origin (`https` or the `guest` listener), principal
kind, HTTP version and peer digest; `EventHub.status()` counts streams per
view and the upstream's connections, failures and resyncs.

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
shared source the locked room also shows remains allowed.
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
absent. Approved public activity Strings without a semantic recipe use a stable
Item-scoped, per-site HMAC (`hidden:` prefix), preserving change detection
without exposing plaintext or an offline-guessable raw value hash.
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
the native server to loopback alone may break callbacks advertised on LAN:13601.
The deployment owner must verify the pinned callback route/port and retain an
exact method/device-origin callback relay or supported separate listener.
Host networking alone does not establish this. The identity core implements
no unverified callback route, firewall, root service or native listener change.

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
