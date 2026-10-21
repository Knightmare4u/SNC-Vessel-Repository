"""Sign-in with Microsoft (Entra ID) work accounts, using MSAL's auth code flow."""

import logging

import msal
from django.conf import settings
from django.contrib.auth.models import User
from django.db import transaction
from django.db.models import Q

from ..models import UserProfile

logger = logging.getLogger(__name__)

# Set in the session of a user who signed in with Microsoft
SESSION_KEY = 'signed_in_with_microsoft'

CONTACT_IT = 'Please contact the IT department.'


class MicrosoftLoginError(Exception):
    """Sign-in refused; the message is for the user."""


def is_enabled():
    return all(
        [
            settings.MICROSOFT_CLIENT_ID,
            settings.MICROSOFT_CLIENT_SECRET,
            settings.MICROSOFT_TENANT_ID,
            settings.MICROSOFT_REDIRECT_URI,
        ]
    )


def _app():
    # Made for each sign-in, so the tokens MSAL keeps go with it: only the ID
    # token is used, once
    return msal.ConfidentialClientApplication(
        settings.MICROSOFT_CLIENT_ID,
        client_credential=settings.MICROSOFT_CLIENT_SECRET,
        authority=f'https://login.microsoftonline.com/{settings.MICROSOFT_TENANT_ID}',
        # Nothing is called on the user's behalf later, so no refresh token
        exclude_scopes=['offline_access'],
    )


def start_flow():
    """Return the flow to keep in the session until Microsoft sends the user
    back; flow['auth_uri'] is where to send them."""
    # The code comes back in the query string, which MSAL warns about: form_post
    # would be a cross-site POST, which the session cookie (SameSite=Lax) is not
    # sent with. PKCE and the client secret make a leaked code useless anyway
    return _app().initiate_auth_code_flow(
        scopes=[],  # Sign-in only; MSAL adds openid and profile
        redirect_uri=settings.MICROSOFT_REDIRECT_URI,
        # Pick the account each time, not straight back into the last one used
        prompt='select_account',
    )


def authenticate(flow, auth_response):
    """Finish the sign-in Microsoft sent the user back from, and return the
    user. Raises MicrosoftLoginError if it is refused."""
    try:
        result = _app().acquire_token_by_auth_code_flow(flow, auth_response)
    except ValueError:
        # MSAL's check of the state failed, e.g. a stale or replayed response
        raise MicrosoftLoginError('Your Microsoft sign-in expired. Please try again.')
    if 'error' in result:
        logger.warning(
            'Microsoft sign-in failed: %s %s',
            result['error'],
            result.get('error_description', ''),
        )
        raise MicrosoftLoginError('Microsoft sign-in was cancelled or did not complete.')
    return get_user(result['id_token_claims'])


def get_user(claims):
    # MSAL no longer checks the ID token. It came straight from Microsoft over
    # HTTPS, so its signature need not be checked, but who it is for and which
    # organization it is from must be
    if claims.get('aud', '').lower() != settings.MICROSOFT_CLIENT_ID.lower() or (
        claims.get('tid', '').lower() != settings.MICROSOFT_TENANT_ID.lower()
    ):
        logger.warning(
            'Microsoft sign-in refused: token for app %s, tenant %s',
            claims.get('aud'),
            claims.get('tid'),
        )
        raise MicrosoftLoginError('Microsoft sign-in failed. Please try again.')

    # The account's sign-in name (UPN), set by the organization's admins
    email = claims.get('preferred_username', '').lower()
    allowed_domains = [d.lower() for d in settings.MICROSOFT_ALLOWED_DOMAINS]
    if email.rpartition('@')[2] not in allowed_domains:
        logger.warning('Microsoft sign-in refused for %s: domain not allowed', email)
        raise MicrosoftLoginError(
            f'Only {" or ".join("@" + d for d in allowed_domains)} '
            'Microsoft accounts can sign in here.'
        )

    # oid is the account's ID: unlike the email, it never changes or gets reused
    profile = (
        UserProfile.objects.select_related('user')
        .filter(microsoft_oid=claims['oid'])
        .first()
    )
    user = profile.user if profile else _link_user(claims['oid'], email, claims)
    if not user.is_active:
        logger.warning('Microsoft sign-in refused for %s: user is inactive', email)
        raise MicrosoftLoginError(f'Your account has been disabled. {CONTACT_IT}')
    return user


@transaction.atomic
def _link_user(oid, email, claims):
    """Link the Microsoft account to the user with its email (or that as
    username), or make a new user, with no folders until an admin grants some."""
    users = list(
        User.objects.filter(Q(email__iexact=email) | Q(username__iexact=email))[:2]
    )
    if len(users) > 1:
        logger.warning('Microsoft sign-in refused for %s: several users match', email)
        raise MicrosoftLoginError(
            f'Your Microsoft account matches more than one user here. {CONTACT_IT}'
        )

    if users:
        user = users[0]
        profile, _ = UserProfile.objects.get_or_create(user=user)
        if profile.microsoft_oid:
            # e.g. someone who had this address before; an admin can clear it
            logger.warning(
                'Microsoft sign-in refused for %s: user %s is linked to another '
                'Microsoft account',
                email,
                user.username,
            )
            raise MicrosoftLoginError(
                f'This user is linked to a different Microsoft account. {CONTACT_IT}'
            )
        logger.info('Microsoft account %s linked to user %s', email, user.username)
    else:
        # The token has the full name only; display_name() joins the parts again
        first_name, _, last_name = claims.get('name', '').partition(' ')
        user = User(
            username=email, email=email, first_name=first_name, last_name=last_name
        )
        # Signs in with Microsoft only
        user.set_unusable_password()
        user.save()
        profile = UserProfile.objects.create(user=user)
        logger.info('User %s created on first Microsoft sign-in', email)

    profile.microsoft_oid = oid
    profile.save(update_fields=['microsoft_oid'])
    return user
