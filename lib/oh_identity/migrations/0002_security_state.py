from django.db import migrations, models


def seed_security(apps, schema_editor):
    accounts = apps.get_model("oh_identity", "User")
    security = apps.get_model("oh_identity", "SecurityState")
    alias = schema_editor.connection.alias
    security.objects.using(alias).create(id=1, enabled=accounts.objects.using(alias).exists())


class Migration(migrations.Migration):
    dependencies = [("oh_identity", "0001_initial")]
    operations = [
        migrations.CreateModel(
            name="SecurityState",
            fields=[
                ("id", models.PositiveSmallIntegerField(default=1, editable=False, primary_key=True, serialize=False)),
                ("enabled", models.BooleanField(default=False)),
                ("revision", models.PositiveIntegerField(default=1)),
                ("setup_digest", models.CharField(blank=True, max_length=64)),
                ("setup_expires_at", models.DateTimeField(blank=True, null=True)),
            ],
            options={"constraints": [models.CheckConstraint(condition=models.Q(id=1), name="security_singleton")]},
        ),
        migrations.RunPython(seed_security, migrations.RunPython.noop),
    ]
