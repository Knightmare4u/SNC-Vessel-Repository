import base64
import binascii
import hashlib
import io
import logging
import os
import posixpath
import shutil
import tempfile
import uuid
from collections import defaultdict
from datetime import timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone
from PIL import Image, ImageChops, ImageOps
from pypdf import PdfReader, PdfWriter
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen import canvas
from reportlab.platypus import Image as PdfImage
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from ..models import (
    File,
    Signer,
    SigningActivity,
    SigningField,
    SigningRequest,
    UserSignature,
)
from ..utils import (
    display_name,
    get_storage_path,
    has_permission,
    is_in_archive,
    log_activity,
    normalize_path,
)
from . import filesystem, notifications

logger = logging.getLogger(__name__)

# SigningRequest.Meta.permissions
REQUEST_SIGNATURES = 'filemanager.request_signatures'

# A signer is reminded once their turn has waited this long; the reminder job
# runs daily, so they then get one reminder a day until they sign
REMINDER_AFTER = timedelta(days=1)

# Built-in CID font: renders Chinese names without bundling a font file
TABLE_FONT = 'MSung-Light'
pdfmetrics.registerFont(UnicodeCIDFont(TABLE_FONT))
# Height of a signature in the signature table; every signer row is sized to fit one
TABLE_SIGNATURE_HEIGHT = 18 * mm

SIGNATURE_DATA_URL_PREFIX = 'data:image/png;base64,'
MAX_SIGNATURE_BYTES = 2 * 1024 * 1024
MAX_SIGNATURE_PIXELS = 4000 * 4000
SIGNATURE_SIZE = (800, 300)
# Grey level (0-255) above which uploaded paper counts as background
WHITE_THRESHOLD = 200


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def visible_requests(user):
    """Requests the user created, signs or is notified of. Drafts only show to
    their creator until they are sent."""
    return SigningRequest.objects.filter(
        Q(created_by=user)
        | (
            (Q(signers__user=user) | Q(notify_users=user))
            & ~Q(status=SigningRequest.Status.DRAFT)
        )
    ).distinct()


def is_locked(item):
    """True if item is a file that has been sent for signing, or a folder
    containing one. Sent files cannot be deleted, so the request keeps its file."""
    return _requests_in(item).exclude(status=SigningRequest.Status.DRAFT).exists()


def delete_drafts(item):
    """Delete the drafts for item, a file or a folder, when it is deleted: they
    cannot be sent without the file."""
    _requests_in(item).filter(status=SigningRequest.Status.DRAFT).delete()


def _requests_in(item):
    if isinstance(item, File):
        return SigningRequest.objects.filter(file=item)
    return SigningRequest.objects.filter(
        file__folder_id__in=filesystem.get_subtree_ids(item),
        file__deleted_at__isnull=True,
    )


def validate_source(actor, source_path):
    """Return the File at source_path if it is a PDF the actor may send for
    signing."""
    source_path = normalize_path(source_path)
    # The permission says who may use signing; write access says on which files,
    # since signing locks the file and finally overwrites it (superusers pass both)
    if not (
        actor.has_perm(REQUEST_SIGNATURES)
        and has_permission(actor, posixpath.dirname(source_path), 'write')
    ):
        raise PermissionDenied

    if is_in_archive(get_storage_path(source_path)):
        raise ValidationError('Archived files cannot be sent for signing.')
    file = filesystem.resolve_file(source_path)
    if (
        file is None
        or not file.name.lower().endswith('.pdf')
        or not file.file.storage.exists(file.file.name)
    ):
        raise ValidationError('Only existing PDF files can be sent for signing.')
    if SigningRequest.objects.filter(file=file).exists():
        raise ValidationError('This file already has a signing request.')
    return file


def signature_from_data_url(data_url):
    """A signature drawn on the canvas, sent as a PNG data URL."""
    if not data_url.startswith(SIGNATURE_DATA_URL_PREFIX):
        raise ValidationError('Invalid signature image.')
    try:
        raw = base64.b64decode(
            data_url[len(SIGNATURE_DATA_URL_PREFIX) :], validate=True
        )
    except binascii.Error:
        raise ValidationError('Invalid signature image.')
    # The canvas background is already transparent
    return _clean_signature(raw, remove_white=False)


def signature_from_upload(file):
    """An uploaded PNG or JPG, usually a scan or photo on white paper."""
    if file.size > MAX_SIGNATURE_BYTES:
        raise ValidationError('Signature image must be 2 MB or smaller.')
    return _clean_signature(file.read(), remove_white=True)


def _clean_signature(raw, *, remove_white):
    """Return the signature cropped to its strokes and re-encoded as PNG."""
    if len(raw) > MAX_SIGNATURE_BYTES:
        raise ValidationError('Signature image must be 2 MB or smaller.')
    try:
        with Image.open(io.BytesIO(raw)) as img:
            if img.format not in ('PNG', 'JPEG'):
                raise ValidationError('Signature image must be a PNG or JPG.')
            # Checked from the header, before the pixels are decoded
            if img.width * img.height > MAX_SIGNATURE_PIXELS:
                raise ValidationError('Signature image is too large.')
            img = ImageOps.exif_transpose(img).convert('RGBA')
    except (OSError, Image.DecompressionBombError):
        raise ValidationError('Invalid signature image.')

    if remove_white:
        # Near-white paper becomes transparent, so no white box covers the PDF
        ink = img.convert('L').point(lambda v: 0 if v > WHITE_THRESHOLD else 255)
        img.putalpha(ImageChops.multiply(img.getchannel('A'), ink))

    # Transparent background, so the alpha channel bounds the strokes
    bbox = img.getchannel('A').getbbox()
    if not bbox:
        raise ValidationError('Signature is empty.')
    img = img.crop(bbox)
    img.thumbnail(SIGNATURE_SIZE)

    output = io.BytesIO()
    img.save(output, format='PNG')
    return output.getvalue()


def saved_signature_png(user):
    """The user's saved default signature, or None."""
    saved = UserSignature.objects.filter(user=user).first()
    if saved is None:
        return None
    try:
        with saved.image.open('rb') as f:
            return f.read()
    except FileNotFoundError:
        # The image is gone from media/ (e.g. not restored after a rebuild);
        # treat it as no saved signature rather than failing the signing page
        logger.warning("Saved signature file missing for %s", user)
        return None


def saved_signature_data_url(user):
    """The saved signature as a data URL for an <img>, or None. Inline because it
    is small and only ever shown to its owner."""
    png = saved_signature_png(user)
    if png is None:
        return None
    return SIGNATURE_DATA_URL_PREFIX + base64.b64encode(png).decode()


def save_user_signature(user, png):
    """Make png the user's default signature, replacing any earlier one."""
    UserSignature.objects.update_or_create(
        user=user,
        defaults={'image': ContentFile(png, name=f'{uuid.uuid4().hex}.png')},
    )


def _read_pdf(file):
    with file.file.open('rb') as f:
        data = f.read()
    try:
        reader = PdfReader(io.BytesIO(data))
        encrypted = reader.is_encrypted
        if not encrypted:
            len(reader.pages)  # Fails on a damaged page tree
    except Exception:
        # Uploaded files are untrusted and pypdf raises many error types
        raise ValidationError('This PDF file cannot be read.')
    if encrypted:
        raise ValidationError('Password-protected PDFs cannot be signed.')
    return data


@transaction.atomic
def create_draft(*, source_path, signers, notify_users, placement_mode, actor):
    """Save a signing request without sending it. The file is copied now, so
    signature boxes are placed on exactly the pages that will be signed."""
    file = validate_source(actor, source_path)
    data = _read_pdf(file)

    signing_request = SigningRequest(
        created_by=actor,
        placement_mode=placement_mode,
        status=SigningRequest.Status.DRAFT,
        file=file,
        original_sha=_sha256(data),
    )
    signing_request.original_file.save(
        f'{uuid.uuid4().hex}.pdf', ContentFile(data), save=False
    )
    try:
        # Savepoint, so a duplicate request leaves the outer transaction usable
        with transaction.atomic():
            signing_request.save()
    except IntegrityError:
        # Another request for the same file was created since validate_source()
        signing_request.original_file.delete(save=False)
        raise ValidationError('This file already has a signing request.')

    Signer.objects.bulk_create(
        Signer(signing_request=signing_request, user=user, signing_order=order)
        for order, user in enumerate(signers, start=1)
    )
    signing_request.notify_users.set(u for u in notify_users if u not in signers)
    SigningActivity.objects.create(
        signing_request=signing_request,
        actor=actor,
        action=SigningActivity.Action.CREATED,
    )
    return signing_request


@transaction.atomic
def send(signing_request, actor):
    """Send a draft to its first signer."""
    signing_request = SigningRequest.objects.select_for_update().get(
        pk=signing_request.pk
    )
    if signing_request.status != SigningRequest.Status.DRAFT:
        raise ValidationError('This signing request has already been sent.')
    if (
        signing_request.placement_mode == SigningRequest.PlacementMode.MANUAL
        and signing_request.signers.exclude(
            fields__field_type=SigningField.FieldType.SIGNATURE
        ).exists()
    ):
        raise ValidationError('Place a signature box for every signer.')
    # Drafts are not locked, and the boxes were placed on the copy taken with the draft
    current_sha = _file_sha(signing_request.file.file.path)
    if current_sha != signing_request.original_sha:
        raise ValidationError(
            'The file has changed since this draft was saved. '
            'Delete the draft and start again.'
        )

    first_signer = signing_request.signers.select_related('user').first()
    first_signer.status = Signer.Status.PENDING
    first_signer.save(update_fields=['status'])
    signing_request.status = SigningRequest.Status.IN_PROGRESS
    signing_request.sent_at = timezone.now()
    signing_request.save(update_fields=['status', 'sent_at'])
    SigningActivity.objects.create(
        signing_request=signing_request, actor=actor, action=SigningActivity.Action.SENT
    )
    # The sender is already here, so they need no email when they sign first
    if first_signer.user != actor:
        transaction.on_commit(lambda: notifications.notify_signing_turn(first_signer))
    return signing_request


@transaction.atomic
def create_and_send(*, source_path, signers, notify_users, actor):
    """Auto placement: nothing to place, so create and send in one step."""
    signing_request = create_draft(
        source_path=source_path,
        signers=signers,
        notify_users=notify_users,
        placement_mode=SigningRequest.PlacementMode.AUTO,
        actor=actor,
    )
    return send(signing_request, actor)


@transaction.atomic
def save_fields(signing_request, fields):
    """Replace a draft's boxes. Each field is a dict with signer (pk), field_type,
    page (1-based) and x, y, width, height as 0-1 ratios of the page shown."""
    if signing_request.status != SigningRequest.Status.DRAFT:
        raise ValidationError('Boxes can only be changed before the request is sent.')
    signers = {signer.pk: signer for signer in signing_request.signers.all()}
    with signing_request.original_file.open('rb') as f:
        page_count = len(PdfReader(f).pages)

    rows, seen = [], set()
    for field in fields:
        try:
            signer = signers[int(field['signer'])]
            field_type = field['field_type']
            page = int(field['page'])
            x, y, width, height = (
                float(field[k]) for k in ('x', 'y', 'width', 'height')
            )
        except (KeyError, TypeError, ValueError):
            raise ValidationError('Invalid signature box.')
        if (
            field_type not in SigningField.FieldType.values
            or (signer.pk, field_type) in seen
        ):
            raise ValidationError('Invalid signature box.')
        # A little slack for rounding in the browser
        if not (
            1 <= page <= page_count
            and 0 <= x
            and 0 <= y
            and 0 < width
            and 0 < height
            and x + width <= 1.001
            and y + height <= 1.001
        ):
            raise ValidationError('A signature box is outside the page.')
        seen.add((signer.pk, field_type))
        rows.append(
            SigningField(
                signer=signer,
                field_type=field_type,
                page=page,
                x=x,
                y=y,
                width=width,
                height=height,
            )
        )

    SigningField.objects.filter(signer__signing_request=signing_request).delete()
    SigningField.objects.bulk_create(rows)


def delete_draft(signing_request):
    """Delete a request that was never sent; its file copy goes with it."""
    if signing_request.status != SigningRequest.Status.DRAFT:
        raise ValidationError('Only drafts can be deleted.')
    signing_request.delete()


def sign(*, signing_request, actor, signature_png, save_as_default=False):
    """Apply the actor's signature and pass the request to the next signer,
    completing it after the last one. save_as_default also keeps the signature
    as the actor's default for next time."""
    with transaction.atomic():
        signing_request = SigningRequest.objects.select_for_update().get(
            pk=signing_request.pk
        )
        if signing_request.status != SigningRequest.Status.IN_PROGRESS:
            raise ValidationError('This signing request is not in progress.')

        signer = signing_request.signers.filter(status=Signer.Status.PENDING).first()
        if signer is None or signer.user_id != actor.pk:
            raise ValidationError('It is not your turn to sign.')

        signer.signature_image.save(
            f'{uuid.uuid4().hex}.png', ContentFile(signature_png), save=False
        )
        signer.status = Signer.Status.SIGNED
        signer.signed_at = timezone.now()
        signer.save(update_fields=['signature_image', 'status', 'signed_at'])
        if save_as_default:
            save_user_signature(actor, signature_png)

        SigningActivity.objects.create(
            signing_request=signing_request,
            actor=actor,
            action=SigningActivity.Action.SIGNED,
        )

        # Stamp after every signature, so the next signer sees the ones before
        # theirs. Always rebuilt from the original, never layered on the last one.
        pdf = render_signed_pdf(signing_request)
        signing_request.signed_file.save(
            f'{uuid.uuid4().hex}.pdf', ContentFile(pdf), save=False
        )
        signing_request.save(update_fields=['signed_file'])

        next_signer = (
            signing_request.signers.filter(status=Signer.Status.WAITING)
            .select_related('user')
            .first()
        )
        if next_signer:
            next_signer.status = Signer.Status.PENDING
            next_signer.save(update_fields=['status'])
            transaction.on_commit(
                lambda: notifications.notify_signing_turn(next_signer)
            )
        else:
            _complete(signing_request, actor, pdf)

    return signing_request


def signers_to_remind(now=None):
    """Signers whose turn has waited at least REMINDER_AFTER. Deactivated accounts
    are skipped: they cannot sign, so an admin has to replace them."""
    now = now or timezone.now()
    pending = Signer.objects.filter(
        status=Signer.Status.PENDING,
        signing_request__status=SigningRequest.Status.IN_PROGRESS,
        user__is_active=True,
    ).select_related('user', 'signing_request__created_by')

    # One pending signer per request (they sign in turn), and this runs once a
    # day, so a query per signer below is fine
    to_remind = []
    for signer in pending:
        signer.turn_started = _turn_started(signer)
        if now - signer.turn_started >= REMINDER_AFTER:
            to_remind.append(signer)
    return to_remind


def _turn_started(signer):
    """When it became this signer's turn: when the person before them signed,
    or for the first signer, when the request was sent."""
    previous = (
        signer.signing_request.signers.filter(signing_order__lt=signer.signing_order)
        .order_by('-signing_order')
        .first()
    )
    if previous is None:
        return signer.signing_request.sent_at
    return previous.signed_at


def _complete(signing_request, actor, pdf):
    """pdf is the fully signed PDF, already saved as signed_file."""
    # signed_sha is only set here: it marks the final version
    signing_request.signed_sha = _sha256(pdf)
    signing_request.status = SigningRequest.Status.COMPLETED
    signing_request.completed_at = timezone.now()
    signing_request.save(update_fields=['signed_sha', 'status', 'completed_at'])

    # Last step: overwriting the original cannot be rolled back
    reason = _write_back(signing_request, pdf)
    if reason:
        detail = {'written_back': False, 'reason': reason}
    else:
        detail = {'written_back': True}
        file = signing_request.file
        log_activity(
            actor,
            file.name,
            posixpath.join(filesystem.get_path(file.folder), file.name),
            'sign',
            None,
            len(pdf),
        )

    SigningActivity.objects.create(
        signing_request=signing_request,
        actor=actor,
        action=SigningActivity.Action.COMPLETED,
        detail=detail,
    )
    transaction.on_commit(
        lambda: notifications.notify_signing_completed(signing_request, detail, actor)
    )


def _file_sha(full_path):
    """SHA-256 of a file, or None if it does not exist."""
    try:
        with open(full_path, 'rb') as f:
            return hashlib.file_digest(f, 'sha256').hexdigest()
    except FileNotFoundError:
        return None


def _replace_file(full_path, data):
    # Write next to the file, then swap, so it is never left half-written
    fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(full_path), suffix='.tmp')
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
        shutil.copymode(full_path, tmp_path)  # mkstemp creates it owner-only
        os.replace(tmp_path, full_path)
    except OSError:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def _replace_content(file, data):
    """Overwrite file's content on disk in place, keeping its row in step."""
    _replace_file(file.file.path, data)
    file.size = len(data)
    file.save(update_fields=['size', 'updated_at'])


def _write_back(signing_request, pdf):
    """Replace the original file with the signed PDF. Return the reason it was
    not replaced, or '' on success."""
    file = signing_request.file
    current_sha = _file_sha(file.file.path)
    if current_sha is None:
        return 'source_missing'
    if current_sha != signing_request.original_sha:
        return 'source_modified'

    try:
        _replace_content(file, pdf)
    except OSError:
        logger.exception("Write back failed for %s", file.file.name)
        return 'write_failed'
    return ''


@transaction.atomic
def reset(signing_request):
    """Undo a signing request as if it was never sent: put the original file back
    if the signed PDF replaced it, then delete the request along with its signers,
    signatures and file copies. A file changed after signing is left as it is."""
    file = signing_request.file
    if (
        signing_request.signed_sha
        and _file_sha(file.file.path) == signing_request.signed_sha
    ):
        with signing_request.original_file.open('rb') as f:
            original = f.read()
        try:
            _replace_content(file, original)
        except OSError:
            logger.exception("Restore failed for %s", file.file.name)
            raise ValidationError(f'Could not restore the original file {file.name}.')
    signing_request.delete()


def render_signed_pdf(signing_request):
    """The original PDF with the signatures: in their boxes (manual), or in a
    signature table appended as the last page(s) (auto)."""
    if signing_request.placement_mode == SigningRequest.PlacementMode.MANUAL:
        return _render_on_fields(signing_request)
    writer = PdfWriter()
    with signing_request.original_file.open('rb') as f:
        for page in PdfReader(f).pages:
            writer.add_page(page)
        for page in PdfReader(
            io.BytesIO(_render_signature_table(signing_request))
        ).pages:
            writer.add_page(page)

        output = io.BytesIO()
        writer.write(output)
    return output.getvalue()


def _render_on_fields(signing_request):
    fields_by_page = defaultdict(list)
    # Only people who have signed so far; the others' boxes stay empty
    for field in SigningField.objects.filter(
        signer__signing_request=signing_request, signer__status=Signer.Status.SIGNED
    ).select_related('signer'):
        fields_by_page[field.page].append(field)

    writer = PdfWriter()
    with signing_request.original_file.open('rb') as f:
        for number, page in enumerate(PdfReader(f).pages, start=1):
            if number in fields_by_page:
                # Boxes were placed on the page as displayed, rotation included;
                # make the content upright too so the coordinates line up
                page.transfer_rotation_to_content()
                overlay = _render_overlay(page, fields_by_page[number])
                page.merge_page(PdfReader(io.BytesIO(overlay)).pages[0])
            writer.add_page(page)
        output = io.BytesIO()
        writer.write(output)
    return output.getvalue()


def _render_overlay(page, fields):
    """A transparent page with each signer's signature or date drawn in its box."""
    # The visible area (crop box) is what the sender saw, and may not start at 0,0
    box = page.cropbox
    left, bottom = float(box.left), float(box.bottom)
    page_width, page_height = float(box.width), float(box.height)

    output = io.BytesIO()
    pdf = canvas.Canvas(
        output, pagesize=(float(page.mediabox.right), float(page.mediabox.top))
    )
    for field in fields:
        width = field.width * page_width
        height = field.height * page_height
        # Ratios are measured from the top left; PDF coordinates from the bottom left
        x = left + field.x * page_width
        y = bottom + (1 - field.y - field.height) * page_height
        if field.field_type == SigningField.FieldType.SIGNATURE:
            pdf.drawImage(
                ImageReader(field.signer.signature_image.path),
                x,
                y,
                width,
                height,
                mask='auto',
                preserveAspectRatio=True,
                anchor='c',
            )
        else:
            size = min(height * 0.6, 12)
            pdf.setFont(TABLE_FONT, size)
            pdf.drawCentredString(
                x + width / 2,
                y + (height - size) / 2 + size * 0.2,
                timezone.localtime(field.signer.signed_at).strftime('%Y-%m-%d'),
            )
    pdf.save()
    return output.getvalue()


def _render_signature_table(signing_request):
    title_style = ParagraphStyle('title', fontName=TABLE_FONT, fontSize=16, leading=20)
    text_style = ParagraphStyle('text', fontName=TABLE_FONT, fontSize=10, leading=13)

    # Every signer from the start, in signing order, so the table keeps its
    # layout; rows of people who have not signed yet stay empty
    rows = [['Name', 'Signature', 'Date']]
    for signer in signing_request.signers.select_related('user'):
        name = Paragraph(display_name(signer.user), text_style)
        if signer.status != Signer.Status.SIGNED:
            rows.append([name, '', ''])
            continue
        rows.append(
            [
                name,
                PdfImage(
                    signer.signature_image.path,
                    width=60 * mm,
                    height=TABLE_SIGNATURE_HEIGHT,
                    kind='proportional',
                ),
                timezone.localtime(signer.signed_at).strftime('%Y-%m-%d'),
            ]
        )

    # 159mm: the A4 width inside SimpleDocTemplate's default 1-inch margins.
    # Signer rows fit a signature plus 6pt padding above and below, signed or
    # not, so empty rows leave room to sign
    table = Table(
        rows,
        colWidths=[55 * mm, 70 * mm, 34 * mm],
        rowHeights=[None] + [TABLE_SIGNATURE_HEIGHT + 12] * (len(rows) - 1),
        repeatRows=1,
    )
    table.setStyle(
        TableStyle(
            [
                ('FONTNAME', (0, 0), (-1, -1), TABLE_FONT),
                ('FONTSIZE', (0, 0), (-1, -1), 10),
                ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#eef2f6')),
                ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#cbd3dd')),
                ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                ('TOPPADDING', (0, 1), (-1, -1), 6),
                ('BOTTOMPADDING', (0, 1), (-1, -1), 6),
            ]
        )
    )

    output = io.BytesIO()
    doc = SimpleDocTemplate(output, pagesize=A4, title='Signatures')
    doc.build(
        [
            Paragraph('Signatures', title_style),
            Spacer(1, 6 * mm),
            table,
        ]
    )
    return output.getvalue()
