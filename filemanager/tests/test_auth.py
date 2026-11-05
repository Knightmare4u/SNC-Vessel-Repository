from django.contrib.auth.models import User
from django.urls import reverse

from .base import StorageTestCase


class PasswordChangeRequiredTests(StorageTestCase):
    def test_user_without_profile_must_change_password(self):
        # e.g. accounts made by createsuperuser
        self.login(User.objects.create_superuser('admin', 'a@x.com', 'pw'))
        self.assertRedirects(
            self.client.get(reverse('dashboard')),
            reverse('change_password'),
            fetch_redirect_response=False,
        )

    def test_logout_is_allowed(self):
        self.login(self.make_user('u', password_changed=False))
        response = self.client.post(reverse('logout'))
        self.assertRedirects(response, reverse('login'), fetch_redirect_response=False)
