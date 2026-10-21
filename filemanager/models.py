import posixpath
import uuid

from django.conf import settings
from django.contrib.auth.models import User
from django.core.files.storage import storages
from django.db import models
from django.db.models import Q
from django.db.models.functions import Lower
from django_cleanup import cleanup


class FolderPermission(models.Model):
    PERMISSION_CHOICES = [
        ('read', 'Read Only'),
        ('write', 'Read and Write'),
        ('admin', 'Full Access'),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE)
    folder_path = models.CharField(max_length=1000)
    permission = models.CharField(max_length=10, choices=PERMISSION_CHOICES)

    class Meta:
        unique_together = ['user', 'folder_path']


class UserProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    password_changed = models.BooleanField(default=False)
    vessel_name = models.CharField(max_length=100, blank=True)
    # Entra ID object ID, set on the first sign-in with Microsoft
    microsoft_oid = models.CharField(
        'Microsoft account ID',
        max_length=36,
        unique=True,
        null=True,
        blank=True,
        help_text=(
            'Set on the first sign-in with Microsoft. Clear it to let a '
            'different Microsoft account link to this user.'
        ),
    )

    def __str__(self):
        return f"{self.user.username} - {self.vessel_name}"


class FileActivity(models.Model):
    ACTIVITY_CHOICES = [
        ('upload', 'File Upload'),
        ('download', 'File Download'),
        ('delete', 'File Delete'),
        ('view', 'File View'),
        ('rename', 'File Rename'),
        ('sign', 'File Signed'),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE)
    filename = models.CharField(max_length=255)
    filepath = models.CharField(max_length=1000)
    activity_type = models.CharField(max_length=10, choices=ACTIVITY_CHOICES)
    timestamp = models.DateTimeField(auto_now_add=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    file_size = models.BigIntegerField(null=True, blank=True)

    def __str__(self):
        return f"{self.user.username} - {self.activity_type} - {self.filename}"


class UploadSession(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    session_id = models.CharField(max_length=100)
    folder_path = models.CharField(max_length=1000)
    total_files = models.IntegerField(default=0)
    completed_files = models.IntegerField(default=0)
    total_size = models.BigIntegerField(default=0)
    uploaded_size = models.BigIntegerField(default=0)
    status = models.CharField(max_length=20, default='uploading')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


def vessel_storage():
    return storages['vessel']


def file_upload_to(instance, filename):
    # Stored under a random name; the real name lives in File.name. Uploads go
    # in 'objects', apart from the files imported where they already were, and
    # the first two characters pick a subfolder so no folder gets too big. The
    # extension is kept so the file still opens straight from the shared folder.
    name = uuid.uuid4().hex
    extension = posixpath.splitext(filename)[1].lower()
    return f'objects/{name[:2]}/{name}{extension}'


class Folder(models.Model):
    # Only the root folder has no parent
    parent = models.ForeignKey(
        'self', on_delete=models.PROTECT, null=True, blank=True, related_name='children'
    )
    name = models.CharField(max_length=255)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='+',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    deleted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='+',
    )
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            # Case-insensitive, like Windows, where the files end up
            models.UniqueConstraint(
                'parent',
                Lower('name'),
                condition=Q(deleted_at__isnull=True),
                name='unique_active_folder_name',
            ),
        ]

    def __str__(self):
        return self.name


# Imported files are the originals in vessel_files, so deleting a row must
# never delete the file on disk
@cleanup.ignore
class File(models.Model):
    folder = models.ForeignKey(Folder, on_delete=models.PROTECT, related_name='files')
    name = models.CharField(max_length=255)
    # Imported files keep their original path, which can be long
    file = models.FileField(
        upload_to=file_upload_to, storage=vessel_storage, max_length=1000
    )
    size = models.PositiveBigIntegerField()
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='+',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    deleted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='+',
    )
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            # Case-insensitive, like Windows, where the files end up
            models.UniqueConstraint(
                'folder',
                Lower('name'),
                condition=Q(deleted_at__isnull=True),
                name='unique_active_file_name',
            ),
        ]

    def __str__(self):
        return self.name

    @property
    def extension(self):
        """Lowercased, with the dot, e.g. '.pdf'; '' if there is none."""
        return posixpath.splitext(self.name)[1].lower()


class UserSignature(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='saved_signature',
    )
    image = models.ImageField(upload_to='signing/user_signatures/')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class SigningRequest(models.Model):
    class Status(models.TextChoices):
        DRAFT = 'draft', 'Draft'
        IN_PROGRESS = 'in_progress', 'In progress'
        COMPLETED = 'completed', 'Completed'

    class PlacementMode(models.TextChoices):
        MANUAL = 'manual', 'Manual'
        AUTO = 'auto', 'Auto'

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name='signing_requests',
    )

    placement_mode = models.CharField(
        max_length=10,
        choices=PlacementMode,
        default=PlacementMode.MANUAL,
    )
    status = models.CharField(max_length=20, choices=Status, default=Status.DRAFT)

    # One request per file; it follows the file when the file is renamed
    file = models.OneToOneField(
        File, on_delete=models.PROTECT, related_name='signing_request'
    )
    original_file = models.FileField(upload_to='signing/original/', max_length=255)
    original_sha = models.CharField(max_length=64)
    # Updated after every signature: the PDF with all signatures so far, the
    # final one once completed. signed_sha is only set for that final version.
    signed_file = models.FileField(
        upload_to='signing/signed/', max_length=255, blank=True
    )
    signed_sha = models.CharField(max_length=64, blank=True)

    notify_users = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        blank=True,
        related_name='signing_notifications',
    )

    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        # Who may start a signing request; superusers always may
        permissions = [('request_signatures', 'Can request signatures')]

    def __str__(self):
        return self.filename

    @property
    def filename(self):
        return self.file.name


class Signer(models.Model):
    class Status(models.TextChoices):
        WAITING = 'waiting', 'Waiting'
        PENDING = 'pending', 'Pending'
        SIGNED = 'signed', 'Signed'

    signing_request = models.ForeignKey(
        SigningRequest,
        on_delete=models.CASCADE,
        related_name='signers',
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name='signer_entries',
    )
    signing_order = models.PositiveSmallIntegerField()
    status = models.CharField(max_length=10, choices=Status, default=Status.WAITING)
    signature_image = models.ImageField(upload_to='signing/signatures/', blank=True)
    signed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['signing_order']
        constraints = [
            models.UniqueConstraint(
                fields=['signing_request', 'signing_order'],
                name='unique_signer_order',
            ),
            models.UniqueConstraint(
                fields=['signing_request', 'user'],
                name='unique_signer_user',
            ),
        ]

    def __str__(self):
        return f"{self.signing_order}. {self.user}"


class SigningField(models.Model):
    class FieldType(models.TextChoices):
        SIGNATURE = 'signature', 'Signature'
        DATE = 'date', 'Date'

    signer = models.ForeignKey(Signer, on_delete=models.CASCADE, related_name='fields')
    field_type = models.CharField(max_length=10, choices=FieldType)
    page = models.PositiveIntegerField()

    # Position and size as a 0-1 ratio of the page width/height
    x = models.FloatField()
    y = models.FloatField()
    width = models.FloatField()
    height = models.FloatField()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['signer', 'field_type'],
                name='unique_signer_field_type',
            ),
        ]


class SigningActivity(models.Model):
    class Action(models.TextChoices):
        CREATED = 'created', 'Created'
        SENT = 'sent', 'Sent'
        SIGNED = 'signed', 'Signed'
        COMPLETED = 'completed', 'Completed'

    signing_request = models.ForeignKey(
        SigningRequest,
        on_delete=models.CASCADE,
        related_name='activities',
    )
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    action = models.CharField(max_length=20, choices=Action)

    # e.g. COMPLETED: {'written_back': False, 'reason': 'source_modified'}
    detail = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']
