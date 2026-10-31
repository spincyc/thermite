"""ASGI entrypoint; service composition installs a finite runtime adapter."""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "oh_identity.settings")
application = get_asgi_application()
