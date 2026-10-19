from django.contrib.auth.models import User
from django.urls import reverse

from .base import StorageTestCase


class LoginTests(StorageTestCase):
    def test_login(self):
        self.make_user('u')
        self.client.post(reverse('login'), {'username': 'u', 'password': 'wrong'})
        self.assertNotIn('_auth_user_id', self.client.session)

        response = self.client.post(
            reverse('login'), {'username': 'u', 'password': 'pw'}
        )
        self.assertRedirects(response, reverse('dashboard'))


class PasswordChangeRequiredTests(StorageTestCase):
    def test_redirects_until_password_is_changed(self):
        self.login(self.make_user('u', [('/', 'read')], password_changed=False))
        for url in [
            reverse('dashboard'),
            reverse('file_browser'),
            reverse('search_files') + '?q=a',
        ]:
            with self.subTest(url=url):
                self.assertRedirects(
                    self.client.get(url),
                    reverse('change_password'),
                    fetch_redirect_response=False,
                )

        response = self.client.post(
            reverse('change_password'),
            {
                'old_password': 'pw',
                'new_password1': 'a-much-longer-password-2026',
                'new_password2': 'a-much-longer-password-2026',
            },
        )
        self.assertRedirects(response, reverse('dashboard'))

    def test_user_without_profile_must_change_password(self):
        # e.g. accounts made by create_users or createsuperuser
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
