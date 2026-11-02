# Application releases and private sites

Thermite supports both an existing complete source checkout and an installed
application with a separate private site. These remain development builds,
versioned `0.1.0.dev0`. Installation does not start a runtime, discover devices,
configure credentials, enable accounts, or change a house.

## Install and initialize

Build a wheel from a complete source checkout with
`thermite package --out .scratch/releases`. The builder first creates the
reviewed public artifact and runs its privacy gate. It packages that explicit
program list and fictional example, never the real inventory or generated
configuration. The wheel has ordinary Python metadata and a `thermite` console
entry point. PyYAML is its configuration dependency; Node.js, the locked
terminal package, native runtime downloads and optional service dependencies
remain separate explicit setup steps.

The public export also supports `python -m pip install .`. Use a dedicated
virtual environment, outside the private site. To install a previously built
wheel without accessing a package index, install its PyYAML dependency first
and use `python -m pip install --no-index --no-deps /path/to/thermite_app.whl`.

```sh
thermite init /path/to/private-site
thermite --site /path/to/private-site generate
thermite --site /path/to/private-site validate
```

`init` requires a new or empty directory and writes a fictional inventory,
sample host settings, runtime pins, a private launcher, a small Makefile and
`thermite-site.json`. The directory is private; configuration files are mode 0600.
It rejects symlinks, overlap with the installed program and existing user
files. Edit the sample before commissioning real hardware. Authentication is
the example's default; init never changes an existing site's explicit mode.

`--site DIR` or `THERMITE_SITE` selects the private site. An installed command
also recognizes a site manifest in its current directory or an ancestor.
Without a site, help, theme and documentation commands work, while commands
that would create configuration or state refuse. Complete source checkouts
without a site manifest retain their existing paths and behavior.

Program resources are read-only: integrations, templates, web assets, terminal
source, themes and dependency locks come from the installed application.
Inventory, custom themes and IR profiles, generated `conf/`, `metrics/`,
`deploy/`, caches and runtime state belong to the selected site. The terminal
receives the generated site's house model explicitly. It never silently uses
the installed example after generation.

## Private Git deployment and application upgrades

A separate site still needs private Git for production installation and
deployment. Init does not create, commit or publish a repository. Commit the
site manifest, real configuration, pins, launcher, Makefile and generated
outputs only to that private repository. `var/` and `build/` remain ignored.
Existing clean-commit, generated-output, native boot, pairing-log protection
and owner-controlled installation gates remain in place. Installing a wheel
does not bypass any of them.

The site manifest pins both an application version and the digest of a release
manifest. A version match alone is insufficient. A release lists every
program file and its SHA256; changed, missing, extra or symlinked program
files are rejected. The installer caches complete verified releases below
the site's `var/application-releases/`, outside Git, and never overwrites a
prior release. Keep that cache for rollback.

Ordinary installer-created bytecode caches are accepted only as regular cache
files mapped to listed Python sources. The trusted installed and source
launchers select a fresh, never-created cache prefix before importing program
modules, and disable bytecode writes. Existing caches are therefore inert;
cache symlinks, unknown cache files and nested cache directories are rejected.
The Python interpreter and installed bootstrap remain trusted. This integrity
check is not a sandbox against an adversary who controls those components.

Upgrade the application in its virtual environment, then explicitly run
`thermite init --upgrade /path/to/private-site`. This caches the new release
and changes the site's application requirement and generated launcher only.
It preserves inventory, host settings, runtime pins, generated outputs,
credentials, data and older releases. A customized launcher is refused for
manual review. Internal symlinks in the launcher's or release cache's paths
are refused before any upgrade write. Generate and validate again, commit the new private revision,
then use the normal owner-controlled install/upgrade/deploy workflow.

For a separate site, deployment takes `conf/`, `metrics/` and the application
lock from the selected private Git revision. It copies only the finite helper
and exporter program files from that revision's verified release into the
existing production locations. A rollback resolves the older revision's exact
cached release; it does not substitute whatever package is installed now.
Missing or modified rollback artifacts fail before configuration or helper
writes. Deployment never downloads a missing release automatically.

An original complete Git checkout continues to deploy its own committed
configuration and program files exactly as before. Production directory names,
service units and persistent identity/TLS state are not relocated by packaging.

## Offline evidence

The packaging tests build a deterministic wheel and exercise the real public
PEP517 build entry point. They install the wheel into an isolated virtual
environment, run from an unrelated directory with network calls blocked,
initialize and edit a separate site, generate/validate, reject an incompatible
requirement and confirm installed program hashes remain unchanged.

Two private Git fixture revisions select distinct application releases. The
test deploys into a synthetic local state directory, rolls back to the older
helper bytes and verifies a tampered old release is rejected before writes.
It starts no native runtime or service and sends no device command. Real
installation and commissioning remain separate owner-controlled checks.
