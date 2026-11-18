from django.db import migrations, models
from django.utils import timezone


def settle(apps, schema_editor):
    """What the authentication switch decided becomes each setting's own
    value, so that an upgrade changes one thing only: no Guest sees the
    cameras until an Admin says so (guest_cameras starts off everywhere).

    While the switch was on, nothing else depended on it: the stored settings
    and locks stand. While it was off, every client was a Guest who saw free
    text and whom no lock stopped, whatever was stored. So free text stays
    shown, and each stored schedule is kept but disabled and each manual hold
    released, under a new revision as every change to a policy has: a lock
    that did not apply before the upgrade does not apply after it.
    """
    alias = schema_editor.connection.alias
    state = apps.get_model("oh_identity", "SecurityState").objects.using(alias).filter(pk=1).first()
    if state is None or state.enabled:
        return
    if not state.guest_text:
        state.guest_text = True
        state.save(update_fields=["guest_text"])
    now = timezone.now()
    for row in apps.get_model("oh_identity", "RoomPolicy").objects.using(alias).all():
        config = row.config
        if not isinstance(config, dict) or config.get("enabled") is False and config.get("manual_lock") is None:
            continue  # Already unable to lock its room (oh_room_policy.active).
        row.revision += 1
        row.config = {**{key: value for key, value in config.items() if key != "manual_lock"},
                      "enabled": False, "revision": row.revision}
        row.updated_at = now
        row.save(update_fields=["revision", "config", "updated_at"])
    apps.get_model("oh_identity", "ReconcileState").objects.using(alias).all().delete()


def unsettle(apps, schema_editor):
    """Backwards, the switch is on wherever an account exists, as 0002 seeded
    it. An install goes back another way: by restoring the state its upgrade
    set aside."""
    alias = schema_editor.connection.alias
    on = apps.get_model("oh_identity", "User").objects.using(alias).exists()
    apps.get_model("oh_identity", "SecurityState").objects.using(alias).update(enabled=on)


class Migration(migrations.Migration):
    """Access without a mode (oh_identity.security)."""

    dependencies = [("oh_identity", "0004_security_guest_text")]
    operations = [
        migrations.AddField(model_name="securitystate", name="guest_cameras", field=models.BooleanField(default=False)),
        migrations.RunPython(settle, unsettle),
        migrations.RemoveField(model_name="securitystate", name="enabled"),
        migrations.RemoveField(model_name="securitystate", name="revision"),
    ]
