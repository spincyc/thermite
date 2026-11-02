# Application releases and private sites

Thermite supports a complete source checkout and an installed application
with a separate private site. These are development builds, currently
`0.1.0.dev0`. Follow [installation](installation.md) for a Linux server or
[ISO installation](iso.md) for a bare machine.

## Package the application

From source, run `python3 bin/thermite package --out .scratch/releases`.
The builder creates a wheel with ordinary Python metadata and the
`thermite` console entry point. It includes the public program, documentation
and fictional example; it excludes private inventory, credentials and runtime
state. The public source also supports `python -m pip install .`.

Use a dedicated virtual environment outside the private site. PyYAML is the
application's configuration dependency. Node.js, the locked terminal package,
native runtime downloads and service dependencies have separate setup steps.
For an offline wheel installation, supply PyYAML first, then run
`python -m pip install --no-index --no-deps /path/to/thermite_app.whl`.

## Initialize a site

```sh
thermite init /path/to/private-site
thermite --site /path/to/private-site generate
thermite --site /path/to/private-site validate
```

Init requires a new or empty directory. It writes a fictional inventory,
sample host settings, runtime pins, a private launcher, a small Makefile and
`thermite-site.json`. Configuration files are mode 0600. It rejects symlinks,
application overlap and existing user files. Replace the example before
commissioning hardware. Init creates no account or service and does not
initialize or publish Git.

Select a site with `--site DIR`, `THERMITE_SITE`, or by working inside its
directory. The private launcher also selects its site and Python environment.
Help, themes, documentation, packaging and ISO builds work without a site;
commands that create configuration or state require one. Complete source
checkouts retain their original behavior.

The application supplies integrations, templates, web assets, terminal source,
themes and dependency locks. The private site owns inventory, custom themes,
IR profiles, generated `conf/`, `metrics/`, `deploy/`, caches and runtime state.
Keep its configuration, manifest and generated outputs in private Git;
`var/` and `build/` stay ignored. Production installation still requires a
clean committed site and the native boot test.

## Update and roll back

A site's manifest pins an application version and the exact release digest.
The release manifest lists program files and SHA256 values. Changed, missing,
extra or symlinked program files fail validation. The installed application
is not a writable configuration directory.

Install a new application artifact in its environment, then explicitly run:

```sh
thermite init --upgrade /path/to/private-site
thermite --site /path/to/private-site generate
thermite --site /path/to/private-site validate
```

This updates the application requirement and launcher while preserving
inventory, runtime pins, generated outputs, credentials and state. A
customized launcher is refused for manual review. Commit the resulting site
revision, then review the normal runtime upgrade/deploy plan.

Verified application releases are cached under
`var/application-releases/`. Retain that cache and the original release
artifacts for rollback. Deployment resolves configuration and the application
requirement from the selected private Git revision, then copies the finite
helper/exporter program files from that revision's cached release. Rollback
uses that older release. A missing or modified artifact fails before deployment
writes; deployment never downloads a substitute automatically.

Existing complete Git checkouts continue deploying their own committed program
and configuration. Packaging does not relocate production data or service
units. Program integrity assumes a trusted Python interpreter and installed
bootstrap; it does not protect against an administrator changing them.

## Offline verification

The packaging tests build deterministic wheels through both the package API
and public PEP517 entry point. They install outside the checkout with network
calls blocked, initialize a separate site, generate and validate, and confirm
that application resources remain unchanged. Two private fixture revisions
exercise exact helper rollback and reject a tampered old release before writes.
ISO tests additionally check installer confirmation settings and an offline
bootstrap into a synthetic filesystem. These tests start no runtime or device
operation; actual boot and commissioning have separate acceptance checks.
