# Application releases and private sites

Install the application separately from your private site's configuration and
state. Each site pins an exact application release. These development builds
are currently `0.1.0.dev0`; complete source checkouts are also supported.
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
native runtime downloads and service dependencies have separate setup steps.
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
commands that create configuration or state require one. Complete source
checkouts retain their original behavior.

The application supplies integrations, templates, web assets, terminal source,
themes and dependency locks. The private site owns its `instance/` data
(inventory, host settings, custom themes and IR profiles), native runtime pins
in `versions.json`, and its application pin in `thermite-site.json`. Commit
these inputs in private Git. The generated `conf/`, `metrics/` and `deploy/`
trees are ignored local build output, alongside `var/` and `build/`.
Application JavaScript changes therefore require no generated-code commit in
the private repository.

New sites use manifest schema 2. Deployment exports the selected Git revision's
inputs and renders them with that revision's exact verified application from
`var/application-releases/`. It uses a temporary private directory and an
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

Keep that environment active for the site upgrade below. Use the same procedure with a
new environment after an Arch system update changes Python's minor version.
Retain the previous application and private-site backups until the update
has been verified.

### Update the private site

With the new application environment active:

```sh
thermite init --upgrade /path/to/private-site
thermite check --config /path/to/private-site
```

This selects schema 2 and updates the application requirement and ignore rules
while preserving inventory, runtime pins, credentials and state. It removes
recognized generated launchers and an unchanged generated Makefile. Customized
launchers are refused for review; custom Makefiles are preserved. If the site previously
tracked generated output, remove that tracking before committing the new site
revision:

```sh
git rm -r --cached --ignore-unmatch conf metrics deploy
```

This leaves the local files in place. Generated files are rebuilt from the
committed inputs when required. Review the normal runtime upgrade/deploy plan
after committing the inputs and application pin.

### Preserve rollback releases

Verified application releases are cached under
`var/application-releases/`. Retain that cache and the original release
artifacts for rollback. Deployment resolves inputs and the application
requirement from the selected private Git revision, renders its configuration
and units with that cached release, and copies the finite helper/exporter
program files from the same release. Rollback uses that older release. A
missing or modified artifact fails before deployment writes; deployment never
downloads a substitute automatically. A previous-deployment boot waiver must
match the actual derived configuration digest as well as its Git revision.

When an application update changes shared script libraries, use
`thermite install --restart-runtime` after committing the new site pin. The
normal installer preserves state and performs one controlled runtime restart
after deployment, followed by readiness checks, under the same run approval.

### Legacy sites

Historical schema-1 site revisions retain their committed generated trees and
exact cached helper application. Historical complete-checkout revisions retain
their committed source and generated configuration. Their rollback format is
not silently reinterpreted as schema 2.

Existing complete Git checkouts continue deploying their own committed program
and configuration until explicitly reduced to a config-only site. Init still
refuses to overwrite a nonempty legacy checkout; conversion requires reviewed
private Git changes after the application has been installed separately.
Preserve the site's existing directory and ignored `var/`, and retain host
state paths, ports, service identity and separately staged account/TLS state.
Packaging does not relocate production data or service units. Program
integrity assumes a trusted Python interpreter and installed
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
