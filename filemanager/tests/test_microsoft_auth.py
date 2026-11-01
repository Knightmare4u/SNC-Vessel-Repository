from unittest import mock

from django.contrib.auth.models import User
from django.test import override_settings
from django.urls import reverse

from ..models import UserProfile
from .base import StorageTestCase, no_static_manifest

CLIENT_ID = '11111111-1111-1111-1111-111111111111'
TENANT_ID = '22222222-2222-2222-2222-222222222222'
HOST = 'snseafiles.sinceregp.com'
AUTH_URI = 'https://login.microsoftonline.com/authorize?state=s'
SERVICE_LOGGER = 'filemanager.services.microsoft_auth'


def claims(**overrides):
    return {
        'aud': CLIENT_ID,
        'tid': TENANT_ID,
        'oid': 'oid-1',
        'preferred_username': 'Jane.Doe@sinceregp.com',
        'name': 'Jane Doe',
        **overrides,
    }


@no_static_manifest
@override_settings(
    MICROSOFT_CLIENT_ID=CLIENT_ID,
    MICROSOFT_CLIENT_SECRET='secret',
    MICROSOFT_TENANT_ID=TENANT_ID,
    MICROSOFT_REDIRECT_URI=f'https://{HOST}/auth/microsoft/callback/',
    MICROSOFT_ALLOWED_DOMAINS=['sinceregp.com'],
)
class MicrosoftLoginTests(StorageTestCase):
    def setUp(self):
        super().setUp()
        self.client.defaults['HTTP_HOST'] = HOST
        # Stands in for MSAL, which would call Microsoft
        self.app = mock.Mock()
        self.app.initiate_auth_code_flow.return_value = {
            'auth_uri': AUTH_URI,
            'state': 's',
        }
        patcher = mock.patch(
            'filemanager.services.microsoft_auth._app', return_value=self.app
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def start(self, **params):
        response = self.client.get(reverse('microsoft_login'), params)
        self.assertRedirects(response, AUTH_URI, fetch_redirect_response=False)

    def come_back(self, result):
        """Microsoft sends the user back; MSAL gives this result."""
        self.app.acquire_token_by_auth_code_flow.return_value = result
        return self.client.get(
            reverse('microsoft_callback'), {'code': 'c', 'state': 's'}
        )

    def sign_in(self, next_url=None, **claim_overrides):
        self.start(**({'next': next_url} if next_url else {}))
        return self.come_back({'id_token_claims': claims(**claim_overrides)})

    def signed_in_user(self):
        return User.objects.filter(pk=self.client.session.get('_auth_user_id')).first()

    def assertRefused(self, response, message):
        self.assertRedirects(response, reverse('login'), fetch_redirect_response=False)
        self.assertIsNone(self.signed_in_user())
        self.assertContains(self.client.get(reverse('login')), message)

    def test_button_shown_once_configured(self):
        response = self.client.get(reverse('login'), {'next': '/browser/'})
        self.assertContains(response, 'Sign in with Microsoft')
        self.assertContains(response, reverse('microsoft_login') + '?next=/browser/')

        with override_settings(MICROSOFT_CLIENT_SECRET=''):
            response = self.client.get(reverse('login'))
            self.assertNotContains(response, 'Sign in with Microsoft')
            response = self.client.get(reverse('microsoft_login'))
            self.assertEqual(response.status_code, 404)

    def test_new_user_is_created_without_folders(self):
        response = self.sign_in()
        self.assertRedirects(
            response, reverse('dashboard'), fetch_redirect_response=False
        )
        user = self.signed_in_user()
        self.assertEqual(user.username, 'jane.doe@sinceregp.com')
        self.assertEqual(user.email, 'jane.doe@sinceregp.com')
        self.assertEqual(user.get_full_name(), 'Jane Doe')
        self.assertFalse(user.has_usable_password())
        self.assertEqual(user.userprofile.microsoft_oid, 'oid-1')
        # No password to change, so not sent to the change form
        self.assertContains(self.client.get(reverse('dashboard')), 'No folders assigned')

    def test_session_flow_is_finished_once(self):
        self.sign_in()
        flow, auth_response = self.app.acquire_token_by_auth_code_flow.call_args.args
        self.assertEqual(flow, {'auth_uri': AUTH_URI, 'state': 's'})
        self.assertEqual(auth_response, {'code': 'c', 'state': 's'})

        self.client.logout()
        response = self.come_back({'id_token_claims': claims()})
        self.assertRefused(response, 'Your Microsoft sign-in expired')
        self.assertEqual(self.app.acquire_token_by_auth_code_flow.call_count, 1)

    def test_existing_user_is_linked_by_email(self):
        user = self.make_user('jdoe', [('/', 'read')], password_changed=False)
        user.email = 'Jane.Doe@SincereGP.com'
        user.save()
        self.sign_in()
        self.assertEqual(self.signed_in_user(), user)
        self.assertEqual(UserProfile.objects.get(user=user).microsoft_oid, 'oid-1')
        # The initial password is only to be changed after signing in with it
        self.assertEqual(self.client.get(reverse('dashboard')).status_code, 200)

        # Then found by the account ID, even if the address changes
        self.client.logout()
        self.sign_in(preferred_username='jane.chen@sinceregp.com')
        self.assertEqual(self.signed_in_user(), user)
        self.assertEqual(User.objects.count(), 1)

    def test_existing_user_is_linked_by_username(self):
        user = self.make_user('jane.doe@sinceregp.com')
        self.sign_in()
        self.assertEqual(self.signed_in_user(), user)

    def test_user_linked_to_another_account_is_refused(self):
        user = self.make_user('jdoe')
        user.email = 'jane.doe@sinceregp.com'
        user.save()
        UserProfile.objects.filter(user=user).update(microsoft_oid='oid-old')
        with self.assertLogs(SERVICE_LOGGER, 'WARNING'):
            response = self.sign_in()
        self.assertRefused(response, 'linked to a different Microsoft account')

    def test_several_matching_users_are_refused(self):
        for username in ['jdoe', 'jane']:
            User.objects.create_user(username, email='jane.doe@sinceregp.com')
        with self.assertLogs(SERVICE_LOGGER, 'WARNING'):
            response = self.sign_in()
        self.assertRefused(response, 'matches more than one user')

    def test_other_domains_are_refused(self):
        for address in [
            'jane@gmail.com',
            'jane@sinceregp.com.example.com',
            'jane@notsinceregp.com',
            '',
        ]:
            with self.subTest(address=address):
                with self.assertLogs(SERVICE_LOGGER, 'WARNING'):
                    response = self.sign_in(preferred_username=address)
                self.assertRefused(
                    response, 'Only @sinceregp.com Microsoft accounts can sign in'
                )
        self.assertFalse(User.objects.exists())

    def test_token_for_another_app_or_organization_is_refused(self):
        for overrides in [{'aud': 'another-app'}, {'tid': 'another-tenant'}]:
            with self.subTest(**overrides):
                with self.assertLogs(SERVICE_LOGGER, 'WARNING'):
                    response = self.sign_in(**overrides)
                self.assertRefused(response, 'Microsoft sign-in failed')
        self.assertFalse(User.objects.exists())

    def test_inactive_user_is_refused(self):
        user = self.make_user('jane.doe@sinceregp.com')
        user.is_active = False
        user.save()
        with self.assertLogs(SERVICE_LOGGER, 'WARNING'):
            response = self.sign_in()
        self.assertRefused(response, 'Your account has been disabled')

    def test_cancelled_sign_in(self):
        self.start()
        with self.assertLogs(SERVICE_LOGGER, 'WARNING'):
            response = self.come_back(
                {'error': 'access_denied', 'error_description': 'User cancelled'}
            )
        self.assertRefused(response, 'cancelled or did not complete')

    def test_state_mismatch(self):
        self.start()
        self.app.acquire_token_by_auth_code_flow.side_effect = ValueError
        self.assertRefused(self.come_back(None), 'Your Microsoft sign-in expired')

    def test_microsoft_unreachable(self):
        self.app.initiate_auth_code_flow.side_effect = ConnectionError
        with self.assertLogs('filemanager.views', 'ERROR'):
            response = self.client.get(reverse('microsoft_login'))
        self.assertRefused(response, 'Microsoft sign-in is unavailable')

    def test_returns_to_next_page(self):
        response = self.sign_in(next_url='/browser/Ships/')
        self.assertRedirects(
            response, '/browser/Ships/', fetch_redirect_response=False
        )

    def test_next_to_another_site_is_ignored(self):
        response = self.sign_in(next_url='https://example.com/')
        self.assertRedirects(
            response, reverse('dashboard'), fetch_redirect_response=False
        )

    def test_started_at_ip_address_moves_to_redirect_uri_site(self):
        response = self.client.get(
            reverse('microsoft_login'),
            {'next': '/browser/'},
            HTTP_HOST='121.200.241.178',
        )
        self.assertRedirects(
            response,
            f'https://{HOST}/auth/microsoft/login/?next=%2Fbrowser%2F',
            fetch_redirect_response=False,
        )
        self.app.initiate_auth_code_flow.assert_not_called()
