"""Production defaults. The owner supplies state, origin and runtime contract."""

import os
import hashlib

from .state import private_state

STATE = private_state(os.environ.get("THERMITE_IDENTITY_STATE"))
SECRET_KEY = (STATE / "secret-key").read_text().strip()
SITE_ID = (STATE / "site-id").read_text().strip()
COOKIE_PREFIX = "thermite_" + hashlib.sha256(SITE_ID.encode()).hexdigest()[:16]
DEBUG = False
ALLOWED_HOSTS = [x.strip() for x in os.environ.get("THERMITE_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if x.strip()]
ROOT_URLCONF = "oh_identity.urls"
ASGI_APPLICATION = "oh_identity.asgi.application"
INSTALLED_APPS = ["django.contrib.auth", "django.contrib.contenttypes", "django.contrib.sessions", "oh_identity"]
# GuestListener admits plain-HTTP Guest listener requests (oh_identity.guest):
# inside the header middleware, so its refusals carry them, and outside the
# session and CSRF middleware, whose cookies it withholds.
MIDDLEWARE = ["oh_identity.hardening.SecurityHeaders", "oh_identity.guest.TransportSecurity", "django.middleware.clickjacking.XFrameOptionsMiddleware",
              "oh_identity.guest.GuestListener", "django.contrib.sessions.middleware.SessionMiddleware",
              "django.contrib.auth.middleware.AuthenticationMiddleware", "oh_identity.auth.IdentityCSRF"]
AUTH_USER_MODEL = "oh_identity.User"
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": STATE / "identity.sqlite3",
                         # WAL lets status reads proceed beside the worker's writes.
                         "OPTIONS": {"timeout": 5, "transaction_mode": "IMMEDIATE",
                                     "init_command": "PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL"}}}
CONN_MAX_AGE = 0
USE_TZ = True
TIME_ZONE = os.environ.get("THERMITE_TIME_ZONE", "UTC")
PASSWORD_HASHERS = ["django.contrib.auth.hashers.Argon2PasswordHasher"]
AUTH_PASSWORD_VALIDATORS = [{"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 12}},
                            {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
                            {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"}]
SESSION_ENGINE = "django.contrib.sessions.backends.db"
SESSION_COOKIE_NAME = COOKIE_PREFIX + "_session"
# Secure cookies never travel over the plain-HTTP Guest listener, which
# neither reads nor sets any cookie.
SESSION_COOKIE_SECURE = True
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_AGE = 12 * 60 * 60
CSRF_COOKIE_SECURE = True
CSRF_COOKIE_NAME = COOKIE_PREFIX + "_csrf"
CSRF_COOKIE_SAMESITE = "Lax"
CSRF_FAILURE_VIEW = "oh_identity.auth.csrf_failure"
# Except on the Guest listener (oh_identity.guest.TransportSecurity).
SECURE_SSL_REDIRECT = True
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
DATA_UPLOAD_MAX_MEMORY_SIZE = 65536
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
# Server errors and package warnings reach stderr (the journal) with paths,
# frames and exception classes only: no request objects, headers, bodies,
# query strings, tokens, upstream bodies or exception messages. Django's
# own client 4xx and CSRF refusals are not logged; the trail's request line
# (oh_identity.trail.RequestLog) names a refused request's method, path,
# status and keyed peer digest only.
_JOURNAL = {"handlers": ["journal"], "level": "WARNING", "propagate": False}
LOGGING = {"version": 1, "disable_existing_loggers": False,
           "filters": {"private": {"()": "oh_identity.hardening.PrivateRecord"}},
           "formatters": {"private": {"()": "oh_identity.hardening.PrivateFormatter",
                                      "format": "%(levelname)s %(name)s: %(message)s"}},
           "handlers": {"null": {"class": "logging.NullHandler"},
                        "journal": {"class": "logging.StreamHandler", "stream": "ext://sys.stderr",
                                    "filters": ["private"], "formatter": "private"}},
           "loggers": {"django.request": {**_JOURNAL, "level": "ERROR"},
                       "django.security.csrf": {"handlers": ["null"], "propagate": False},
                       "oh_identity": _JOURNAL, "oh_access": _JOURNAL, "oh_room_policy": _JOURNAL,
                       # The command trail (oh_identity.trail) and the policy worker's
                       # boundary, repair and phase lines: key=value at INFO, with
                       # no credential, pairing code, body or address.
                       "oh_identity.trail": {**_JOURNAL, "level": "INFO"},
                       "oh_room_policy.worker": {**_JOURNAL, "level": "INFO"},
                       # Hypercorn configures no logging of its own (oh_access/service.py
                       # listener); its errors and unretrieved task exceptions use the
                       # same private handler.
                       "hypercorn.error": _JOURNAL, "asyncio": _JOURNAL}}
