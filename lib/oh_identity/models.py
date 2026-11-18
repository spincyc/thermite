"""Persistent identity and policy state; no device credential is a model field."""

import uuid

from django.contrib.auth.models import AbstractUser
from django.db import models

class SecurityState(models.Model):
    """The site's one row of settings (oh_identity.security states the access
    model) and the owner's setup capability, never stored in plaintext."""
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    setup_digest = models.CharField(max_length=64, blank=True)
    setup_expires_at = models.DateTimeField(null=True, blank=True)
    # Whether a Guest sees free-text Items (titles, artists, apps).
    guest_text = models.BooleanField(default=True)
    # Whether a Guest sees the cameras, as an account does.
    guest_cameras = models.BooleanField(default=False)

    class Meta:
        constraints = [models.CheckConstraint(condition=models.Q(id=1), name="security_singleton")]


class User(AbstractUser):
    admin = models.BooleanField(default=False)
    auth_revision = models.PositiveIntegerField(default=1)


class PersonalToken(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    name = models.CharField(max_length=80)
    digest = models.CharField(max_length=64, unique=True)
    auth_revision = models.PositiveIntegerField()
    expires_at = models.DateTimeField()
    revoked = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)


class RoomPolicy(models.Model):
    room_id = models.CharField(max_length=100, unique=True)
    config = models.JSONField(default=dict)
    revision = models.PositiveIntegerField(default=1)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(User, null=True, on_delete=models.SET_NULL)


class BoundaryReceipt(models.Model):
    key = models.CharField(max_length=240, unique=True)
    room_id = models.CharField(max_length=100)
    revision = models.PositiveIntegerField()
    status = models.CharField(max_length=24, default="claimed")
    reason = models.CharField(max_length=40, blank=True)
    updated_at = models.DateTimeField(auto_now=True)


class ReconcileState(models.Model):
    room_id = models.CharField(max_length=100, unique=True)
    revision = models.PositiveIntegerField()
    state = models.JSONField(default=dict)
    updated_at = models.DateTimeField(auto_now=True)


class Audit(models.Model):
    """Security events. principal is guest/session/token/password/setup/
    local/anonymous; peer is a keyed digest of the peer's address bucket,
    never the address itself."""
    at = models.DateTimeField(auto_now_add=True)
    actor_id = models.PositiveBigIntegerField(null=True)
    action = models.CharField(max_length=40)
    target = models.CharField(max_length=100, blank=True)
    outcome = models.CharField(max_length=32)
    principal = models.CharField(max_length=16, blank=True, default="")
    peer = models.CharField(max_length=32, blank=True, default="")


class LoginThrottle(models.Model):
    key = models.CharField(max_length=64, unique=True)
    attempts = models.PositiveIntegerField(default=0)
    until = models.DateTimeField()
