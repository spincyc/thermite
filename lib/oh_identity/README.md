# thermite identity core

This package is a server boundary, not an assertion that the current native
runtime listener is sealed. It contains no production account or credential.
Python 3.11 or newer is required. Install `lib/requirements-site.txt` with
`pip --require-hashes` in the service's isolated environment.

The owner CLI calls `composition.initialize(state_directory, username,
password, allowed_hosts)` once, supplying the password from a hidden terminal
prompt. It creates/migrates the database and atomically creates the first
Admin. There is no public bootstrap endpoint. Further starts call
`composition.setup(...)`, install a `runtime.RuntimeAdapter` through
`install_adapter()`, then run `oh_identity.asgi:application` with a production
ASGI server. No default state path, account, password or upstream token exists.

The service-owned state directory is 0700; database, site UUID and secret key
are 0600, with umask 077 also protecting SQLite side files. An immutable site
UUID makes session and CSRF cookie names distinct between deployments on one
hostname. Do not share one identity database between sites. Use validated TLS,
an explicit host allowlist, and a trusted local proxy configuration. Disable
access/request body logging; authentication and pairing secrets never belong
in URLs or logs. No CORS or arbitrary proxy endpoint is provided.

Browser login: GET `/api/v1/session` returns a CSRF token and secure CSRF cookie;
POST the same endpoint with JSON `{username,password}` and `X-CSRFToken` from
the same origin. It creates a Secure/HttpOnly/SameSite session. POST logout is
CSRF protected. For TUI, POST JSON `/api/v1/token-login` with
`{username,password,name,days}` without Cookie or Origin headers; this distinct
password-authenticated channel issues a personal token and creates no browser
cookie. It rejects browser Origin/cookie use and non-JSON bodies. Use
the `Authorization` header with the `Bearer` scheme for subsequent TUI requests, including SSE;
never put the token in a query string. A token cannot mint another token.

GET `/api/v1/me` returns the site ID, current role ID, canonical permissions,
room grants and can_edit. User controls unlocked granted rooms, Viewer reads,
Admin manages users, roles and policies. GET/POST `/api/v1/users` and
PATCH/DELETE `/api/v1/users/<id>` administer accounts; DELETE deactivates.
Reserved Admin/User/Viewer roles cannot be edited/deleted. Custom roles can
grant site.read/room.control; admin management capabilities remain reserved.
Role edits and user mutations revoke prior authorization revisions. Concurrent
last-Admin deactivation/demotion is serialized by SQLite IMMEDIATE transactions.

Personal token GET/POST `/api/v1/tokens`, DELETE `/api/v1/tokens/<uuid>` expose
metadata only except the raw token's one issuance response. Tokens expire in
1–90 days, store a digest, and are checked against the current active user and
authorization revision. Token logout revokes that token. SSE checks the user,
session/token existence, expiry and revision before each event and on a bounded
quiet-stream heartbeat; cancellation closes the upstream iterator.
Commands and admin mutations revalidate the originating session/PAT after lock
or transaction waits, so a queued request cannot outlive credential revocation.

GET/PUT `/api/v1/rooms/<id>/policy` uses `{revision,config}`. PUT is Admin-only,
CAS protected and delegates validation to the policy engine. Room, timezone
and new revision are supplied by the server. Missing engine with a saved
enabled policy denies control; it never silently unlocks. The separate worker
uses `policy_store.Store` for revision-fenced durable boundary claims and
canonical reconcile status. Claims happen before external effects; uncertain
dispatch is not replayed as a new boundary action.

POST `/api/v1/rooms/<id>/commands` takes `{action,value}`. The immutable server
contract resolves the action to a known Item and validates enum/range/step.
Every affected room, not just the caller's room, is checked against grants and
locks. A fresh principal/revision is checked immediately before transport.
POST `/api/v1/all-off` with `{}` affects only unlocked authorized rooms and
returns accepted/skipped IDs. It cannot create an override. No public API
allows arbitrary Item names, state PUT, runtime settings/scripts or upstream
URLs. The runtime transport takes a service-owned 0600 bearer file and explicit
loopback origin, disables redirects and environment proxy discovery, and never
returns upstream error bodies to a client.

Read compatibility is limited to known `/rest/items/<item>/state` and finite
`/rest/events?topics=...`. Unknown/private/credential Items are filtered before
payload parsing or output. Pairing statuses/results are reconstructed from
known fields. Free String values fail closed. Private house.js/JSON assets
require auth even when accidentally marked public in the asset declaration;
all filesystem assets are exact basenames under a configured asset root.
Pairing metadata comes only from the compiled `pairings` map; its command
Item state is always NULL and is never fetched. Status/result reads are
projected. Only Admin can submit REQUEST/AGAIN/CANCEL or a metadata-validated
code through that finite map. No response/audit echoes the code. Camera
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
When a site timezone changes, call `policy_store.rebase_timezone()` under the
settings mutation lock; it revises/retimestamps policies so the worker fences
old occurrences and does not replay prior boundaries.

Native SSE suffix compaction uses exact public Item wildcards only when all
three verbs are approved, never pairing credential families. Complete exact
topics split into at most four upstream URL batches of at most 6000 bytes;
oversized topics or a fifth batch fail closed, never truncate. One shared HTTP
client feeds a 64-entry queue with backpressure; projection happens before the
queue. A closed/failed shard cancels all shards, and disconnect/revocation
closes their HTTP contexts. The compiler/integration checks must prove complete
topic coverage with no extra Items. Set connection/concurrency limits at the ASGI/TLS
service boundary; this core has no unbounded event history or fanout queue.

Production cutover still requires owner integration: native REST/HTTPS/ws and
alternate UIs must not bypass this boundary, implicit user role must be off,
trusted networks must not grant anonymous access, and internal helpers must use
their own credentials. **Preserve actual Sonos/UPnP callback delivery.** Binding
the native server to loopback alone may break callbacks advertised on LAN:8080.
The deployment owner must verify the pinned callback route/port and retain an
exact method/device-origin callback relay or supported separate listener.
Host networking alone does not establish this. The identity core implements
no unverified callback route, firewall, root service or native listener change.

## Testing

Run `make check` from the repository root for the shipped portable
configuration, documentation and offline smoke checks. Full identity
development fixtures and their Django test settings are not included
in this portable source export.
