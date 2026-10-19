import io
import os
import zipfile
from unittest import mock

from django.core.files.uploadedfile import InMemoryUploadedFile, SimpleUploadedFile
from django.test import Client
from django.urls import reverse

from ..models import FileActivity, FolderPermission
from ..services import filesystem
from .base import StorageTestCase


class FileBrowserTests(StorageTestCase):
    def test_missing_folder_or_a_file_path_returns_404(self):
        self.make_file('A/a.txt')
        self.login(self.make_user('su', superuser=True))
        for path in ['does-not-exist', 'A/a.txt']:
            with self.subTest(path=path):
                response = self.client.get(reverse('file_browser', args=[path]))
                self.assertEqual(response.status_code, 404)
        self.assertIsNone(filesystem.resolve_folder('does-not-exist'))

    def test_no_permission_redirects_to_dashboard(self):
        self.make_dir('A')
        self.login(self.make_user('u', [('/B', 'read')]))
        response = self.client.get(reverse('file_browser', args=['A']))
        self.assertRedirects(response, reverse('dashboard'))

    def test_legacy_double_slash_url_still_works(self):
        self.make_dir('A')
        self.login(self.make_user('u', [('/A', 'read')]))
        response = self.client.get('/browser//A/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['current_path'], 'A')

    def test_deleted_items_are_hidden(self):
        user = self.make_user('su', superuser=True)
        self.make_file('A/a.txt')
        filesystem.delete_file(self.make_file('A/gone.txt'), user)
        filesystem.delete_folder(self.make_dir('A/gone'), user)
        self.make_file('A/sub/b.txt')
        filesystem.delete_file(self.make_file('A/sub/gone.txt'), user)
        self.login(user)

        response = self.client.get(reverse('file_browser', args=['A']))

        items = {item['name']: item for item in response.context['items']}
        self.assertEqual(sorted(items), ['a.txt', 'sub'])
        self.assertEqual(items['sub']['size'], '1 items')
        self.assertEqual(response.context['file_count'], 1)
        self.assertEqual(response.context['folder_count'], 1)


class FileOperationTestCase(StorageTestCase):
    """A user with write access to /A, which holds a.txt."""

    @classmethod
    def setUpTestData(cls):
        cls.user = cls.make_user('u', [('/A', 'write')])

    def setUp(self):
        super().setUp()
        self.make_file('A/a.txt')
        self.login(self.user)

    def make_admin(self):
        FolderPermission.objects.filter(user=self.user).update(permission='admin')

    def upload(
        self, folder_path, name='new.txt', content=b'data', client=None, **extra
    ):
        return (client or self.client).post(
            reverse('upload_file'),
            {'folder_path': folder_path, 'files': SimpleUploadedFile(name, content)},
            **extra,
        )

    def uploaded_name(self, name):
        """Upload name into A and return the name it was saved as."""
        response = self.upload('A', name=name)
        return response.json()['uploaded_files'][0]['name']

    def rename(self, old_path, new_name):
        return self.post_json(
            reverse('rename_file'), {'old_path': old_path, 'new_name': new_name}
        )


class UploadTests(FileOperationTestCase):
    def test_upload_keeps_the_extension_on_disk(self):
        response = self.upload('A')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.read('A/new.txt'), b'data')
        # So the file still opens straight from the shared folder
        stored = filesystem.resolve_file('A/new.txt').file.name
        self.assertRegex(stored, r'^objects/[0-9a-f]{2}/[0-9a-f]{32}\.txt$')

    def test_upload_sanitizes_the_file_name(self):
        for name, expected in [
            ('My Report #3.pdf', 'My_Report_3.pdf'),
            ('bad:name.txt', 'badname.txt'),
        ]:
            with self.subTest(name=name):
                self.assertEqual(self.uploaded_name(name), expected)

    def test_name_in_use_gets_a_suffix(self):
        self.upload('A', name='dup.txt')
        self.make_dir('A/folder.txt')
        for name, expected in [
            ('dup.txt', 'dup_1.txt'),
            ('A.TXT', 'A_1.TXT'),  # Names ignore case, like Windows
            ('folder.txt', 'folder_1.txt'),  # Taken by a folder
        ]:
            with self.subTest(name=name):
                self.assertEqual(self.uploaded_name(name), expected)

    def test_upload_never_overwrites_a_file_created_after_the_name_check(self):
        # Simulate a concurrent upload taking a.txt after the name check:
        # free at the check, taken when re-checked after the insert fails
        with mock.patch(
            'filemanager.services.filesystem.name_taken',
            side_effect=[False, True, False],
        ):
            response = self.upload('A', name='a.txt', content=b'new')
        self.assertEqual(response.json()['uploaded_files'][0]['name'], 'a_1.txt')
        self.assertEqual(self.read('A/a.txt'), b'hello')
        self.assertEqual(self.read('A/a_1.txt'), b'new')

    def test_failed_upload_leaves_no_file(self):
        def failing_chunks(self, chunk_size=None):
            yield b'part'
            raise OSError('disk full')

        with mock.patch.object(InMemoryUploadedFile, 'chunks', failing_chunks):
            response = self.upload('A', name='big.bin')
        self.assertEqual(response.status_code, 500)
        self.assertIsNone(filesystem.resolve_file('A/big.bin'))
        # No half-written file either: only a.txt is on disk
        on_disk = [name for _, _, names in os.walk(self.storage) for name in names]
        self.assertEqual(len(on_disk), 1)

    def test_an_invalid_name_rejects_the_whole_batch(self):
        response = self.client.post(
            reverse('upload_file'),
            {
                'folder_path': 'A',
                'files': [
                    SimpleUploadedFile('ok.txt', b'x'),
                    SimpleUploadedFile('???', b'x'),
                ],
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertIsNone(filesystem.resolve_file('A/ok.txt'))

    def test_upload_needs_write_access_and_an_existing_folder(self):
        self.make_dir('B')
        FolderPermission.objects.create(
            user=self.user, folder_path='/B', permission='read'
        )
        self.assertEqual(self.upload('B').status_code, 403)
        self.assertEqual(self.upload('A/missing').status_code, 404)
        self.assertIsNone(filesystem.resolve_folder('A/missing'))

    def test_upload_requires_csrf_token(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        self.assertEqual(self.upload('A', client=client).status_code, 403)

        client.get(reverse('file_browser', args=['A']))
        token = client.cookies['csrftoken'].value
        response = self.upload('A', client=client, HTTP_X_CSRFTOKEN=token)
        self.assertEqual(response.status_code, 200)

    def test_create_folder(self):
        response = self.post_json(
            reverse('create_folder'), {'folder_path': 'A', 'folder_name': 'New Folder'}
        )
        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(filesystem.resolve_folder('A/New Folder'))
        # The parent may have been deleted while the page was open
        response = self.post_json(
            reverse('create_folder'), {'folder_path': 'A/gone', 'folder_name': 'new'}
        )
        self.assertEqual(response.status_code, 404)


class RenameTests(FileOperationTestCase):
    def test_folder_names_are_kept_and_file_names_sanitized(self):
        self.make_dir('A/VSL SHARE BS')
        response = self.rename('A/VSL SHARE BS', 'VSL SHARE BS2')
        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(filesystem.resolve_folder('A/VSL SHARE BS2'))

        self.assertEqual(self.rename('A/a.txt', 'My Report?.txt').status_code, 200)
        self.assertIsNotNone(filesystem.resolve_file('A/My_Report.txt'))

    def test_invalid_names_are_rejected(self):
        self.make_dir('A/sub')
        for old_path, new_name in [('A/sub', 'a:b'), ('A/a.txt', '???')]:
            with self.subTest(old_path=old_path):
                self.assertEqual(self.rename(old_path, new_name).status_code, 400)
                self.assertIsNotNone(filesystem.resolve(old_path))

    def test_case_only_rename(self):
        self.make_dir('A/sub')
        for old_path, new_name in [('A/a.txt', 'A.txt'), ('A/sub', 'SUB')]:
            with self.subTest(old_path=old_path):
                self.assertEqual(self.rename(old_path, new_name).status_code, 200)
                self.assertEqual(filesystem.resolve(f'A/{new_name}').name, new_name)

    def test_rename_onto_another_item_is_rejected(self):
        self.make_file('A/b.txt', b'keep')
        self.assertEqual(self.rename('A/a.txt', 'b.txt').status_code, 400)
        self.assertEqual(self.read('A/b.txt'), b'keep')
        self.assertEqual(self.read('A/a.txt'), b'hello')

    def test_permissions_follow_a_renamed_folder(self):
        self.make_dir('VSL SHARE ALL')
        self.make_dir('VSL SHARE ALL OMC')
        other = self.make_user(
            'other',
            [
                ('/VSL SHARE ALL', 'read'),
                ('/VSL SHARE ALL/sub', 'read'),
                ('/VSL SHARE ALL OMC', 'read'),
            ],
        )
        self.login(self.make_user('su', superuser=True))
        self.assertEqual(self.rename('VSL SHARE ALL', 'VSL_ALL').status_code, 200)
        self.assertEqual(
            sorted(
                FolderPermission.objects.filter(user=other).values_list(
                    'folder_path', flat=True
                )
            ),
            ['/VSL SHARE ALL OMC', '/VSL_ALL', '/VSL_ALL/sub'],
        )


class DeleteTests(FileOperationTestCase):
    def test_delete_requires_admin_permission(self):
        response = self.post_json(reverse('delete_file'), {'path': 'A/a.txt'})
        self.assertEqual(response.status_code, 403)
        self.assertIsNotNone(filesystem.resolve_file('A/a.txt'))

    def test_delete_only_marks_the_file_deleted(self):
        self.make_admin()
        file = filesystem.resolve_file('A/a.txt')
        response = self.post_json(reverse('delete_file'), {'path': 'A/a.txt'})
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(filesystem.resolve_file('A/a.txt'))
        file.refresh_from_db()
        self.assertIsNotNone(file.deleted_at)
        self.assertEqual(file.deleted_by, self.user)
        self.assertTrue(os.path.isfile(file.file.path))

    def test_deleting_a_folder_removes_its_permissions(self):
        self.make_admin()
        self.make_dir('A/sub/deeper')
        self.make_dir('A/sub2')
        other = self.make_user(
            'other',
            [('/A/sub', 'read'), ('/A/sub/deeper', 'write'), ('/A/sub2', 'read')],
        )
        response = self.post_json(reverse('delete_file'), {'path': 'A/sub'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            list(
                FolderPermission.objects.filter(user=other).values_list(
                    'folder_path', flat=True
                )
            ),
            ['/A/sub2'],
        )


class DownloadTests(FileOperationTestCase):
    def test_download(self):
        response = self.client.get(reverse('download_file', args=['A/a.txt']))
        self.assertEqual(self.read_response(response), b'hello')

    def test_names_with_url_characters(self):
        # e.g. files copied straight onto the share
        self.make_file('A/Report #3/Invoice 100%.pdf')
        url = reverse('download_file', args=['A/Report #3/Invoice 100%.pdf'])
        self.assertEqual(self.read_response(self.client.get(url)), b'hello')

        response = self.client.get(reverse('search_files'), {'q': 'invoice'})
        [result] = response.json()['results']
        self.assertEqual(result['download_url'], url)

    def test_preview_and_download_check_access_and_existence(self):
        self.make_file('B/b.txt')
        for view in ['file_preview', 'download_file']:
            with self.subTest(view=view):
                response = self.client.get(reverse(view, args=['B/b.txt']))
                self.assertEqual(response.status_code, 403)
                response = self.client.get(reverse(view, args=['A/missing.txt']))
                self.assertEqual(response.status_code, 404)

    def test_file_missing_on_disk_returns_404(self):
        # e.g. removed from the shared folder outside the site
        os.remove(filesystem.resolve_file('A/a.txt').file.path)
        for view in ['file_preview', 'download_file']:
            with self.subTest(view=view), self.assertLogs('filemanager.views', 'ERROR'):
                response = self.client.get(reverse(view, args=['A/a.txt']))
                self.assertEqual(response.status_code, 404)

        with self.assertLogs('filemanager.views', 'ERROR'):
            response = self.client.post(
                reverse('bulk_download'), {'paths': ['A/a.txt']}
            )
        self.assertEqual(response.status_code, 404)
        self.assertFalse(FileActivity.objects.filter(activity_type='download').exists())

    def test_bulk_download_zip_layout(self):
        self.make_file('A/sub/a.txt')
        self.make_file('A/sub/deep/b.txt')
        for paths, expected in [
            # Selected items at the top level, folders with what is inside them
            (['A/a.txt', 'A/sub'], ['a.txt', 'sub/a.txt', 'sub/deep/b.txt']),
            # Same-named files, e.g. search results from different folders
            (['A/a.txt', 'A/sub/a.txt'], ['a.txt', 'a_1.txt']),
        ]:
            with self.subTest(paths=paths):
                response = self.client.post(reverse('bulk_download'), {'paths': paths})
                zip_file = zipfile.ZipFile(io.BytesIO(self.read_response(response)))
                self.assertEqual(sorted(zip_file.namelist()), expected)

    def test_bulk_download_is_all_or_nothing(self):
        self.make_file('B/b.txt')
        for paths, status in [
            (['A/a.txt', 'B/b.txt'], 403),  # No access to B
            (['A/a.txt', 'A/missing.txt'], 404),
        ]:
            with self.subTest(paths=paths):
                response = self.client.post(reverse('bulk_download'), {'paths': paths})
                self.assertEqual(response.status_code, status)
        self.assertFalse(FileActivity.objects.filter(activity_type='download').exists())


class SearchTests(FileOperationTestCase):
    def test_search_covers_only_folders_the_user_can_read(self):
        self.make_file('B/a.txt')
        for params in [{'q': 'a.txt'}, {'q': 'a.txt', 'folder': 'A'}]:
            with self.subTest(params=params):
                response = self.client.get(reverse('search_files'), params)
                paths = [result['path'] for result in response.json()['results']]
                self.assertEqual(paths, ['A/a.txt'])
