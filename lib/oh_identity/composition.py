"""Trusted service/CLI hooks. No public bootstrap or runtime reconfiguration API."""

import os
from pathlib import Path


def setup(state_directory, allowed_hosts=None):
    """Call before importing app models, once per process/deployment."""
    directory = str(Path(state_directory).expanduser().absolute())
    previous = os.environ.get("THERMITE_IDENTITY_STATE")
    if previous and previous != directory:
        raise ValueError("A process cannot serve multiple identity databases")
    os.environ["THERMITE_IDENTITY_STATE"] = directory
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "oh_identity.settings")
    if allowed_hosts:
        os.environ["THERMITE_ALLOWED_HOSTS"] = ",".join(allowed_hosts)
    import django
    django.setup()


def initialize(state_directory, username, password, allowed_hosts=None):
    """Owner's CLI supplies a hidden terminal password, never argv/env/chat."""
    setup(state_directory, allowed_hosts)
    from django.core.management import call_command
    from .services import bootstrap
    call_command("migrate", interactive=False, verbosity=0)
    return bootstrap(username, password)
