from django.db import migrations, models


class Migration(migrations.Migration):
    """Guests see free-text Items unless an Admin turns it off."""

    dependencies = [("oh_identity", "0003_principals_and_audit")]
    operations = [
        migrations.AddField(model_name="securitystate", name="guest_text", field=models.BooleanField(default=True)),
    ]
