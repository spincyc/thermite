"""Real Django test DB in the caller's private scratch state directory."""

from .settings import *  # noqa: F403

ALLOWED_HOSTS = ["testserver", "localhost"]
DATABASES["default"]["TEST"] = {"NAME": STATE / "test-identity.sqlite3"}  # noqa: F405
