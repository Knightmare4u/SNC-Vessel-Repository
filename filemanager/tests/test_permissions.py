import os

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from ..models import FolderPermission
from ..services import filesystem
from ..utils import has_permission, normalize_path, validate_name
from .base import StorageTestCase

LEVELS = ('read', 'write', 'admin')


class NameAndPathTests(SimpleTestCase):
    def test_validate_name(self):
        # Same rules as Windows, where the files end up
        # fmt: off
        valid = ['VSL SHARE BS2', 'My Report #3.pdf', '2026 報告', '.hidden', 'CONSOLE.txt']
        invalid = [
            '', '.', '..', ' leading', 'trailing ', 'ends with dot.',
            'a/b', 'a\\b', 'a:b', 'a*b', 'a?b', 'a"b', 'a<b', 'a>b', 'a|b', 'tab\there',
            'CON', 'nul.txt', 'com1', 'LPT9.log', 'x' * 256,
        ]
        # fmt: on
        for name in valid:
            with self.subTest(name=name):
                validate_name(name)
        for name in invalid:
            with self.subTest(name=name), self.assertRaises(ValidationError):
                validate_name(name)

    def test_normalize_path_never_leaves_the_root(self):
        for raw, expected in {
            '': '/',
            '///': '/',
            'a/b/': '/a/b',
            '/a//b': '/a/b',
            'a/../b': '/b',
            '../../etc/passwd': '/etc/passwd',
            'a\\..\\b': '/b',
        }.items():
            with self.subTest(raw=raw):
                self.assertEqual(normalize_path(raw), expected)


class HasPermissionTests(TestCase):
    def make_user(self, *perms):
        user = User.objects.create_user('u', password='pw')
        for folder_path, permission in perms:
            FolderPermission.objects.create(
                user=user, folder_path=folder_path, permission=permission
            )
        return user

    def test_grant_covers_its_folder_and_subfolders_only(self):
        user = self.make_user(('/VSL SHARE ALL', 'write'))
        for path, expected in [
            ('/VSL SHARE ALL', True),
            ('VSL SHARE ALL', True),
            ('/VSL SHARE ALL/2026/report', True),
            ('/VSL SHARE ALL OMC', False),  # Same prefix, other folder
            ('/VSL SHARE ALL OMC/sub', False),
            ('/VSL SHARE ALL/../B', False),
            ('/', False),
        ]:
            with self.subTest(path=path):
                self.assertEqual(has_permission(user, path, 'read'), expected)

    def test_levels_and_additive_grants(self):
        user = self.make_user(
            ('/R', 'read'), ('/W', 'write'), ('/X', 'admin'), ('/', 'read')
        )
        for path, expected in [
            ('/R', [True, False, False]),
            ('/W', [True, True, False]),
            ('/X/sub', [True, True, True]),
            ('/other', [True, False, False]),  # From the grant on '/'
        ]:
            with self.subTest(path=path):
                self.assertEqual(
                    [has_permission(user, path, level) for level in LEVELS], expected
                )


class PathTraversalTests(StorageTestCase):
    # Attack URLs are written out literally on purpose: they mimic raw requests
    # (e.g. "%2e%2e" for "..") that reverse() would never produce.

    def setUp(self):
        super().setUp()
        with open(os.path.join(self.tmp, 'secret.env'), 'wb') as f:
            f.write(b'SECRET_KEY=top-secret')
        self.make_file('A/a.txt')
        self.make_file('B/b.txt')

    def test_requests_cannot_leave_the_storage_root(self):
        self.login(self.make_user('u', [('/', 'read')]))
        response = self.client.get('/download/%2e%2e/secret.env/')
        self.assertEqual(response.status_code, 404)
        response = self.client.get('/preview/%2e%2e/secret.env/')
        self.assertEqual(response.status_code, 404)
        # '../secret.env' normalizes to '/secret.env' inside the storage root
        response = self.client.post(
            reverse('bulk_download'), {'paths': ['../secret.env']}
        )
        self.assertEqual(response.status_code, 404)

    def test_requests_cannot_jump_to_another_folder(self):
        self.login(self.make_user('u', [('/A', 'read')]))
        response = self.client.get('/download/A/%2e%2e/B/b.txt/')
        self.assertEqual(response.status_code, 403)
        response = self.client.get(
            reverse('search_files'), {'q': 'b', 'folder': 'A/../B'}
        )
        self.assertEqual(response.status_code, 403)

    def test_new_folder_name_cannot_be_a_path(self):
        self.login(self.make_user('u', [('/A', 'write')]))
        for name in ['', '.', '..', 'a/b', '../B/x', 'a\\b', '..\\B\\x']:
            with self.subTest(name=name):
                response = self.post_json(
                    reverse('create_folder'), {'folder_path': 'A', 'folder_name': name}
                )
                self.assertEqual(response.status_code, 400)
        self.assertIsNone(filesystem.resolve_folder('B/x'))

    def test_root_cannot_be_renamed_or_deleted(self):
        self.login(self.make_user('su', superuser=True))
        response = self.post_json(reverse('delete_file'), {'path': 'A/..'})
        self.assertEqual(response.status_code, 400)
        response = self.post_json(
            reverse('rename_file'), {'old_path': 'A/..', 'new_name': 'x'}
        )
        self.assertEqual(response.status_code, 400)
        self.assertIsNotNone(filesystem.resolve_folder('A'))


class ArchiveTests(StorageTestCase):
    def setUp(self):
        super().setUp()
        self.make_file('ARCHIVE/A/old.txt')
        self.make_file('A/a.txt')

    def test_archive_is_read_only(self):
        self.login(self.make_user('su', superuser=True))
        upload = SimpleUploadedFile('x.txt', b'x')
        requests = {
            'upload': lambda: self.client.post(
                reverse('upload_file'), {'folder_path': 'ARCHIVE/A', 'files': upload}
            ),
            'create folder': lambda: self.post_json(
                reverse('create_folder'),
                {'folder_path': 'ARCHIVE', 'folder_name': 'new'},
            ),
            'rename inside': lambda: self.post_json(
                reverse('rename_file'),
                {'old_path': 'ARCHIVE/A/old.txt', 'new_name': 'x.txt'},
            ),
            'rename archive': lambda: self.post_json(
                reverse('rename_file'), {'old_path': 'ARCHIVE', 'new_name': 'OLD'}
            ),
            'delete inside': lambda: self.post_json(
                reverse('delete_file'), {'path': 'ARCHIVE/A/old.txt'}
            ),
            'delete archive': lambda: self.post_json(
                reverse('delete_file'), {'path': 'ARCHIVE'}
            ),
        }
        for name, request in requests.items():
            with self.subTest(name):
                response = request()
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.json(), {'error': 'The archive is read-only'})
        self.assertIsNotNone(filesystem.resolve_file('ARCHIVE/A/old.txt'))
        self.assertIsNone(filesystem.resolve_file('ARCHIVE/A/x.txt'))
        self.assertIsNone(filesystem.resolve_folder('ARCHIVE/new'))

    def test_superusers_can_browse_and_download_the_archive(self):
        self.login(self.make_user('su', superuser=True))
        response = self.client.get(reverse('file_browser', args=['ARCHIVE/A']))
        self.assertEqual(response.status_code, 200)
        url = reverse('download_file', args=['ARCHIVE/A/old.txt'])
        self.assertEqual(self.read_response(self.client.get(url)), b'hello')

    def test_archive_is_hidden_from_other_users(self):
        # Access to the root covers every folder, but not the archive
        self.login(self.make_user('root_admin', [('/', 'admin')]))

        response = self.client.get(reverse('file_browser'))
        self.assertEqual([item['name'] for item in response.context['items']], ['A'])
        response = self.client.get(reverse('file_browser', args=['ARCHIVE/A']))
        self.assertRedirects(response, reverse('dashboard'))
        url = reverse('download_file', args=['ARCHIVE/A/old.txt'])
        self.assertEqual(self.client.get(url).status_code, 403)
        response = self.client.get(reverse('search_files'), {'q': 'old'})
        self.assertEqual(response.json()['results'], [])
