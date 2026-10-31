"""Persistent identity and policy state; no device credential is a model field."""

import uuid

from django.contrib.auth.models import AbstractUser
from django.db import models

CATALOG = ("site.read", "room.control", "settings.manage", "users.manage", "roles.manage", "pairing.manage")
RESERVED = {"Admin": CATALOG, "User": ("site.read", "room.control"), "Viewer": ("site.read",)}


class Role(models.Model):
    name = models.CharField(max_length=64, unique=True)
    permissions = models.JSONField(default=list, blank=True)
    reserved = models.BooleanField(default=False)


class User(AbstractUser):
    role = models.ForeignKey(Role, null=True, on_delete=models.PROTECT)
    auth_revision = models.PositiveIntegerField(default=1)
    # None means all configured rooms; [] explicitly grants no room controls.
    rooms = models.JSONField(default=None, null=True, blank=True)


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
    at = models.DateTimeField(auto_now_add=True)
    actor_id = models.PositiveBigIntegerField(null=True)
    action = models.CharField(max_length=40)
    target = models.CharField(max_length=100, blank=True)
    outcome = models.CharField(max_length=32)


class LoginThrottle(models.Model):
    key = models.CharField(max_length=64, unique=True)
    attempts = models.PositiveIntegerField(default=0)
    until = models.DateTimeField()
