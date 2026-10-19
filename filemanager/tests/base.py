import json
import os
import posixpath
import shutil
import tempfile
from unittest import mock

from django.conf import settings
from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.core.files.storage import FileSystemStorage
from django.test import TestCase, override_settings

from ..models import File, Folder, FolderPermission, UserProfile
from ..services import filesystem


def no_static_manifest(cls):
    """For tests of pages that use {% static %}, which otherwise needs the
    manifest that collectstatic writes."""
    return override_settings(
        STORAGES={
            **settings.STORAGES,
            'staticfiles': {
                'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'
            },
        }
    )(cls)


class StorageTestCase(TestCase):
    """Runs each test against an empty temporary file storage."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.storage = os.path.join(self.tmp, 'vessel_files')
        os.makedirs(self.storage)
        override = override_settings(
            FILE_STORAGE_ROOT=self.storage,
            ARCHIVE_ROOT=os.path.join(self.storage, 'ARCHIVE'),
        )
        override.enable()
        self.addCleanup(override.disable)
        # The field's storage is created once at import, so swap it directly
        patcher = mock.patch.object(
            File._meta.get_field('file'),
            'storage',
            FileSystemStorage(location=self.storage),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def make_dir(self, rel):
        folder = filesystem.get_root()
        for name in rel.split('/'):
            if name:
                folder, _ = Folder.objects.get_or_create(
                    parent=folder, name=name, deleted_at=None
                )
        return folder

    def make_file(self, rel, content=b'hello'):
        file = File(
            folder=self.make_dir(posixpath.dirname(rel)),
            name=posixpath.basename(rel),
            size=len(content),
        )
        file.file.save(file.name, ContentFile(content))
        return file

    def read(self, rel):
        with filesystem.resolve_file(rel).file.open('rb') as f:
            return f.read()

    @staticmethod
    def make_user(username, perms=(), superuser=False, password_changed=True):
        if superuser:
            user = User.objects.create_superuser(username, f'{username}@x.com', 'pw')
        else:
            user = User.objects.create_user(username, password='pw')
        UserProfile.objects.create(user=user, password_changed=password_changed)
        for folder_path, permission in perms:
            FolderPermission.objects.create(
                user=user, folder_path=folder_path, permission=permission
            )
        return user

    def login(self, user):
        self.client.force_login(user)

    def post_json(self, url, data):
        return self.client.post(url, json.dumps(data), content_type='application/json')

    def read_response(self, response):
        content = b''.join(response.streaming_content)
        response.close()
        return content
