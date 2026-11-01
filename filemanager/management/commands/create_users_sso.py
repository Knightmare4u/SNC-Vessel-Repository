import csv

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction


class Command(BaseCommand):
    help = (
        'Create accounts that sign in with Microsoft only, from a CSV file with '
        'the header username,email,first_name,last_name. The email must be the '
        "person's Microsoft sign-in address: their Microsoft account is linked "
        'to the account by it at their first sign-in. Existing usernames are '
        'skipped.'
    )

    def add_arguments(self, parser):
        parser.add_argument('file', help='CSV file, one person per row')

    def handle(self, *args, **options):
        User = get_user_model()
        # utf-8-sig: Excel's "CSV UTF-8" starts the file with a BOM
        with open(options['file'], encoding='utf-8-sig', newline='') as f:
            rows = list(csv.DictReader(f))

        created = 0
        with transaction.atomic():
            for row in rows:
                username = row['username'].strip()
                if User.objects.filter(username__iexact=username).exists():
                    self.stdout.write(f'Skipped {username}: already exists')
                    continue
                # No password given, so it is unusable: Microsoft sign-in only
                User.objects.create_user(
                    username=username,
                    email=row['email'].strip(),
                    first_name=row['first_name'].strip(),
                    last_name=row['last_name'].strip(),
                )
                created += 1

        self.stdout.write(self.style.SUCCESS(f'Created {created} account(s)'))
