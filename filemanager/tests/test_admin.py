from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from ..admin import FolderPermissionForm
from ..models import FolderPermission, UserProfile
from .base import StorageTestCase, no_static_manifest


@no_static_manifest
class UserAdminTests(StorageTestCase):
    def setUp(self):
        super().setUp()
        self.login(self.make_user('admin', superuser=True))

    def run_action(self, action, user):
        return self.client.post(
            reverse('admin:auth_user_changelist'),
            {'action': action, '_selected_action': [user.pk]},
        )

    def test_user_list_query_count_does_not_grow_with_users(self):
        url = reverse('admin:auth_user_changelist')

        def count_queries():
            with CaptureQueriesContext(connection) as ctx:
                self.assertEqual(self.client.get(url).status_code, 200)
            return len(ctx.captured_queries)

        self.make_user('u1', [('/A', 'read'), ('/B', 'write')])
        baseline = count_queries()
        for i in range(2, 7):
            self.make_user(f'u{i}', [('/A', 'read'), ('/B', 'write')])
        self.assertEqual(count_queries(), baseline)

    def test_root_access_actions_upgrade_but_never_downgrade(self):
        reader = self.make_user('reader', [('/', 'read')])
        full = self.make_user('full', [('/', 'admin')])

        self.run_action('grant_full_access', reader)
        self.run_action('grant_read_access', full)

        for user in (reader, full):
            with self.subTest(user=user.username):
                self.assertEqual(
                    FolderPermission.objects.get(user=user, folder_path='/').permission,
                    'admin',
                )

    def test_force_password_change(self):
        user = self.make_user('u1')
        self.run_action('reset_password_required', user)
        self.assertFalse(UserProfile.objects.get(user=user).password_changed)


class FolderPermissionFormTests(StorageTestCase):
    def setUp(self):
        super().setUp()
        self.make_dir('A/sub')
        self.make_dir('B')
        self.make_dir('ARCHIVE/A')
        self.user = self.make_user('u')

    def form(self, folder_path, instance=None):
        data = {'user': self.user.pk, 'folder_path': folder_path, 'permission': 'read'}
        return FolderPermissionForm(data, instance=instance)

    def test_choices_are_existing_folders_outside_the_archive(self):
        widget = FolderPermissionForm().fields['folder_path'].widget
        self.assertEqual(
            [value for value, _ in widget.choices], ['', '/', '/A', '/A/sub', '/B']
        )

    def test_archive_and_missing_folders_are_rejected(self):
        for folder_path in ['/ARCHIVE', '/ARCHIVE/A', '/missing']:
            with self.subTest(folder_path=folder_path):
                form = self.form(folder_path)
                self.assertFalse(form.is_valid())
                self.assertIn('folder_path', form.errors)

    def test_path_is_normalized(self):
        form = self.form('A/sub/')
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data['folder_path'], '/A/sub')

    def test_unchanged_missing_folder_still_saves(self):
        # A grant on a folder deleted since can still be edited, e.g. its level
        perm = FolderPermission.objects.create(
            user=self.user, folder_path='/gone', permission='read'
        )
        form = self.form('/gone', instance=perm)
        self.assertTrue(form.is_valid(), form.errors)
