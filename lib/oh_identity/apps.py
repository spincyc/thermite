import os
import threading

from django.apps import AppConfig
from django.db.backends.signals import connection_created
from django.db.models.signals import post_delete, post_save

_wal_lock = threading.Lock()
_wal_initialized: set[tuple[str, int | None]] = set()


def _configure_sqlite(sender, connection, **kwargs):
    """Apply per-connection SQLite pragmas and initialize WAL mode once per DB file."""
    if connection.vendor != "sqlite":
        return
    name = str(connection.settings_dict.get("NAME") or "")
    with connection.cursor() as cursor:
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        if name and name != ":memory:":
            try:
                inode: int | None = os.stat(name).st_ino
            except OSError:
                inode = None
            key = (name, inode)
            with _wal_lock:
                if key not in _wal_initialized:
                    cursor.execute("PRAGMA journal_mode=WAL")
                    if inode is None:
                        try:
                            inode = os.stat(name).st_ino
                        except OSError:
                            inode = None
                    _wal_initialized.add((name, inode))


def _on_user_save(sender, update_fields=None, **kwargs):
    if update_fields is not None and set(update_fields) <= {"last_login"}:
        return
    from .auth import bump_auth_revocation
    bump_auth_revocation()


def _on_token_save(sender, created=False, **kwargs):
    if created:
        return
    from .auth import bump_auth_revocation
    bump_auth_revocation()


def _on_auth_revocation(sender, **kwargs):
    from .auth import bump_auth_revocation
    bump_auth_revocation()


class IdentityConfig(AppConfig):
    name = "oh_identity"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        from django.contrib.sessions.models import Session
        from .models import PersonalToken, SecurityState, User

        connection_created.connect(_configure_sqlite, dispatch_uid="oh_identity.configure_sqlite")
        post_save.connect(_on_user_save, sender=User, dispatch_uid="oh_identity.revocation.user.save")
        post_delete.connect(_on_auth_revocation, sender=User, dispatch_uid="oh_identity.revocation.user.delete")
        post_save.connect(_on_token_save, sender=PersonalToken, dispatch_uid="oh_identity.revocation.token.save")
        post_delete.connect(_on_auth_revocation, sender=PersonalToken, dispatch_uid="oh_identity.revocation.token.delete")
        post_save.connect(_on_auth_revocation, sender=SecurityState, dispatch_uid="oh_identity.revocation.security.save")
        post_delete.connect(_on_auth_revocation, sender=SecurityState, dispatch_uid="oh_identity.revocation.security.delete")
        post_delete.connect(_on_auth_revocation, sender=Session, dispatch_uid="oh_identity.revocation.session.delete")
