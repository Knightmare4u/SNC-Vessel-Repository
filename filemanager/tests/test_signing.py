import base64
import io
import json
import os
from datetime import datetime, timedelta, timezone
from unittest import mock

from django.contrib.auth.models import Permission
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import override_settings
from django.urls import reverse
from PIL import Image, ImageDraw
from pypdf import PdfReader, PdfWriter

from .. import emails
from ..models import FileActivity, Signer, SigningRequest, UserSignature
from ..services import filesystem
from ..services import signing as signing_service
from .base import StorageTestCase, no_static_manifest


def make_pdf(pages=1, password=None):
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    if password:
        writer.encrypt(password)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def make_signature(blank=False):
    img = Image.new('RGBA', (300, 100), (0, 0, 0, 0))
    if not blank:
        ImageDraw.Draw(img).line((20, 50, 280, 60), fill='black', width=3)
    output = io.BytesIO()
    img.save(output, format='PNG')
    return 'data:image/png;base64,' + base64.b64encode(output.getvalue()).decode()


@no_static_manifest
class SigningTests(StorageTestCase):
    @classmethod
    def setUpTestData(cls):
        # Starting a request needs the permission and write access to the folder
        cls.sender = cls.make_user('sender', [('/A', 'write')])
        cls.sender.user_permissions.add(
            Permission.objects.get(codename='request_signatures')
        )
        cls.signer1 = cls.make_user('signer1')
        cls.signer2 = cls.make_user('signer2')
        cls.watcher = cls.make_user('watcher')
        cls.outsider = cls.make_user('outsider')
        for user in (cls.sender, cls.signer1, cls.signer2, cls.watcher, cls.outsider):
            user.email = f'{user.username}@x.com'
            user.save()

    def setUp(self):
        super().setUp()
        override = override_settings(MEDIA_ROOT=os.path.join(self.tmp, 'media'))
        override.enable()
        self.addCleanup(override.disable)
        # Send in the test's thread so mail.outbox is filled before asserting
        patcher = mock.patch.object(
            emails, '_deliver_in_background', emails._deliver
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.original = make_pdf()
        self.make_file('A/doc.pdf', self.original)

    def overwrite(self, rel, content):
        """Change a file on disk outside the site, e.g. on the shared folder."""
        with open(filesystem.resolve_file(rel).file.path, 'wb') as f:
            f.write(content)

    def create_request(
        self,
        signers=None,
        notify=(),
        placement_mode='auto',
        source_path='A/doc.pdf',
        actor=None,
    ):
        """None in signers leaves that row blank."""
        signers = signers or [self.signer1, self.signer2]
        data = {
            'source_path': source_path,
            'placement_mode': placement_mode,
            'notify_users': [u.pk for u in notify],
            'signers-TOTAL_FORMS': len(signers),
            'signers-INITIAL_FORMS': 0,
            'signers-MIN_NUM_FORMS': 1,
            'signers-MAX_NUM_FORMS': 20,
        }
        for i, user in enumerate(signers):
            data[f'signers-{i}-user'] = user.pk if user else ''
        self.login(actor or self.sender)
        # Emails are sent on commit, which TestCase never reaches by itself
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(reverse('signing_create'), data)

    def post_signature(self, user, signing_request, data):
        self.login(user)
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(
                reverse('signing_sign', args=[signing_request.pk]), data
            )

    def sign_as(self, user, signing_request):
        return self.post_signature(
            user, signing_request, {'source': 'drawn', 'signature': make_signature()}
        )

    def signer_status(self, signing_request, user):
        return signing_request.signers.get(user=user).status

    # ---- Starting a request ------------------------------------------------

    def test_create_sends_to_first_signer(self):
        response = self.create_request(notify=[self.watcher])
        signing_request = SigningRequest.objects.get()
        self.assertRedirects(
            response, reverse('signing_detail', args=[signing_request.pk])
        )
        self.assertEqual(signing_request.file, filesystem.resolve_file('A/doc.pdf'))
        self.assertEqual(signing_request.status, SigningRequest.Status.IN_PROGRESS)
        self.assertEqual(
            [(s.user, s.status) for s in signing_request.signers.all()],
            [
                (self.signer1, Signer.Status.PENDING),
                (self.signer2, Signer.Status.WAITING),
            ],
        )
        self.assertEqual(list(signing_request.notify_users.all()), [self.watcher])
        self.assertEqual([m.to for m in mail.outbox], [['signer1@x.com']])
        sign_url = reverse('signing_sign', args=[signing_request.pk])
        self.assertIn(sign_url, mail.outbox[0].body)
        html, mimetype = mail.outbox[0].alternatives[0]
        self.assertEqual(mimetype, 'text/html')
        self.assertIn(sign_url, html)

    def test_sender_signing_first_gets_no_email(self):
        self.create_request(signers=[self.sender, self.signer1])
        self.assertEqual(mail.outbox, [])

    def test_create_requires_permission_and_folder_write(self):
        permission = Permission.objects.get(codename='request_signatures')
        no_permission = self.make_user('no_permission', [('/A', 'admin')])
        read_only = self.make_user('read_only', [('/A', 'read')])
        read_only.user_permissions.add(permission)
        no_folder = self.make_user('no_folder')
        no_folder.user_permissions.add(permission)
        for user in (no_permission, read_only, no_folder):
            with self.subTest(user=user.username):
                self.login(user)
                response = self.client.get(
                    reverse('signing_create'), {'path': 'A/doc.pdf'}
                )
                self.assertEqual(response.status_code, 403)

    def test_invalid_signer_lists_are_rejected(self):
        for signers in ([self.signer1, None], [self.signer1, self.signer1]):
            with self.subTest(signers=signers):
                response = self.create_request(signers=signers)
                self.assertEqual(response.status_code, 200)
        self.assertFalse(SigningRequest.objects.exists())

    def test_file_can_only_be_sent_once(self):
        self.create_request()
        response = self.create_request()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(SigningRequest.objects.count(), 1)

    def test_only_readable_pdfs_outside_the_archive_can_be_sent(self):
        self.make_file('A/notes.txt')
        self.make_file('A/broken.pdf', b'not really a pdf')
        self.make_file('A/locked.pdf', make_pdf(password='pw'))
        self.make_file('ARCHIVE/old.pdf', make_pdf())
        # A superuser, since other users have no access to the archive at all
        superuser = self.make_user('su', superuser=True)
        for path, error in [
            ('A/notes.txt', 'Only existing PDF files can be sent for signing.'),
            ('A/broken.pdf', 'This PDF file cannot be read.'),
            ('A/locked.pdf', 'Password-protected PDFs cannot be signed.'),
            ('ARCHIVE/old.pdf', 'Archived files cannot be sent for signing.'),
        ]:
            with self.subTest(path=path):
                response = self.create_request(source_path=path, actor=superuser)
                self.assertContains(response, error)
        self.assertFalse(SigningRequest.objects.exists())

    # ---- Who can see and sign ----------------------------------------------

    def test_only_the_pending_signer_can_sign(self):
        self.create_request()
        signing_request = SigningRequest.objects.get()
        detail_url = reverse('signing_detail', args=[signing_request.pk])
        # Participants are sent to the progress page and told why
        for user in (self.signer2, self.sender):
            with self.subTest(user=user.username):
                response = self.sign_as(user, signing_request)
                self.assertRedirects(response, detail_url)
        response = self.sign_as(self.outsider, signing_request)
        self.assertEqual(response.status_code, 404)
        self.assertFalse(
            signing_request.signers.filter(status=Signer.Status.SIGNED).exists()
        )

        # Nobody signs twice: not before the end, nor once it is complete
        self.sign_as(self.signer1, signing_request)
        self.assertRedirects(self.sign_as(self.signer1, signing_request), detail_url)
        self.sign_as(self.signer2, signing_request)
        self.assertRedirects(self.sign_as(self.signer2, signing_request), detail_url)
        self.assertEqual(
            signing_request.activities.filter(action='signed').count(), 2
        )

    def test_outsider_cannot_view(self):
        self.create_request(notify=[self.watcher])
        signing_request = SigningRequest.objects.get()
        for user, status in [(self.watcher, 200), (self.outsider, 404)]:
            with self.subTest(user=user.username):
                self.login(user)
                for name in ('signing_detail', 'signing_pdf'):
                    response = self.client.get(reverse(name, args=[signing_request.pk]))
                    self.assertEqual(response.status_code, status)

    def test_signing_list_tabs(self):
        self.create_request(notify=[self.watcher])
        signing_request = SigningRequest.objects.get()

        def listed(user, tab):
            self.login(user)
            response = self.client.get(reverse('signing_list'), {'tab': tab})
            # A tab the user doesn't have falls back to 'to_sign'
            return (
                response.context['tab'] == tab
                and signing_request in response.context['page'].object_list
            )

        for user, tab, expected in [
            (self.signer1, 'to_sign', True),
            (self.signer2, 'to_sign', False),  # Not their turn yet
            (self.sender, 'sent', True),
            (self.signer1, 'sent', False),
            (self.watcher, 'notified', True),
            (self.outsider, 'to_sign', False),
            (self.outsider, 'sent', False),
            (self.outsider, 'notified', False),
        ]:
            with self.subTest(user=user.username, tab=tab):
                self.assertEqual(listed(user, tab), expected)

        # The tab filters on signer1's row; the counts must still cover both
        self.login(self.signer1)
        response = self.client.get(reverse('signing_list'), {'tab': 'to_sign'})
        [listed_request] = response.context['page'].object_list
        self.assertEqual(
            (listed_request.signer_count, listed_request.signed_count), (2, 0)
        )

    # ---- Signatures --------------------------------------------------------

    def test_invalid_signatures_are_rejected(self):
        self.create_request()
        signing_request = SigningRequest.objects.get()
        for data, error in [
            (
                {'source': 'drawn', 'signature': make_signature(blank=True)},
                'Signature is empty.',
            ),
            (
                {
                    'source': 'uploaded',
                    'signature_file': SimpleUploadedFile('sig.png', b'not an image'),
                },
                'Invalid signature image.',
            ),
            ({'source': 'saved'}, 'You have no saved signature yet.'),
        ]:
            with self.subTest(source=data['source']):
                response = self.post_signature(self.signer1, signing_request, data)
                self.assertContains(response, error)
        self.assertEqual(
            self.signer_status(signing_request, self.signer1), Signer.Status.PENDING
        )

    def test_upload_removes_white_background(self):
        img = Image.new('RGB', (300, 100), 'white')
        ImageDraw.Draw(img).line((20, 50, 280, 60), fill='black', width=4)
        upload = io.BytesIO()
        img.save(upload, format='JPEG')
        self.create_request()
        signing_request = SigningRequest.objects.get()

        self.post_signature(
            self.signer1,
            signing_request,
            {
                'source': 'uploaded',
                'signature_file': SimpleUploadedFile(
                    'sig.jpg', upload.getvalue(), 'image/jpeg'
                ),
            },
        )

        signer = signing_request.signers.get(user=self.signer1)
        self.assertEqual(signer.status, Signer.Status.SIGNED)
        with signer.signature_image.open('rb') as f, Image.open(f) as saved:
            alpha = saved.getchannel('A')
            # The line is diagonal, so the cropped corners are paper
            self.assertEqual(alpha.getpixel((0, saved.height - 1)), 0)
            self.assertEqual(alpha.getextrema()[1], 255)

    def test_save_as_default(self):
        self.create_request(signers=[self.signer1])
        signing_request = SigningRequest.objects.get()
        self.post_signature(
            self.signer1,
            signing_request,
            {
                'source': 'drawn',
                'signature': make_signature(),
                'save_as_default': 'on',
            },
        )
        self.assertTrue(UserSignature.objects.filter(user=self.signer1).exists())

    def test_sign_with_saved_signature(self):
        signing_service.save_user_signature(
            self.signer1, signing_service.signature_from_data_url(make_signature())
        )
        self.create_request()
        signing_request = SigningRequest.objects.get()
        self.post_signature(self.signer1, signing_request, {'source': 'saved'})
        signer = signing_request.signers.get(user=self.signer1)
        self.assertEqual(signer.status, Signer.Status.SIGNED)
        # A copy, so replacing the default later leaves this signature alone
        self.assertNotEqual(
            signer.signature_image.name, self.signer1.saved_signature.image.name
        )

    # ---- Completing and writing back ---------------------------------------

    def test_signing_in_order_completes_and_replaces_original(self):
        self.create_request(notify=[self.watcher])
        signing_request = SigningRequest.objects.get()

        self.sign_as(self.signer1, signing_request)
        self.assertEqual(
            self.signer_status(signing_request, self.signer2), Signer.Status.PENDING
        )
        self.sign_as(self.signer2, signing_request)

        signing_request.refresh_from_db()
        self.assertEqual(signing_request.status, SigningRequest.Status.COMPLETED)
        signed = self.read('A/doc.pdf')
        with signing_request.signed_file.open('rb') as f:
            self.assertEqual(signed, f.read())
        # The row keeps the size of what is now on disk
        self.assertEqual(filesystem.resolve_file('A/doc.pdf').size, len(signed))
        # Original page plus the signature table page
        self.assertEqual(len(PdfReader(io.BytesIO(signed)).pages), 2)
        self.assertTrue(FileActivity.objects.filter(activity_type='sign').exists())
        self.assertEqual(
            signing_request.activities.get(action='completed').detail,
            {'written_back': True},
        )
        # signer2 completed it, so is not emailed
        self.assertEqual(
            sorted(mail.outbox[-1].to),
            ['sender@x.com', 'signer1@x.com', 'watcher@x.com'],
        )

    def test_each_signature_is_stamped_before_completion(self):
        self.create_request()
        signing_request = SigningRequest.objects.get()

        self.sign_as(self.signer1, signing_request)

        signing_request.refresh_from_db()
        self.assertEqual(signing_request.status, SigningRequest.Status.IN_PROGRESS)
        # The next signer sees a copy with signer1's signature table page
        with signing_request.signed_file.open('rb') as f:
            self.assertEqual(len(PdfReader(f).pages), 2)
        # The original is only replaced once everyone has signed
        self.assertEqual(self.read('A/doc.pdf'), self.original)
        self.assertEqual(signing_request.signed_sha, '')

    def test_changed_original_is_not_replaced(self):
        self.create_request(signers=[self.signer1])
        signing_request = SigningRequest.objects.get()
        changed = make_pdf(pages=3)
        self.overwrite('A/doc.pdf', changed)

        self.sign_as(self.signer1, signing_request)

        signing_request.refresh_from_db()
        self.assertEqual(signing_request.status, SigningRequest.Status.COMPLETED)
        self.assertEqual(self.read('A/doc.pdf'), changed)
        self.assertEqual(
            signing_request.activities.get(action='completed').detail,
            {'written_back': False, 'reason': 'source_modified'},
        )

    def test_missing_original_is_not_written_back(self):
        # e.g. removed from the shared folder outside the site
        self.create_request(signers=[self.signer1])
        signing_request = SigningRequest.objects.get()
        path = filesystem.resolve_file('A/doc.pdf').file.path
        os.remove(path)

        self.sign_as(self.signer1, signing_request)

        signing_request.refresh_from_db()
        self.assertEqual(signing_request.status, SigningRequest.Status.COMPLETED)
        self.assertFalse(os.path.exists(path))
        self.assertEqual(
            signing_request.activities.get(action='completed').detail,
            {'written_back': False, 'reason': 'source_missing'},
        )

    # ---- The file while it is in signing -----------------------------------

    def test_sent_file_and_its_folders_cannot_be_deleted(self):
        self.create_request()
        self.make_dir('A/sub')
        self.login(self.make_user('folder_admin', [('/', 'admin')]))
        for path in ('A/doc.pdf', 'A'):
            with self.subTest(path=path):
                response = self.post_json(reverse('delete_file'), {'path': path})
                self.assertEqual(response.status_code, 403)
                self.assertIn('signing', response.json()['error'])
        self.assertIsNotNone(filesystem.resolve_file('A/doc.pdf'))
        # Other folders are not locked
        response = self.post_json(reverse('delete_file'), {'path': 'A/sub'})
        self.assertEqual(response.status_code, 200)

    def test_request_follows_a_renamed_file(self):
        self.create_request()
        signing_request = SigningRequest.objects.get()
        self.login(self.make_user('folder_admin', [('/', 'admin')]))
        for old_path, new_name in (('A/doc.pdf', 'new.pdf'), ('A', 'B')):
            with self.subTest(old_path=old_path):
                response = self.post_json(
                    reverse('rename_file'), {'old_path': old_path, 'new_name': new_name}
                )
                self.assertEqual(response.status_code, 200)
        self.assertEqual(signing_request.file, filesystem.resolve_file('B/new.pdf'))

        self.sign_as(self.signer1, signing_request)
        self.sign_as(self.signer2, signing_request)

        signing_request.refresh_from_db()
        self.assertEqual(
            signing_request.activities.get(action='completed').detail,
            {'written_back': True},
        )
        self.assertNotEqual(self.read('B/new.pdf'), self.original)

    # ---- Admin reset -------------------------------------------------------

    def reset(self, signing_request):
        self.login(self.make_user('admin', superuser=True))
        data = {
            'action': 'reset_signing',
            '_selected_action': [signing_request.pk],
            'post': 'yes',  # Skip the confirmation page
        }
        return self.client.post(
            reverse('admin:filemanager_signingrequest_changelist'), data
        )

    def test_reset_in_progress_request_unlocks_file(self):
        self.create_request()
        self.reset(SigningRequest.objects.get())
        self.assertFalse(SigningRequest.objects.exists())
        self.assertFalse(Signer.objects.exists())
        file = filesystem.resolve_file('A/doc.pdf')
        self.assertFalse(signing_service.is_locked(file))
        self.assertEqual(self.read('A/doc.pdf'), self.original)

    def test_reset_completed_request_restores_original(self):
        self.create_request(signers=[self.signer1])
        signing_request = SigningRequest.objects.get()
        self.sign_as(self.signer1, signing_request)
        self.assertNotEqual(self.read('A/doc.pdf'), self.original)

        self.reset(signing_request)

        self.assertFalse(SigningRequest.objects.exists())
        self.assertEqual(self.read('A/doc.pdf'), self.original)
        self.assertEqual(
            filesystem.resolve_file('A/doc.pdf').size, len(self.original)
        )

    def test_reset_leaves_file_changed_after_signing(self):
        self.create_request(signers=[self.signer1])
        signing_request = SigningRequest.objects.get()
        self.sign_as(self.signer1, signing_request)
        changed = make_pdf(pages=3)
        self.overwrite('A/doc.pdf', changed)

        self.reset(signing_request)

        self.assertFalse(SigningRequest.objects.exists())
        self.assertEqual(self.read('A/doc.pdf'), changed)

    # ---- Manual placement --------------------------------------------------

    def place_and_send(self, signing_request, boxes):
        self.login(self.sender)
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(
                reverse('signing_fields', args=[signing_request.pk]),
                {'boxes': json.dumps(boxes)},
            )

    def signature_box(self, signer, **overrides):
        box = {
            'signer': signer.pk, 'field_type': 'signature', 'page': 1,
            'x': 0.1, 'y': 0.8, 'width': 0.3, 'height': 0.1,
        }  # fmt: skip
        box.update(overrides)
        return box

    def test_manual_placement_saves_a_draft_without_sending(self):
        response = self.create_request(placement_mode='manual')
        signing_request = SigningRequest.objects.get()
        self.assertRedirects(
            response, reverse('signing_fields', args=[signing_request.pk])
        )
        self.assertEqual(signing_request.status, SigningRequest.Status.DRAFT)
        self.assertEqual(mail.outbox, [])
        self.assertFalse(signing_service.is_locked(signing_request.file))

    def test_draft_is_only_visible_to_its_creator(self):
        self.create_request(placement_mode='manual')
        signing_request = SigningRequest.objects.get()
        self.login(self.signer1)
        for name in ('signing_fields', 'signing_detail'):
            with self.subTest(view=name):
                response = self.client.get(reverse(name, args=[signing_request.pk]))
                self.assertEqual(response.status_code, 404)

    def test_send_requires_a_signature_box_for_every_signer(self):
        self.create_request(placement_mode='manual')
        signing_request = SigningRequest.objects.get()
        signer1 = signing_request.signers.get(user=self.signer1)

        response = self.place_and_send(signing_request, [self.signature_box(signer1)])

        self.assertContains(response, 'Place a signature box for every signer.')
        signing_request.refresh_from_db()
        self.assertEqual(signing_request.status, SigningRequest.Status.DRAFT)
        self.assertEqual(mail.outbox, [])

    def test_box_outside_the_page_is_rejected(self):
        self.create_request(placement_mode='manual', signers=[self.signer1])
        signing_request = SigningRequest.objects.get()
        signer1 = signing_request.signers.get()
        for box in [
            self.signature_box(signer1, x=0.9),  # Runs off the right edge
            self.signature_box(signer1, page=2),  # The PDF has one page
        ]:
            with self.subTest(box=box):
                response = self.place_and_send(signing_request, [box])
                self.assertContains(response, 'outside the page')

    def test_send_is_blocked_if_the_file_changed_after_the_draft(self):
        self.create_request(placement_mode='manual', signers=[self.signer1])
        signing_request = SigningRequest.objects.get()
        self.overwrite('A/doc.pdf', make_pdf(pages=2))

        response = self.place_and_send(
            signing_request, [self.signature_box(signing_request.signers.get())]
        )

        self.assertContains(response, 'The file has changed since this draft was saved.')
        signing_request.refresh_from_db()
        self.assertEqual(signing_request.status, SigningRequest.Status.DRAFT)

    def test_manual_signing_stamps_the_pages_without_a_table(self):
        self.create_request(placement_mode='manual')
        signing_request = SigningRequest.objects.get()
        signer1 = signing_request.signers.get(user=self.signer1)
        signer2 = signing_request.signers.get(user=self.signer2)
        date_box = self.signature_box(
            signer2, field_type='date', x=0.5, y=0.92, width=0.2, height=0.05
        )
        self.place_and_send(
            signing_request,
            [
                self.signature_box(signer1),
                self.signature_box(signer2, x=0.5),
                date_box,
            ],
        )
        self.assertEqual(mail.outbox[0].to, ['signer1@x.com'])

        self.sign_as(self.signer1, signing_request)
        self.sign_as(self.signer2, signing_request)

        signing_request.refresh_from_db()
        self.assertEqual(signing_request.status, SigningRequest.Status.COMPLETED)
        signed = self.read('A/doc.pdf')
        self.assertNotEqual(signed, self.original)
        # Signatures go on the existing page; no signature table is appended
        self.assertEqual(len(PdfReader(io.BytesIO(signed)).pages), 1)

    def test_delete_draft_frees_the_file(self):
        self.create_request(placement_mode='manual')
        signing_request = SigningRequest.objects.get()
        self.client.post(reverse('signing_delete', args=[signing_request.pk]))
        self.assertFalse(SigningRequest.objects.exists())
        # The file can be sent for signing again
        self.create_request()
        self.assertTrue(SigningRequest.objects.exists())

    def test_deleting_the_file_or_its_folder_deletes_the_draft(self):
        folder_admin = self.make_user('folder_admin', [('/', 'admin')])
        for path in ('A/doc.pdf', 'A'):
            with self.subTest(path=path):
                if filesystem.resolve_file('A/doc.pdf') is None:
                    self.make_file('A/doc.pdf', self.original)
                self.create_request(placement_mode='manual')

                self.login(folder_admin)
                response = self.post_json(reverse('delete_file'), {'path': path})

                self.assertEqual(response.status_code, 200)
                self.assertFalse(SigningRequest.objects.exists())

    # ---- Reminders ---------------------------------------------------------

    def remind(self):
        call_command('send_signing_reminders', stdout=io.StringIO())

    def age_turn(self, signing_request, **delta):
        # Move the turn's start back in time: the send and any signature so far
        when = datetime.now(timezone.utc) - timedelta(**delta)
        SigningRequest.objects.filter(pk=signing_request.pk).update(sent_at=when)
        signing_request.signers.filter(status=Signer.Status.SIGNED).update(
            signed_at=when
        )

    def test_reminder_goes_to_the_pending_signer_after_a_day(self):
        self.create_request()
        signing_request = SigningRequest.objects.get()
        self.age_turn(signing_request, days=1, minutes=1)
        mail.outbox = []

        self.remind()

        # Only signer1, whose turn it is; signer2 is still waiting for theirs
        self.assertEqual([m.to for m in mail.outbox], [['signer1@x.com']])
        self.assertIn('still waiting', mail.outbox[0].subject)

    def test_later_signers_wait_from_the_previous_signature(self):
        self.create_request()
        signing_request = SigningRequest.objects.get()
        self.sign_as(self.signer1, signing_request)
        now = datetime.now(timezone.utc)
        # Sent days ago, but signer1 only just signed: signer2's turn just began
        SigningRequest.objects.filter(pk=signing_request.pk).update(
            sent_at=now - timedelta(days=5)
        )
        mail.outbox = []

        self.remind()
        self.assertEqual(mail.outbox, [])

        signing_request.signers.filter(user=self.signer1).update(
            signed_at=now - timedelta(days=1, minutes=1)
        )
        self.remind()
        self.assertEqual([m.to for m in mail.outbox], [['signer2@x.com']])

    def test_no_reminder_too_soon_or_to_a_deactivated_signer(self):
        self.create_request()
        signing_request = SigningRequest.objects.get()
        mail.outbox = []

        self.age_turn(signing_request, hours=23)
        self.remind()
        self.assertEqual(mail.outbox, [])

        # A deactivated signer cannot sign; an admin has to replace them
        self.age_turn(signing_request, days=1, minutes=1)
        self.signer1.is_active = False
        self.signer1.save()
        self.remind()
        self.assertEqual(mail.outbox, [])
