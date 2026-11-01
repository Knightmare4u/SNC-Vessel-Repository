import logging
import smtplib
import threading

from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string

logger = logging.getLogger(__name__)


def send_email(*, subject, recipients, template, context, background=True):
    """Render <template>.txt and <template>.html and send them. In the background
    by default, so the user does not wait on the mail server; scheduled jobs send
    one at a time instead, so a batch does not open many connections at once."""
    message = EmailMultiAlternatives(
        subject, render_to_string(f'{template}.txt', context), to=recipients
    )
    message.attach_alternative(
        render_to_string(f'{template}.html', context), 'text/html'
    )
    if background:
        _deliver_in_background(message)
    else:
        _deliver(message)


def _deliver_in_background(message):
    # The message is fully built, so the thread needs no database access
    threading.Thread(target=_deliver, args=(message,)).start()


def _deliver(message):
    # A mail failure must not undo the action that triggered it
    try:
        message.send()
    except (smtplib.SMTPException, OSError):
        logger.exception("Could not send email to %s", message.to)
