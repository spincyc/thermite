# Site access services

`thermite access stage` copies the program and every remote asset into a new
persistent private release, compiles authority from the validated house and
native providers, installs the hash-pinned site environment, and writes two
systemd units. It never installs or starts those units. No service path points
back into the source checkout. Use `--wheelhouse` for an offline installation.

Pass a separate persistent `--state` directory and reuse it for every release
of the same site. Releases share that directory's site identity, signing key,
users, policies, durable claims and locks. Staging validates custody but does
not migrate SQLite or initialize authority. Bootstrap only a genuinely new
site; existing-site schema migrations require an explicit backed-up upgrade.

For a new site, the owner then runs that release's `thermite access bootstrap --config …
--user …` locally. Both password prompts are hidden and require a terminal;
there is no default password or remote bootstrap. Existing identities cannot
be replaced by running bootstrap again. The state directory is mode 0700;
the database, secret, identity, runtime bearer and configuration are mode 0600.

`serve --config …` loads a private compiled contract and explicit host list,
validates the TLS certificate/key, and runs one direct-TLS ASGI process. It
does not trust proxy headers, environment HTTP proxies, redirects, cookies
from the native runtime, or arbitrary caller-supplied upstream URLs. The
separate `worker --config …` uses the same SQLite database and sorted
`room:<id>` / `device:<id>` file locks. One process lease prevents a second
worker. No database transaction spans device I/O.

The worker observes only exact native `ItemStateUpdatedEvent` topics for
compiled route fields. It starts Unknown, clears observations after any
stream disconnect, and never obtains observation timestamps from REST reads
or command acknowledgements. Apple TV power additionally requires its
provider's acquisition timestamp before the paired power update. A missing,
stale or invalid timestamp stays Unknown; native drift can trigger only the
engine's bounded mismatch repairs. Source boundary commands remain durably
claimed once, with uncertain outcomes preserved after transport failure.

Terminal clients use `access login --url https://… --user …`. Their personal
token is stored atomically in an owned mode-0600 regular file under
`${XDG_CONFIG_HOME:-~/.config}/thermite/sites/<sha256-origin>.json`. Its five
fields are `version`, `origin`, `token`, `id`, and `expires_at`. A private CA
path, when supplied, lives separately in the matching `.ca.json`; the remote
launcher gives Node that trust path before startup. Tokens and passwords are
never command arguments, environment values or normal output. `logout`
revokes first and removes only the unchanged credential file.

## Optional device callbacks

`stage --callbacks <private-file>` validates and copies an explicit callback
configuration and stages a third unit. Without it, the release still has only
the HTTPS service and policy worker. No unit is installed or started.
`thermite access callbacks --config <private-file>` runs that separate legacy
plaintext UPnP ingress; it has no credential or site-state input.

The mode-0600 JSON has exactly `version: 1`, `routes`, `upstream`, `hosts`,
`bind_host` and `port`. `routes` maps each verified native callback path to
its publishing Sonos peer IPv4 address. The bind address is one explicit LAN
IPv4 address; `hosts` contains exactly that address and port. The upstream
must match the site's fixed loopback native HTTP origin. There are no wildcard
paths, browser endpoints, REST proxying or automatic device grants.

The relay accepts only bounded, validated NOTIFY events. It rejects cookies,
authorization, redirects, ambiguous framing and DTD/entity declarations;
it forwards no upstream response content or credentials. Its pinned h11
server bounds connections, initial-header time, body time and upstream time.
The native runtime retains subscription SID/sequence authority.

Before activation, verify the actual native SUBSCRIBE callback address,
port and path, then a notification that changes the native Item. jUPnP's
`httpProxyPort` changes the advertised port; it does not create this listener.
An HTTP 200 alone does not prove a state update or callback preservation.

## Activation boundary

The staged receipt explicitly says `security_boundary_active: false`.
Before owner activation, seal every bypassing native HTTP, HTTPS, websocket
and alternate UI listener, disable implicit roles and trusted-network grants,
and verify client TLS trust. Preserve Sonos/UPnP callbacks during this change:
loopback binding alone can break their LAN callback URLs. This package does
not enable an unverified callback relay. The owner must validate the exact
native callback path and either its supported listener/proxy setting or an
independently allowlisted relay before changing listeners.

Only after that checklist is satisfied should the owner install the
staged unit files and enable the services. Staging, a responsive login page,
or a working authenticated client alone does not establish that direct native
access has been sealed. Existing hardware remotes and device protocols can
still cause drift; room locks provide bounded reconciliation, not a physical
interlock. Passive endpoints remain explicitly unverifiable.
