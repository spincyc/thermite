# Application releases and private sites

Install the application separately from your private site's configuration and
state. Each site pins an exact application release. These development builds
are currently `0.1.0.dev0`. Deployment uses an explicit private site;
application source checkouts remain available for development and packaging.
For a first installation, use the [server guide](installation.md) or
[standalone ISO](iso.md).

## Package the application

From source, run `python3 bin/thermite package --out .scratch/releases`.
The builder creates a wheel with ordinary Python metadata and the
`thermite` console entry point. It includes the public program, documentation
and fictional example; it excludes private inventory, credentials and runtime
state. The public source also supports `python -m pip install .`.

Use a dedicated virtual environment outside the private site. PyYAML is the
application's configuration dependency. Node.js, the locked terminal package,
native runtime downloads and service dependencies are prepared by the
installation workflow.
The server guide provides the distribution-specific prerequisite commands;
do not install the application into the OS-managed Python environment.
For an offline wheel installation, supply PyYAML first, then run
`python -m pip install --no-index --no-deps /path/to/thermite_app.whl`.

## Initialize a site

```sh
thermite init /path/to/private-site
thermite check --config /path/to/private-site
```

Init requires a new or empty directory. It writes a fictional inventory,
sample host settings, runtime pins, ignore rules and
`thermite-site.json`. Configuration files are mode 0600. It rejects symlinks,
application overlap and existing user files. Replace the example before
commissioning hardware. Init creates no account or service and does not
initialize or publish Git.

Run `thermite install --config /path/to/private-site` from any directory.
Use `--config DIR` to select a site; `--site DIR` remains an alias.
Access service commands retain `--config FILE` for their service descriptor;
use `--site DIR` when selecting a site for those commands.
`THERMITE_SITE` or working inside the site also selects it. The application
command comes from its installed environment, outside the private repository.
New sites contain no launcher or Makefile. Keep the application environment
outside disposable source workspaces.
Help, themes, documentation, packaging and ISO builds work without a site;
commands that create configuration or state require one. Install and upgrade
require a site manifest even when launched from an application source checkout;
they do not convert a mixed source/configuration tree automatically.

The application supplies integrations, templates, web assets, terminal source,
themes and dependency locks. The private site owns its `instance/` data
(inventory, host settings, custom themes and IR profiles), native runtime pins
in `versions.json`, and its application pin in `thermite-site.json`. Commit
these inputs in private Git. The generated `conf/`, `metrics/` and `deploy/`
trees are ignored local build output, alongside `var/` and `build/`.
Application JavaScript changes therefore require no generated-code commit in
the private repository.

### Use a fresh clone on an existing host

Run the same `thermite install --config /path/to/private-site` command.
Install selects its current application release, prepares its private owner
environment, and reconciles the site's application requirement under the one
approval. When the release already matches, it makes no duplicate pin commit.
It recreates a missing application cache from the verified release, reconnects
the native services and adopts the installed gateway's identity and TLS state.
To retain a specific historical application, launch that exact verified release;
to move the whole installation to current official source, use
`thermite upgrade --config /path/to/private-site`.

Git does not copy private `var/` state. If this host already runs the site and
the clone lacks its runtime token, installation can reconnect using the
service's saved monitoring token. Under the normal install approval, it
validates that token against the loopback runtime and saves a private operator
copy. It does not replace an existing local token or managed credential record.
Keep private backups of `var/` for other state and historical releases. Restore
`var/gateway/authority/` into the clone before installing: without it, the run
creates a new certificate authority and every client must trust it again.

New sites use manifest schema 2. Deployment exports the selected Git revision's
inputs and renders them with that revision's exact verified application from
`var/application-releases/`, or the matching installed release when the cache
is absent. It uses a temporary private directory and an
isolated Python subprocess; it never imports site `lib/` or uses working-tree
inventory, generated files or environment overrides as deployment authority.
Application source directories in a schema-2 site are refused. Planning may
create and clean a temporary render directory, including in dry-run, but
materializes no persistent generated tree and changes no service.
Production installation still requires clean committed inputs and a boot test
of the exact derived configuration.

The private configuration stays owned by the operator. Installed
application services use the non-login `thermite:thermite` account, UID/GID
9001, and their deployed configuration and state belong to that account.
The installer sets up trusted operator group access; see
[service ownership and migration](service-account.md).

## Update and roll back

The normal commands are:

```sh
thermite install --config /path/to/private-site
thermite upgrade --config /path/to/private-site --dry-run
thermite upgrade --config /path/to/private-site
```

Install deploys the current application release. Upgrade fetches the official
`https://github.com/spincyc/thermite.git` repository's `main` branch after the
owner approves the run. To select a reviewed local checkout or another Git
revision explicitly:

```sh
thermite upgrade --config /path/to/private-site --source /path/to/thermite
thermite upgrade --config /path/to/private-site --revision REVIEWED_COMMIT
```

With `--source`, the default revision is that clean checkout's HEAD; combine
it with `--revision` to select one of its other commits or tags. Source
`make upgrade CONFIG=/path/to/private-site` is only an alias for the same public
upgrade command, with the same official-source default. It maintains no
separate lifecycle. No application checkout is required for ordinary upgrades.

### What the one command does

Before mutation, the workflow requires a clean private repository and any
explicit source checkout, verifies the owner command can be replaced safely,
and asks once to approve the entire run. It fetches the selected source when
needed, prepares or reuses a private application environment under
`~/.local/share/thermite/application/`, and verifies every installed release
file. Python package installation happens inside that environment, from the
hashed `lib/requirements-app.txt`, never in the OS-managed interpreter. An
environment built for another Python minor version is rebuilt; the old one is
retained. A dry run performs no fetch, environment creation,
site update or service change; the remote revision remains unknown until the
approved fetch.

The candidate validates the private site before committing its exact
application version and release digest in `thermite-site.json`. The same
bounded transaction may update ignore rules. Inventory, runtime pins and
unrelated Git files remain untouched. Nothing is pushed to the site's remote
repository. Pinning a release older than the site's committed one is refused
unless `--allow-downgrade` is given after deliberate state recovery.

The candidate then runs the complete native and access workflow: boot checks,
deployment and service readiness, gateway staging, schema migration,
activation, native authentication and callback configuration, and live HTTPS
verification. An upgrade runs it as one install transaction that sets the
installed version aside first and either commits the new one or puts the
installed one back in the same run; see
[how an upgrade runs](installation.md#how-an-upgrade-runs).
Existing identity, security settings, certificates and callback routes are
adopted. The gateway's hosts, listening address and timezone come from the
host on every run, and a certificate whose names no longer match is reissued
by the same authority. A fresh gateway has authentication off until owner-authorized first
administrator setup on Security; it prints its HTTPS URL and the path of its
private certificate authority's certificate, which each client trusts once.
The authority lives in the site's `var/gateway/authority/`; see
[HTTPS and accounts](installation.md#set-up-https-and-accounts).

Only a successful complete run, for an upgrade a committed transaction,
selects the candidate through `~/.local/bin/thermite`. Keep that directory first on PATH; a successful run
warns when `thermite` on PATH is a different launcher. A recognized standard
pip-installed owner entry point is backed up before adoption; a custom command
is not overwritten. The one-time application bootstrap in the
[server guide](installation.md) is the only normal pip/environment setup.
Upgrades need no manual pip, `init --upgrade`, site-pin commit or access-release
commands.

Application-only changes also deploy and restart the runtime when its shared
script libraries need reloading. Native component versions still come from the
private site's committed `versions.json`; fetching newer application code does
not silently change them.

### Resume or recover

Validation or commit failures restore only the upgrade's bounded site edits
when those files have not changed concurrently. Concurrent edits are preserved
for review. An upgrade's host phase that ends without a commit says what
happened and keeps the previous owner command selected: refused before its
install transaction began (nothing of Thermite changed), rolled back (the
version that ran before runs again), or stuck or interrupted (the next
`thermite upgrade` resolves the transaction first). Once nothing began, or
the rollback finished, it undoes the site's pin commit, so the site pins the
version that runs; the candidate environment stays cached for the next run. See
[when an upgrade fails](installation.md#when-an-upgrade-fails) for the exit
statuses.

An install's host phase that fails keeps the committed candidate pin and
candidate environment, while the previous owner command stays selected. Rerun
the printed Resume command, which names the fetched commit with `--revision`:
it revalidates and reuses the candidate without a duplicate pin commit.
Meanwhile the previous owner command runs site commands with the site's pinned
release, verified from `var/application-releases/<digest>`, instead of
refusing. A changed or damaged retained release is refused. `--dry-run` shows
a provisional host plan only when the site pins the release that is running
it.

To go back to an older release after an upgrade committed, revert the site's
release-pin commit, then run that release's cached
`var/application-releases/<digest>/bin/thermite install --config DIR`. Before
selecting an older gateway, establish that it supports the current identity
schema. Do not bootstrap a new authority, reset accounts or replace
credentials to retry.

Verified application releases are also cached under
`var/application-releases/`. Keep that cache, `var/gateway/authority/`,
previous application environments, gateway releases and original artifacts. A historical deployment resolves the
private Git revision's exact application requirement, renders configuration
with that release and copies its finite helper files. A missing cache can use
the exact installed release; unavailable or modified artifacts fail before
deployment writes. Rollback does not download a substitute automatically.

### Legacy sites

Historical schema-1 site revisions retain their committed generated trees and
exact cached helper application. Historical complete-checkout revisions retain
their committed source and generated configuration. Their rollback format is
not silently reinterpreted as schema 2.

The unified install and upgrade commands require a private site manifest.
They refuse a nonempty mixed source/configuration checkout without converting
or overwriting it. Converting such a checkout requires reviewed private Git
changes after the application has been bootstrapped separately. Preserve its
ignored `var/`, existing native state paths, ports, service identity and access
identity/TLS state. Init accepts only a new or empty directory; it is not an
in-place conversion command. Packaging does not relocate production data or
service units. Program integrity assumes a trusted Python interpreter and
installed bootstrap; it does not protect against an administrator changing them.

## Offline verification

The packaging tests build deterministic wheels through both the package API
and public PEP517 entry point. They install outside the checkout with network
calls blocked, initialize a separate site, generate and validate, and confirm
that application resources remain unchanged. Two private fixture revisions
exercise exact helper rollback and reject a tampered old release before writes.
ISO tests additionally check installer confirmation settings and the bundled
application bootstrap into a synthetic filesystem. The Arch installer uses
online signed OS packages; the Ubuntu variant also supports offline OS
installation. These tests start no runtime or device operation. Actual
machine installation and commissioning for each path have separate
acceptance checks; see the [review baseline](requirements.md#basis-and-review).
