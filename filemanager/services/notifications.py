from django.conf import settings
from django.urls import reverse

from ..emails import send_email
from ..utils import display_name


def _absolute_url(path):
    # Emails are sent without a request to build the URL from
    return settings.BASE_URL.rstrip('/') + path


def _signing_turn_context(signer):
    signing_request = signer.signing_request
    return {
        'recipient_name': display_name(signer.user),
        'sender_name': display_name(signing_request.created_by),
        'filename': signing_request.filename,
        'signing_order': signer.signing_order,
        'signer_count': signing_request.signers.count(),
        'url': _absolute_url(reverse('signing_sign', args=[signing_request.pk])),
    }


def notify_signing_turn(signer):
    """Tell a signer it is their turn."""
    if not signer.user.email:
        return
    send_email(
        subject=(
            '[SNSeaFiles] A document is waiting for your signature: '
            f'{signer.signing_request.filename}'
        ),
        recipients=[signer.user.email],
        template='filemanager/emails/signing_turn',
        context=_signing_turn_context(signer),
    )


def notify_signing_reminder(signer, waiting_days):
    """Remind a signer whose turn has been waiting. Same email as their turn
    notice, saying how long it has waited. Sent right away (scheduled job)."""
    if not signer.user.email:
        return
    send_email(
        subject=(
            '[SNSeaFiles] A document is still waiting for your signature: '
            f'{signer.signing_request.filename}'
        ),
        recipients=[signer.user.email],
        template='filemanager/emails/signing_turn',
        context={**_signing_turn_context(signer), 'waiting_days': waiting_days},
        background=False,
    )


def notify_signing_completed(signing_request, detail, actor):
    """Tell the sender, signers and notify_users that everyone has signed. The
    last signer (actor) completed it and has just seen the result."""
    signers = list(signing_request.signers.select_related('user'))
    users = [
        signing_request.created_by,
        *(signer.user for signer in signers),
        *signing_request.notify_users.all(),
    ]
    recipients = list(dict.fromkeys(u.email for u in users if u.email and u != actor))
    if not recipients:
        return

    send_email(
        subject=(
            '[SNSeaFiles] A document has been fully signed: '
            f'{signing_request.filename}'
        ),
        recipients=recipients,
        template='filemanager/emails/signing_completed',
        context={
            'filename': signing_request.filename,
            'written_back': detail['written_back'],
            'signers': [
                {
                    'order': signer.signing_order,
                    'name': display_name(signer.user),
                    'signed_at': signer.signed_at,
                }
                for signer in signers
            ],
            'url': _absolute_url(reverse('signing_detail', args=[signing_request.pk])),
        },
    )
