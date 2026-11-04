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

Run `make check` on the exact commit before publishing. Identity tests also run
with `PYTHONPATH=lib DJANGO_SETTINGS_MODULE=oh_identity.test_settings` and a
private scratch `THERMITE_IDENTITY_STATE`, using `python -m django test oh_identity
--noinput`. Pinned native-runtime tests require the documented local dependency
paths. Report any skipped checks.
