import io

import openpyxl
from django.urls import reverse

from ..models import FileActivity
from .base import StorageTestCase


class PreviewTests(StorageTestCase):
    def setUp(self):
        super().setUp()
        self.login(self.make_user('u', [('/A', 'read')]))

    def preview(self, name, content):
        self.make_file(f'A/{name}', content)
        response = self.client.get(reverse('file_preview', args=[f'A/{name}']))
        if response.streaming:
            self.read_response(response)
        return response

    def test_user_content_is_sandboxed(self):
        # A sandbox CSP stops uploaded content running scripts on this site
        for name, content in [
            ('evil.html', b'<script>alert(1)</script>'),
            ('evil.htm', b'<script>alert(1)</script>'),
            ('evil.svg', b'<svg xmlns="http://www.w3.org/2000/svg"><script/></svg>'),
            ('evil.xml', b'<x/>'),
            ('pic.png', b'data'),
            ('notes.txt', b'data'),
            ('data.csv', b'a,b'),
            ('unknown.zzz', b'data'),
        ]:
            with self.subTest(name=name):
                response = self.preview(name, content)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response['Content-Security-Policy'], 'sandbox')

    def test_pdf_video_and_audio_are_not_sandboxed(self):
        # They cannot run scripts, and the browsers' viewers break in a sandbox
        for name in ['doc.pdf', 'clip.mp4', 'song.mp3']:
            with self.subTest(name=name):
                response = self.preview(name, b'data')
                self.assertEqual(response.status_code, 200)
                self.assertNotIn('Content-Security-Policy', response)

    def test_spreadsheet_preview_is_escaped_sandboxed_and_logged(self):
        wb = openpyxl.Workbook()
        wb.active.title = '<b>S<b>'
        wb.active.append(['<script>alert(1)</script>'])
        content = io.BytesIO()
        wb.save(content)

        response = self.preview('x.xlsx', content.getvalue())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Security-Policy'], 'sandbox')
        self.assertNotContains(response, '<script>alert(1)</script>')
        self.assertContains(response, '&lt;script&gt;alert(1)&lt;/script&gt;')
        self.assertContains(response, '&lt;b&gt;S&lt;b&gt;')
        self.assertTrue(
            FileActivity.objects.filter(
                activity_type='view', filepath='/A/x.xlsx'
            ).exists()
        )

    def test_range_requests(self):
        # Video seeking and PDF viewers ask for parts of the file
        self.make_file('A/clip.mp4', b'0123456789')
        url = reverse('file_preview', args=['A/clip.mp4'])
        for header, content_range, body in [
            ('bytes=2-5', 'bytes 2-5/10', b'2345'),
            ('bytes=7-', 'bytes 7-9/10', b'789'),
            ('bytes=-3', 'bytes 7-9/10', b'789'),  # The last 3 bytes
            ('bytes=5-100', 'bytes 5-9/10', b'56789'),  # Cut at the end of the file
        ]:
            with self.subTest(header=header):
                response = self.client.get(url, HTTP_RANGE=header)
                self.assertEqual(response.status_code, 206)
                self.assertEqual(response['Content-Range'], content_range)
                self.assertEqual(self.read_response(response), body)

        response = self.client.get(url, HTTP_RANGE='bytes=10-')  # Past the end
        self.assertEqual(response.status_code, 416)
        self.assertEqual(response['Content-Range'], 'bytes */10')

    def test_broken_office_files_cannot_be_previewed(self):
        for name in ['bad.docx', 'bad.xlsx', 'bad.xls']:
            with self.subTest(name=name):
                response = self.preview(name, b'not really an office file')
                self.assertEqual(response.status_code, 422)
