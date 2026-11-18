# Thermite application

This repository is the canonical application. Change shared runtime code,
clients, installer, integrations, tests and documentation here. Private site
repositories own instance configuration, native version pins and an exact
application release requirement; generated runtime files are local derived
output, not an application source tree.

Package only the reviewed files in `.thermite-export.json`. Add new application
files and test fixtures to that manifest. Packaging preserves their exact bytes;
never copy private inventory, credentials or live state into this repository.
Use fictional fixtures and documentation address ranges for portable tests.

Run `make check` on the exact commit before publishing. It runs the tests in
one worker process per CPU; `make check JOBS=1` runs them serially, exactly as
`python -m unittest discover -s tests -t . -q`. Identity tests also run
with `PYTHONPATH=lib DJANGO_SETTINGS_MODULE=oh_identity.test_settings` and a
private scratch `THERMITE_IDENTITY_STATE`, using `python -m django test oh_identity
--noinput`. Pinned native-runtime tests require the documented local dependency
paths. Report any skipped checks.

After a change under `web/remote`, also run `make check-browser`. It opens
every page of the example house in the system's Chromium at 390x844 and at
1440x900, behind the gateway's Content-Security-Policy header and with the
sample backend, and fails on a policy violation, an error, a page that scrolls
sideways, a control under 44 pixels on the phone, or anything painted under
the bar (`tests/browser_smoke.py`). It installs nothing, takes about twenty
seconds and is not part of `make check`; without Chromium, or with Node.js
older than 22, it skips and says why. The nightly Boot workflow runs it.
