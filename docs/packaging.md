# Application releases and private sites

Thermite supports a complete source checkout and an installed application
with a separate private site. These are development builds, currently
`0.1.0.dev0`. Check the [system requirements](requirements.md), then follow
[installation](installation.md) for stock Arch or Ubuntu Server, or
[ISO installation](iso.md) for a bare machine. The standalone ISO defaults
to Arch; an Ubuntu base is optional. All paths use the same application and
separate private site.

## Package the application

From source, run `python3 bin/thermite package --out .scratch/releases`.
The builder creates a wheel with ordinary Python metadata and the
`thermite` console entry point. It includes the public program, documentation
and fictional example; it excludes private inventory, credentials and runtime
state. The public source also supports `python -m pip install .`.

Use a dedicated virtual environment outside the private site. PyYAML is the
application's configuration dependency. Node.js, the locked terminal package,
native runtime downloads and service dependencies have separate setup steps.
The server guide provides the distribution-specific prerequisite commands;
do not install the application into the OS-managed Python environment.
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

The private site and launcher stay owned by the operator. Installed
application services use the non-login `thermite:thermite` account, UID/GID
9001, and their deployed configuration and state belong to that account.
The installer sets up trusted operator group access; see
[service ownership and migration](service-account.md).

## Update and roll back

A site's manifest pins an application version and the exact release digest.
The release manifest lists program files and SHA256 values. Changed, missing,
extra or symlinked program files fail validation. The installed application
is not a writable configuration directory.

Activate the application's Python environment and install the reviewed wheel.
Development builds may share a version number while their release digests
differ; force replacement so pip does not keep an older build of that version:

```sh
python -m pip install --force-reinstall /path/to/thermite_app-0.1.0.dev0-py3-none-any.whl
```

An ISO installation starts with an administrator-owned environment in
`/opt/thermite/venv` without pip. For later application updates, create an
owner-managed environment instead. On Arch, install `python-pip` first;
on Ubuntu, install `python3-venv`. Then run as your owner:

```sh
python3 -m venv "$HOME/.local/share/thermite/venv"
. "$HOME/.local/share/thermite/venv/bin/activate"
python -m pip install /path/to/thermite_app-0.1.0.dev0-py3-none-any.whl
```

Keep that environment active for the site upgrade below. It rewrites the
private launcher to use this interpreter. Use the same procedure with a
new environment after an Arch system update changes Python's minor version.
Retain the previous application and private-site backups until the update
has been verified.

Then explicitly update the private site:

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
ISO tests additionally check installer confirmation settings and the bundled
application bootstrap into a synthetic filesystem. The Arch installer uses
online signed OS packages; the Ubuntu variant also supports offline OS
installation. These tests start no runtime or device operation. Actual
machine installation and commissioning for each path have separate
acceptance checks; see the [review baseline](requirements.md#basis-and-review).
