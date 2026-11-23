from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("filemanager", "0008_fileactivity_move"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="signingfield",
            name="unique_signer_field_type",
        ),
        migrations.AddConstraint(
            model_name="signingfield",
            constraint=models.UniqueConstraint(
                condition=models.Q(("field_type", "signature"), _negated=True),
                fields=("signer", "field_type"),
                name="unique_signer_field_type_except_signature",
            ),
        ),
    ]
