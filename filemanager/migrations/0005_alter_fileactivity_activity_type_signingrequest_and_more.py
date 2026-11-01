import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("filemanager", "0004_create_root_folder"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AlterField(
            model_name="fileactivity",
            name="activity_type",
            field=models.CharField(
                choices=[
                    ("upload", "File Upload"),
                    ("download", "File Download"),
                    ("delete", "File Delete"),
                    ("view", "File View"),
                    ("rename", "File Rename"),
                    ("sign", "File Signed"),
                ],
                max_length=10,
            ),
        ),
        migrations.CreateModel(
            name="SigningRequest",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "placement_mode",
                    models.CharField(
                        choices=[("manual", "Manual"), ("auto", "Auto")],
                        default="manual",
                        max_length=10,
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("draft", "Draft"),
                            ("in_progress", "In progress"),
                            ("completed", "Completed"),
                        ],
                        default="draft",
                        max_length=20,
                    ),
                ),
                (
                    "original_file",
                    models.FileField(max_length=255, upload_to="signing/original/"),
                ),
                ("original_sha", models.CharField(max_length=64)),
                (
                    "signed_file",
                    models.FileField(
                        blank=True, max_length=255, upload_to="signing/signed/"
                    ),
                ),
                ("signed_sha", models.CharField(blank=True, max_length=64)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("sent_at", models.DateTimeField(blank=True, null=True)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                (
                    "created_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="signing_requests",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "file",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="signing_request",
                        to="filemanager.file",
                    ),
                ),
                (
                    "notify_users",
                    models.ManyToManyField(
                        blank=True,
                        related_name="signing_notifications",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "permissions": [("request_signatures", "Can request signatures")],
            },
        ),
        migrations.CreateModel(
            name="SigningActivity",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "action",
                    models.CharField(
                        choices=[
                            ("created", "Created"),
                            ("sent", "Sent"),
                            ("signed", "Signed"),
                            ("completed", "Completed"),
                        ],
                        max_length=20,
                    ),
                ),
                ("detail", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "actor",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "signing_request",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="activities",
                        to="filemanager.signingrequest",
                    ),
                ),
            ],
            options={
                "ordering": ["created_at"],
            },
        ),
        migrations.CreateModel(
            name="Signer",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("signing_order", models.PositiveSmallIntegerField()),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("waiting", "Waiting"),
                            ("pending", "Pending"),
                            ("signed", "Signed"),
                        ],
                        default="waiting",
                        max_length=10,
                    ),
                ),
                (
                    "signature_image",
                    models.ImageField(blank=True, upload_to="signing/signatures/"),
                ),
                ("signed_at", models.DateTimeField(blank=True, null=True)),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="signer_entries",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "signing_request",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="signers",
                        to="filemanager.signingrequest",
                    ),
                ),
            ],
            options={
                "ordering": ["signing_order"],
            },
        ),
        migrations.CreateModel(
            name="UserSignature",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("image", models.ImageField(upload_to="signing/user_signatures/")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "user",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="saved_signature",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="SigningField",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "field_type",
                    models.CharField(
                        choices=[("signature", "Signature"), ("date", "Date")],
                        max_length=10,
                    ),
                ),
                ("page", models.PositiveIntegerField()),
                ("x", models.FloatField()),
                ("y", models.FloatField()),
                ("width", models.FloatField()),
                ("height", models.FloatField()),
                (
                    "signer",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="fields",
                        to="filemanager.signer",
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(
                        fields=("signer", "field_type"), name="unique_signer_field_type"
                    )
                ],
            },
        ),
        migrations.AddConstraint(
            model_name="signer",
            constraint=models.UniqueConstraint(
                fields=("signing_request", "signing_order"), name="unique_signer_order"
            ),
        ),
        migrations.AddConstraint(
            model_name="signer",
            constraint=models.UniqueConstraint(
                fields=("signing_request", "user"), name="unique_signer_user"
            ),
        ),
    ]
