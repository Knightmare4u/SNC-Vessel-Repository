import os
import posixpath
from datetime import datetime, timezone
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from filemanager.models import File, Folder, FolderPermission
from filemanager.services import filesystem
from filemanager.utils import normalize_path

# Top-level folders that are not imported: the old archive, and new uploads
SKIPPED_FOLDERS = {'ARCHIVE', 'objects'}
BATCH_SIZE = 1000
MAX_PATH = File._meta.get_field('file').max_length


def get_times(stat):
    # The same created/modified times the file browser showed from the disk
    return (
        datetime.fromtimestamp(stat.st_ctime, timezone.utc),
        datetime.fromtimestamp(stat.st_mtime, timezone.utc),
    )


class Command(BaseCommand):
    help = (
        'Index the files already in FILE_STORAGE_ROOT into Folder and File, '
        'leaving them where they are. Run once, during the switch-over.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true', help='scan and report; save nothing'
        )

    def handle(self, *args, dry_run, **options):
        root = filesystem.get_root()
        if Folder.objects.exclude(pk=root.pk).exists() or File.objects.exists():
            raise CommandError('Files have already been imported')

        folder_entries, file_entries, errors = scan(Path(settings.FILE_STORAGE_ROOT))
        if errors:
            for error in errors[:20]:
                self.stderr.write(str(error))
            raise CommandError(f'{len(errors)} problems found; nothing was imported')

        with transaction.atomic():
            # os.walk lists parents before their children
            folders = {'': root}
            for path, _ in folder_entries:
                folders[path] = Folder.objects.create(
                    parent=folders[posixpath.dirname(path)],
                    name=posixpath.basename(path),
                )
            new_folders = [folders[path] for path, _ in folder_entries]

            files = File.objects.bulk_create(
                [
                    File(
                        folder=folders[posixpath.dirname(path)],
                        name=posixpath.basename(path),
                        file=path,  # Left where it is
                        size=size,
                    )
                    for path, size, _ in file_entries
                ],
                batch_size=BATCH_SIZE,
            )

            # Creating stamps the current time; put the times from the disk back
            for model, objects, entries in [
                (Folder, new_folders, folder_entries),
                (File, files, file_entries),
            ]:
                for obj, entry in zip(objects, entries):
                    obj.created_at, obj.updated_at = entry[-1]
                model.objects.bulk_update(
                    objects, ['created_at', 'updated_at'], batch_size=BATCH_SIZE
                )

            self.report_missing_permissions()
            if dry_run:
                transaction.set_rollback(True)

        total = sum(size for _, size, _ in file_entries)
        self.stdout.write(
            self.style.SUCCESS(
                f'{"Would import" if dry_run else "Imported"} {len(new_folders)} '
                f'folders and {len(files)} files ({total / 1024**3:.1f} GB)'
            )
        )

    def report_missing_permissions(self):
        # Grants on folders that weren't imported (e.g. already gone, or the archive)
        folder_paths = set(filesystem.get_folder_paths().values())
        for perm in FolderPermission.objects.select_related('user'):
            if normalize_path(perm.folder_path) not in folder_paths:
                self.stdout.write(
                    self.style.WARNING(
                        f'Permission for {perm.user} points at a missing folder: '
                        f'{perm.folder_path}'
                    )
                )


def scan(storage_root):
    """Return (folders, files, errors) found under storage_root, as paths
    relative to it."""
    folder_entries, file_entries, errors = [], [], []
    for dirpath, dirnames, filenames in os.walk(storage_root, onerror=errors.append):
        here = Path(dirpath)
        rel_dir = here.relative_to(storage_root).as_posix()
        if rel_dir == '.':
            rel_dir = ''
            dirnames[:] = [name for name in dirnames if name not in SKIPPED_FOLDERS]
        else:
            try:
                folder_entries.append((rel_dir, get_times(here.stat())))
            except OSError as e:
                errors.append(e)

        for name in filenames:
            path = posixpath.join(rel_dir, name)
            if len(path) > MAX_PATH:
                errors.append(f'Path longer than {MAX_PATH} characters: {path}')
                continue
            try:
                stat = (here / name).stat()
            except OSError as e:
                errors.append(e)
                continue
            file_entries.append((path, stat.st_size, get_times(stat)))
    return folder_entries, file_entries, errors
