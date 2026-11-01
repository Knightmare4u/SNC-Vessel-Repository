from django.core.management.base import BaseCommand
from django.utils import timezone

from filemanager.services import notifications
from filemanager.services import signing as signing_service


class Command(BaseCommand):
    help = (
        'Email every signer whose turn has waited at least a day. '
        'Run once a day (cron / Task Scheduler).'
    )

    def handle(self, *args, **options):
        now = timezone.now()
        sent = 0
        for signer in signing_service.signers_to_remind(now):
            if not signer.user.email:
                continue
            notifications.notify_signing_reminder(
                signer, waiting_days=(now - signer.turn_started).days
            )
            sent += 1
        self.stdout.write(f'Sent {sent} signing reminder(s).')
