import os
import posixpath
from pathlib import Path, PurePosixPath

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.storage import FileSystemStorage

from .models import FileActivity, FolderPermission

PERMISSION_RANK = {'read': 1, 'write': 2, 'admin': 3}

# Same rules as OneDrive/Windows so names stay usable on Windows clients
INVALID_NAME_CHARS = '"*:<>?/\\|'
RESERVED_NAMES = {
    'CON',
    'PRN',
    'AUX',
    'NUL',
    *(f'COM{i}' for i in range(1, 10)),
    *(f'LPT{i}' for i in range(1, 10)),
}


def normalize_path(path):
    """Return a canonical storage path: leading '/', no trailing '/', no '..'."""
    path = (path or '').replace('\\', '/').lstrip('/')
    return posixpath.normpath('/' + path)


def validate_name(name):
    """Validate a user-supplied file or folder name; the name is kept as-is."""
    if not name or name in ('.', '..'):
        raise ValidationError('Name is required')
    if name != name.strip():
        raise ValidationError('Name cannot start or end with a space')
    if name.endswith('.'):
        raise ValidationError('Name cannot end with a period')
    if any(c in INVALID_NAME_CHARS or ord(c) < 32 for c in name):
        raise ValidationError(
            f'Name cannot contain any of these characters: {" ".join(INVALID_NAME_CHARS)}'
        )
    if name.split('.')[0].upper() in RESERVED_NAMES:
        raise ValidationError(f'"{name}" is a reserved name')
    if len(name.encode('utf-8')) > 255:
        raise ValidationError('Name is too long')


def to_url_path(path):
    """Return the storage path without a leading '/' for use in URLs ('' = root)."""
    return normalize_path(path).lstrip('/')


def get_storage_path(rel_path, root=None):
    """Map a storage path to an absolute filesystem path inside root."""
    storage = FileSystemStorage(location=root or settings.FILE_STORAGE_ROOT)
    return storage.path(to_url_path(rel_path))


def is_in_archive(full_path):
    """True if full_path is the archive folder itself or anything inside it."""
    archive_root = os.path.abspath(settings.ARCHIVE_ROOT)
    return Path(os.path.abspath(full_path)).is_relative_to(archive_root)


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
    best = 0

    # Permissions are additive: use the highest grant covering this folder
    for perm in get_user_permissions(user):
        if folder.is_relative_to(normalize_path(perm['folder_path'])):
            best = max(best, PERMISSION_RANK[perm['permission']])

    return best >= PERMISSION_RANK[required_permission]


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
        return '0 B'

    size_names = ['B', 'KB', 'MB', 'GB', 'TB']
    i = 0
    while size_bytes >= 1024 and i < len(size_names) - 1:
        size_bytes /= 1024.0
        i += 1

    return f'{size_bytes:.2f} {size_names[i]}'


def get_folder_size(folder_path):
    total_size = 0
    for dirpath, dirnames, filenames in os.walk(folder_path):
        for filename in filenames:
            filepath = os.path.join(dirpath, filename)
            total_size += os.path.getsize(filepath)
    return total_size
