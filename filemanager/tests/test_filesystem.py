import io
import os
from datetime import datetime, timezone

from django.core.exceptions import ValidationError
from django.core.management import CommandError, call_command
from django.db import IntegrityError, transaction

from ..models import File, Folder
from ..services import filesystem
from .base import StorageTestCase


class FilesystemServiceTests(StorageTestCase):
    def setUp(self):
        super().setUp()
        self.user = self.make_user('u')
        self.a = self.make_dir('A')

    def test_database_rejects_the_same_active_name_in_any_case(self):
        self.make_dir('A/sub')
        self.make_file('A/a.txt')
        with self.assertRaises(IntegrityError), transaction.atomic():
            Folder.objects.create(parent=self.a, name='SUB')
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.make_file('A/A.TXT')

    def test_deleted_name_can_be_reused(self):
        filesystem.delete_folder(self.make_dir('A/sub'), self.user)
        filesystem.delete_file(self.make_file('A/a.txt'), self.user)
        filesystem.create_folder(self.a, 'sub', self.user)
        self.make_file('A/a.txt', b'new')
        self.assertEqual(self.read('A/a.txt'), b'new')

    def test_file_and_folder_cannot_share_a_name(self):
        self.make_file('A/report')
        with self.assertRaises(ValidationError):
            filesystem.create_folder(self.a, 'report', self.user)
        sub = self.make_dir('A/sub')
        with self.assertRaises(ValidationError):
            filesystem.rename_folder(sub, 'report')

    def test_delete_folder_marks_everything_inside_together(self):
        earlier = self.make_file('A/sub/old.txt')
        filesystem.delete_file(earlier, self.user)
        earlier.refresh_from_db()
        file = self.make_file('A/sub/deep/a.txt')

        filesystem.delete_folder(filesystem.resolve_folder('A/sub'), self.user)

        sub = Folder.objects.get(name='sub')
        deep = Folder.objects.get(name='deep')
        file.refresh_from_db()
        self.assertIsNotNone(sub.deleted_at)
        self.assertEqual(
            [deep.deleted_at, file.deleted_at], [sub.deleted_at, sub.deleted_at]
        )
        self.assertEqual(
            [sub.deleted_by, deep.deleted_by, file.deleted_by], [self.user] * 3
        )
        # A file deleted before keeps its own time, so it isn't restored with the folder
        self.assertEqual(File.objects.get(pk=earlier.pk).deleted_at, earlier.deleted_at)
        self.assertIsNone(filesystem.resolve('A/sub'))
        self.assertIsNone(filesystem.resolve('A/sub/deep/a.txt'))
        self.assertIsNotNone(filesystem.resolve_folder('A'))


class ImportFilesTests(StorageTestCase):
    """import_files indexes the files already on disk and leaves them in place."""

    def setUp(self):
        super().setUp()
        for rel, content in [
            ('r.txt', b'root'),
            ('HR/a.pdf', b'pdf'),
            ('HR/Contracts/c.docx', b'doc'),
            ('ARCHIVE/HR/old.pdf', b'old'),
            ('objects/ab/abcdef', b'upload'),
        ]:
            path = os.path.join(self.storage, *rel.split('/'))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'wb') as f:
                f.write(content)
            os.utime(path, (1_700_000_000, 1_700_000_000))
        os.makedirs(os.path.join(self.storage, 'Empty'))

    def snapshot(self):
        result = {}
        for dirpath, dirnames, filenames in os.walk(self.storage):
            for name in dirnames + filenames:
                path = os.path.join(dirpath, name)
                stat = os.stat(path)
                result[os.path.relpath(path, self.storage)] = (
                    stat.st_size,
                    stat.st_mtime,
                )
        return result

    def import_files(self, *args):
        call_command('import_files', *args, stdout=io.StringIO())

    def test_files_are_indexed_where_they_are(self):
        before = self.snapshot()
        self.import_files()

        self.assertEqual(self.snapshot(), before)  # Nothing on disk changes
        file = filesystem.resolve_file('HR/a.pdf')
        self.assertEqual(file.file.name, 'HR/a.pdf')
        self.assertEqual(file.size, 3)
        self.assertEqual(
            file.updated_at, datetime.fromtimestamp(1_700_000_000, timezone.utc)
        )
        self.assertEqual(self.read('r.txt'), b'root')
        self.assertEqual(self.read('HR/Contracts/c.docx'), b'doc')
        self.assertIsNotNone(filesystem.resolve_folder('Empty'))
        # The old archive and new uploads are not imported
        self.assertIsNone(filesystem.resolve('ARCHIVE'))
        self.assertIsNone(filesystem.resolve('objects'))
        self.assertEqual(File.objects.count(), 3)

    def test_dry_run_saves_nothing(self):
        self.import_files('--dry-run')
        self.assertFalse(File.objects.exists())
        self.assertEqual(Folder.objects.count(), 1)

    def test_import_runs_only_once(self):
        self.import_files()
        with self.assertRaises(CommandError):
            self.import_files()
        self.assertEqual(File.objects.count(), 3)

    def test_deleting_a_row_keeps_the_original_file(self):
        self.import_files()
        file = filesystem.resolve_file('HR/a.pdf')
        with self.captureOnCommitCallbacks(execute=True):
            file.delete()
        self.assertTrue(os.path.isfile(os.path.join(self.storage, 'HR', 'a.pdf')))
