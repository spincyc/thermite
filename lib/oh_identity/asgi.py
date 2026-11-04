"""ASGI entrypoint; service composition installs a finite runtime adapter.

RequestLog gives every response an X-Request-Id and journals each request
that changes something or is refused (oh_identity.trail)."""

import os

from django.core.asgi import get_asgi_application

from .trail import RequestLog

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "oh_identity.settings")
application = RequestLog(get_asgi_application())
