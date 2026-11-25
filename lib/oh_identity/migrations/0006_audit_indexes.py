from django.db import migrations, models


class Migration(migrations.Migration):
    """Composite index on Audit(outcome, id) for cap trimming and index on Audit(at)."""

    dependencies = [("oh_identity", "0005_independent_settings")]
    operations = [
        migrations.AddIndex(
            model_name="audit",
            index=models.Index(fields=["outcome", "id"], name="oh_identity_audit_outcome_id"),
        ),
        migrations.AddIndex(
            model_name="audit",
            index=models.Index(fields=["at"], name="oh_identity_audit_at"),
        ),
    ]
