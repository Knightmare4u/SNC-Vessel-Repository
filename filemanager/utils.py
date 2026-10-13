import os
import posixpath
from pathlib import PurePosixPath

from django.conf import settings
from django.core.files.storage import FileSystemStorage

from .models import FileActivity, FolderPermission

PERMISSION_LEVELS = {
    'read': ('read', 'write', 'admin'),
    'write': ('write', 'admin'),
    'admin': ('admin',),
}
PERMISSION_RANK = {'read': 1, 'write': 2, 'admin': 3}


def normalize_path(path):
    """Return a canonical storage path: leading '/', no trailing '/', no '..'."""
    path = (path or '').replace('\\', '/').lstrip('/')
    return posixpath.normpath('/' + path)


def to_url_path(path):
    """Return the storage path without a leading '/' for use in URLs ('' = root)."""
    return normalize_path(path).lstrip('/')


def get_storage_path(rel_path, root=None):
    """Map a storage path to an absolute filesystem path inside root."""
    storage = FileSystemStorage(location=root or settings.FILE_STORAGE_ROOT)
    return storage.path(to_url_path(rel_path))


def get_user_permissions(user):
    if user.is_superuser:
        return [{'folder_path': '/', 'permission': 'admin'}]

    permissions = FolderPermission.objects.filter(user=user)
    return [
        {'folder_path': perm.folder_path, 'permission': perm.permission}
        for perm in permissions
    ]


def has_permission(user, folder_path, required_permission):
    if user.is_superuser:
        return True

    folder = PurePosixPath(normalize_path(folder_path))
    best = None

    # Permissions are additive: use the highest grant covering this folder
    for perm in get_user_permissions(user):
        if folder.is_relative_to(normalize_path(perm['folder_path'])):
            if (
                best is None
                or PERMISSION_RANK[perm['permission']] > PERMISSION_RANK[best]
            ):
                best = perm['permission']

    return best in PERMISSION_LEVELS[required_permission]


def log_activity(user, filename, filepath, activity_type, ip_address, file_size=None):
    FileActivity.objects.create(
        user=user,
        filename=filename,
        filepath=filepath,
        activity_type=activity_type,
        ip_address=ip_address,
        file_size=file_size,
    )


def format_file_size(size_bytes):
    if size_bytes == 0:
        return "0 B"

    size_names = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    while size_bytes >= 1024 and i < len(size_names) - 1:
        size_bytes /= 1024.0
        i += 1

    return f"{size_bytes:.2f} {size_names[i]}"


def get_folder_size(folder_path):
    total_size = 0
    for dirpath, dirnames, filenames in os.walk(folder_path):
        for filename in filenames:
            filepath = os.path.join(dirpath, filename)
            total_size += os.path.getsize(filepath)
    return total_size
