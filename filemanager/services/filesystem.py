import posixpath

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from ..models import File, Folder, FolderPermission
from ..utils import normalize_path

NAME_TAKEN = 'A file/folder with that name already exists'


def get_root():
    return Folder.objects.get(parent=None)


def resolve_folder(path):
    """Return the active folder at path, or None."""
    folder = get_root()
    for name in normalize_path(path).split('/'):
        if name:
            folder = folder.children.filter(name=name, deleted_at__isnull=True).first()
            if folder is None:
                return None
    return folder


def resolve_file(path):
    """Return the active file at path, or None."""
    path = normalize_path(path)
    folder = resolve_folder(posixpath.dirname(path))
    if folder is None:
        return None
    name = posixpath.basename(path)
    return folder.files.filter(name=name, deleted_at__isnull=True).first()


def resolve(path):
    """Return the active folder or file at path, or None."""
    return resolve_folder(path) or resolve_file(path)


def get_path(folder):
    names = []
    while folder.parent_id is not None:
        names.append(folder.name)
        folder = folder.parent
    return '/' + '/'.join(reversed(names))


def get_folder_paths():
    """Map every active folder id to its path, in one query."""
    folders = {
        id: (parent_id, name)
        for id, parent_id, name in Folder.objects.filter(
            deleted_at__isnull=True
        ).values_list('id', 'parent_id', 'name')
    }
    paths = {}

    def path_of(folder_id):
        if folder_id not in paths:
            parent_id, name = folders[folder_id]
            paths[folder_id] = (
                '/' if parent_id is None else posixpath.join(path_of(parent_id), name)
            )
        return paths[folder_id]

    return {folder_id: path_of(folder_id) for folder_id in folders}


def name_taken(folder, name):
    """Names ignore case, like Windows, and a file and a folder in the same
    folder can't share one either."""
    return (
        folder.children.filter(name__iexact=name, deleted_at__isnull=True).exists()
        or folder.files.filter(name__iexact=name, deleted_at__isnull=True).exists()
    )


def create_folder(parent, name, user):
    if name_taken(parent, name):
        raise ValidationError('A folder with that name already exists')
    try:
        with transaction.atomic():
            return Folder.objects.create(parent=parent, name=name, created_by=user)
    except IntegrityError:
        # Created by another request after the check
        raise ValidationError('A folder with that name already exists')


def upload_file(folder, uploaded, name, user):
    """Save an upload into folder; a name in use gets a _1, _2… suffix."""
    file = File(folder=folder, created_by=user)
    file.file.save(name, uploaded, save=False)  # Written to disk under a UUID name
    try:
        file.size = file.file.size
        base, ext = posixpath.splitext(name)
        candidate, counter = name, 1
        while True:
            if not name_taken(folder, candidate):
                file.name = candidate
                try:
                    with transaction.atomic():
                        file.save()
                    return file
                except IntegrityError:
                    # Only a concurrent upload taking the name is retried
                    if not name_taken(folder, candidate):
                        raise
            candidate = f'{base}_{counter}{ext}'
            counter += 1
    except Exception:
        file.file.delete(save=False)  # Don't leave an orphaned file on disk
        raise


def rename_folder(folder, new_name):
    # A case-only change is only "taken" by the folder itself
    if new_name.lower() != folder.name.lower() and name_taken(folder.parent, new_name):
        raise ValidationError(NAME_TAKEN)
    old_path = get_path(folder)
    new_path = posixpath.join(posixpath.dirname(old_path), new_name)
    folder.name = new_name
    try:
        with transaction.atomic():
            folder.save()
            # Permissions are stored as paths, so they follow the rename
            permissions = list(
                FolderPermission.objects.select_for_update().filter(
                    Q(folder_path=old_path) | Q(folder_path__startswith=old_path + '/')
                )
            )
            for perm in permissions:
                perm.folder_path = new_path + perm.folder_path[len(old_path) :]
            FolderPermission.objects.bulk_update(permissions, ['folder_path'])
    except IntegrityError:
        raise ValidationError(NAME_TAKEN)


def rename_file(file, new_name):
    # A case-only change is only "taken" by the file itself
    if new_name.lower() != file.name.lower() and name_taken(file.folder, new_name):
        raise ValidationError(NAME_TAKEN)
    file.name = new_name
    try:
        with transaction.atomic():
            file.save()
    except IntegrityError:
        raise ValidationError(NAME_TAKEN)


def delete_folder(folder, user):
    """Soft-delete folder and everything in it with one timestamp, so they can be
    restored together."""
    path = get_path(folder)
    folder_ids, level = [folder.id], [folder.id]
    while level:
        level = list(
            Folder.objects.filter(
                parent_id__in=level, deleted_at__isnull=True
            ).values_list('id', flat=True)
        )
        folder_ids += level

    now = timezone.now()
    with transaction.atomic():
        Folder.objects.filter(id__in=folder_ids).update(deleted_at=now, deleted_by=user)
        File.objects.filter(folder_id__in=folder_ids, deleted_at__isnull=True).update(
            deleted_at=now, deleted_by=user
        )
        # Grants on a deleted folder must not carry over to a new
        # folder created later with the same name
        FolderPermission.objects.filter(
            Q(folder_path=path) | Q(folder_path__startswith=path + '/')
        ).delete()


def delete_file(file, user):
    file.deleted_at = timezone.now()
    file.deleted_by = user
    file.save(update_fields=['deleted_at', 'deleted_by'])


def walk(folder, prefix=''):
    """Yield (relative path, file) for every active file under folder."""
    for file in folder.files.filter(deleted_at__isnull=True):
        yield posixpath.join(prefix, file.name), file
    for child in folder.children.filter(deleted_at__isnull=True):
        yield from walk(child, posixpath.join(prefix, child.name))
