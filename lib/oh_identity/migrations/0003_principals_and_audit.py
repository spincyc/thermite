from django.db import migrations, models


def admins(apps, schema_editor):
    # Before roles are dropped: exactly the reserved Admin role becomes admin.
    # Every other account (User, Viewer, custom roles) becomes a Member.
    users = apps.get_model("oh_identity", "User")
    users.objects.using(schema_editor.connection.alias).filter(role__name="Admin", role__reserved=True).update(admin=True)


class Migration(migrations.Migration):
    """Roles and room grants are replaced by one admin flag; audit records
    gain the principal kind and a keyed peer label. Rolling back across this
    migration means rolling back the upgrade's install transaction, which
    puts the identity state from before it back."""
    dependencies = [("oh_identity", "0002_security_state")]
    operations = [
        migrations.AddField(model_name="user", name="admin", field=models.BooleanField(default=False)),
        migrations.RunPython(admins, migrations.RunPython.noop),
        migrations.RemoveField(model_name="user", name="role"),
        migrations.RemoveField(model_name="user", name="rooms"),
        migrations.DeleteModel(name="Role"),
        migrations.AddField(model_name="audit", name="principal", field=models.CharField(blank=True, default="", max_length=16)),
        migrations.AddField(model_name="audit", name="peer", field=models.CharField(blank=True, default="", max_length=32)),
    ]
