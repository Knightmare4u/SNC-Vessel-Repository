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

    def __str__(self):
        return f"{self.user.username} - {self.vessel_name}"


class FileActivity(models.Model):
    ACTIVITY_CHOICES = [
        ('upload', 'File Upload'),
        ('download', 'File Download'),
        ('delete', 'File Delete'),
        ('view', 'File View'),
        ('rename', 'File Rename'),
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
    # the first two characters pick a subfolder so no folder gets too big.
    name = uuid.uuid4().hex
    return f'objects/{name[:2]}/{name}'


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
