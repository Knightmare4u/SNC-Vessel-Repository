import json
import logging
import mimetypes
import os
import posixpath
import re
import shutil
from datetime import datetime
from pathlib import Path

import mammoth
import openpyxl
import xlrd
import zipstream
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import PasswordChangeForm
from django.core.exceptions import (
    PermissionDenied,
    SuspiciousFileOperation,
    ValidationError,
)
from django.db import transaction
from django.db.models import Q
from django.http import (
    FileResponse,
    Http404,
    HttpResponse,
    JsonResponse,
    StreamingHttpResponse,
)
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.html import escape
from django.utils.http import content_disposition_header
from django.utils.text import get_valid_filename
from django.views.decorators.http import require_POST
from xlrd.xldate import xldate_as_datetime

from .models import FileActivity, FolderPermission, UploadSession, UserProfile
from .utils import (
    format_file_size,
    get_storage_path,
    get_user_permissions,
    has_permission,
    is_in_archive,
    log_activity,
    normalize_path,
    to_url_path,
    validate_name,
)

logger = logging.getLogger(__name__)

ARCHIVE_READ_ONLY = 'The archive is read-only'

FILE_TYPE_CATEGORIES = {
    'document': ['.doc', '.docx', '.txt'],
    'pdf': ['.pdf'],
    'spreadsheet': ['.xls', '.xlsx'],
    'presentation': ['.ppt', '.pptx'],
    'image': ['.jpg', '.jpeg', '.png', '.gif', '.svg', '.webp', '.bmp', '.ico'],
    'video': ['.mp4', '.webm', '.avi', '.mov', '.wmv'],
    'audio': ['.mp3', '.wav', '.ogg', '.flac', '.m4a', '.aac'],
    'zip': ['.zip', '.rar', '.7z', '.tar', '.gz'],
}

CATEGORY_ICONS = {
    'document': '📘',
    'pdf': '📕',
    'spreadsheet': '📗',
    'presentation': '📙',
    'image': '🖼️',
    'video': '🎬',
    'audio': '🎵',
    'zip': '📦',
}

FILE_ICONS = {
    **{
        ext: CATEGORY_ICONS[category]
        for category, exts in FILE_TYPE_CATEGORIES.items()
        for ext in exts
    },
    '.txt': '📄',  # Plain text, not a Word document
}


def get_file_icon(extension):
    return FILE_ICONS.get(extension, '📄')


def get_item_urls(path, is_dir):
    """URLs for a file browser item, built with reverse() so names are encoded."""
    url_path = to_url_path(path)
    if is_dir:
        return {'browser_url': reverse('file_browser', args=[url_path])}
    return {
        'download_url': reverse('download_file', args=[url_path]),
        'preview_url': reverse('file_preview', args=[url_path]),
    }


@login_required
def change_password(request):
    if request.method == 'POST':
        form = PasswordChangeForm(request.user, request.POST)
        if form.is_valid():
            user = form.save()
            update_session_auth_hash(request, user)

            # Mark password as changed
            profile, created = UserProfile.objects.get_or_create(user=user)
            profile.password_changed = True
            profile.save()

            messages.success(request, 'Password changed successfully!')
            return redirect('dashboard')
    else:
        form = PasswordChangeForm(request.user)

    return render(request, 'filemanager/change_password.html', {'form': form})


@login_required
def dashboard(request):
    base_path = settings.FILE_STORAGE_ROOT
    user_permissions = get_user_permissions(request.user)

    # Get accessible folders
    accessible_folders = []
    for perm in user_permissions:
        folder_path = perm['folder_path']
        full_path = os.path.join(base_path, folder_path.lstrip('/'))
        if os.path.exists(full_path):
            accessible_folders.append(
                {
                    'name': os.path.basename(folder_path),
                    'path': to_url_path(folder_path),
                    'permission': perm['permission'],
                }
            )

    # Get recent activities
    recent_activities = FileActivity.objects.filter(user=request.user).order_by(
        '-timestamp'
    )[:10]

    context = {
        'accessible_folders': accessible_folders,
        'user_permissions': user_permissions,
        'recent_activities': recent_activities,
    }
    return render(request, 'filemanager/dashboard.html', context)


@login_required
def file_browser(request, folder_path=''):
    folder_path = to_url_path(folder_path)
    if not has_permission(request.user, folder_path, 'read'):
        messages.error(request, 'You do not have permission to access this folder')
        return redirect('dashboard')

    full_path = get_storage_path(folder_path)

    archive_path = Path(settings.ARCHIVE_ROOT)
    in_archive = is_in_archive(full_path)

    if not folder_path:
        # Storage root may not exist yet on a fresh deployment
        os.makedirs(full_path, exist_ok=True)
    elif not os.path.isdir(full_path):
        raise Http404('Folder not found')

    # Get files and folders
    items = []
    total_size = 0
    file_count = 0
    folder_count = 0

    try:
        names = os.listdir(full_path)
    except OSError:
        logger.exception("Error accessing folder %s", full_path)
        messages.error(request, 'Error accessing folder')
        names = []

    for item in names:
        item_path = os.path.join(full_path, item)
        rel_path = os.path.join(folder_path, item).replace('\\', '/')

        try:
            if os.path.isdir(item_path):
                try:
                    size = f'{len(os.listdir(item_path))} items'
                except OSError:
                    # Folder is fine, its contents just can't be counted
                    logger.warning("Cannot list folder %s", item_path)
                    size = '—'
                items.append(
                    {
                        'name': item,
                        'type': 'folder',
                        'path': rel_path,
                        'size': size,
                        'size_bytes': 0,
                        'modified': datetime.fromtimestamp(os.path.getmtime(item_path)),
                        'created_at': datetime.fromtimestamp(
                            os.path.getctime(item_path)
                        ),
                        'icon': '📁',
                        'is_archive': Path(item_path) == archive_path,
                    }
                )
                folder_count += 1
            else:
                size = os.path.getsize(item_path)
                items.append(
                    {
                        'name': item,
                        'type': 'file',
                        'path': rel_path,
                        'size': format_file_size(size),
                        'size_bytes': size,
                        'modified': datetime.fromtimestamp(os.path.getmtime(item_path)),
                        'created_at': datetime.fromtimestamp(
                            os.path.getctime(item_path)
                        ),
                        'extension': os.path.splitext(item)[1].lower(),
                        'icon': get_file_icon(os.path.splitext(item)[1].lower()),
                    }
                )
                total_size += size
                file_count += 1
        except OSError:
            # One unreadable entry must not hide the rest of the folder
            logger.warning("Skipping unreadable entry %s", item_path)

    # Sort: folders first, then files
    items.sort(key=lambda x: (x['type'] != 'folder', x['name'].lower()))

    # Breadcrumb
    breadcrumbs = []
    if folder_path:
        parts = folder_path.split('/')
        for i, part in enumerate(parts):
            if part:  # Skip empty parts
                path = '/'.join(parts[: i + 1])
                breadcrumbs.append({'name': part, 'path': path})

    context = {
        'current_path': folder_path,
        'items': items,
        'breadcrumbs': breadcrumbs,
        'can_upload': has_permission(request.user, folder_path, 'write'),
        'can_delete': has_permission(request.user, folder_path, 'admin'),
        'can_rename': has_permission(request.user, folder_path, 'write'),
        'can_create_folder': has_permission(request.user, folder_path, 'write'),
        'in_archive': in_archive,
        'total_size': format_file_size(total_size),
        'file_count': file_count,
        'folder_count': folder_count,
    }
    return render(request, 'filemanager/file_browser.html', context)


@login_required
def download_file(request, file_path):
    file_path = normalize_path(file_path)
    if not has_permission(request.user, os.path.dirname(file_path), 'read'):
        raise PermissionDenied

    full_path = get_storage_path(file_path)

    if os.path.isfile(full_path):
        # Log download activity
        log_activity(
            request.user,
            os.path.basename(file_path),
            file_path,
            'download',
            request.META.get('REMOTE_ADDR'),
            os.path.getsize(full_path),
        )

        content_type, encoding = mimetypes.guess_type(full_path)
        response = FileResponse(
            open(full_path, 'rb'),
            content_type=content_type,
            as_attachment=True,
            filename=os.path.basename(file_path),
        )
        return response

    raise Http404('File not found')


@login_required
@require_POST
def upload_file(request):
    folder_path = normalize_path(request.POST.get('folder_path', ''))

    if not has_permission(request.user, folder_path, 'write'):
        return JsonResponse({'error': 'Permission denied'}, status=403)

    full_path = get_storage_path(folder_path)

    if is_in_archive(full_path):
        return JsonResponse({'error': ARCHIVE_READ_ONLY}, status=403)

    if not os.path.isdir(full_path):
        return JsonResponse({'error': 'Folder not found'}, status=404)

    files = request.FILES.getlist('files')
    uploaded_files = []
    total_size = 0

    # Validate every name first so a bad name rejects the batch before any write
    filenames = []
    for file in files:
        try:
            filenames.append(get_valid_filename(file.name))
        except SuspiciousFileOperation:
            return JsonResponse(
                {'error': f'Invalid file name: {file.name}'}, status=400
            )

    for file, filename in zip(files, filenames):
        try:
            file_path = os.path.join(full_path, filename)

            # Exclusive create ('xb') so a concurrent upload is never overwritten
            counter = 1
            name, ext = os.path.splitext(filename)
            while True:
                try:
                    destination = open(file_path, 'xb')
                    break
                except FileExistsError:
                    pass
                except PermissionError:
                    # Windows raises this instead when a folder has the name
                    if not os.path.isdir(file_path):
                        raise
                filename = f"{name}_{counter}{ext}"
                file_path = os.path.join(full_path, filename)
                counter += 1

            # Save file
            written = 0

            try:
                with destination:
                    for chunk in file.chunks():
                        destination.write(chunk)
                        written += len(chunk)
            except Exception:
                os.remove(file_path)  # Don't leave a partial file behind
                raise

            if written != file.size:
                logger.warning(
                    "Size mismatch for %s: expected %s, wrote %s",
                    file_path,
                    file.size,
                    written,
                )

            file_size = os.path.getsize(file_path)
            total_size += file_size

            # Log upload activity
            relative_path = os.path.join(folder_path, filename).replace('\\', '/')
            log_activity(
                request.user,
                filename,
                relative_path,
                'upload',
                request.META.get('REMOTE_ADDR'),
                file_size,
            )

            uploaded_files.append(
                {
                    'name': filename,
                    'size': file_size,
                    'formatted_size': format_file_size(file_size),
                }
            )

            logger.info(
                "Uploaded %s (%s bytes) by %s", relative_path, file_size, request.user
            )

        except OSError:
            logger.exception("Upload error for %s", file.name)
            return JsonResponse({'error': f'Error uploading {file.name}'}, status=500)

    return JsonResponse(
        {
            'success': True,
            'uploaded_files': uploaded_files,
            'total_size': total_size,
            'formatted_total_size': format_file_size(total_size),
        }
    )


@login_required
def get_upload_progress(request, session_id):
    try:
        session = UploadSession.objects.get(session_id=session_id, user=request.user)
        return JsonResponse(
            {
                'total_files': session.total_files,
                'completed_files': session.completed_files,
                'total_size': session.total_size,
                'uploaded_size': session.uploaded_size,
                'status': session.status,
                'progress': (
                    (session.completed_files / session.total_files * 100)
                    if session.total_files > 0
                    else 0
                ),
            }
        )
    except UploadSession.DoesNotExist:
        return JsonResponse({'error': 'Session not found'}, status=404)


@login_required
@require_POST
def delete_file(request):
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    item_path = normalize_path(data.get('path', ''))

    if item_path == '/':
        return JsonResponse({'error': 'Cannot delete the root folder'}, status=400)

    if not has_permission(request.user, os.path.dirname(item_path), 'admin'):
        return JsonResponse({'error': 'Permission denied'}, status=403)

    full_path = get_storage_path(item_path)

    if is_in_archive(full_path):
        return JsonResponse({'error': ARCHIVE_READ_ONLY}, status=403)

    try:
        if not os.path.exists(full_path):
            return JsonResponse({'error': 'File/folder not found'}, status=404)

        file_size = os.path.getsize(full_path) if os.path.isfile(full_path) else 0

        # Build archive destination path, mirroring the original structure
        archive_dest = get_storage_path(item_path, root=settings.ARCHIVE_ROOT)

        # Avoid overwriting existing archive entries by appending a timestamp
        if os.path.exists(archive_dest):
            name, ext = os.path.splitext(archive_dest)
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            archive_dest = f'{name}_{timestamp}{ext}'

        os.makedirs(os.path.dirname(archive_dest), exist_ok=True)
        with transaction.atomic():
            if os.path.isdir(full_path):
                # Grants on a deleted folder must not carry over to a new
                # folder created later with the same name
                FolderPermission.objects.filter(
                    Q(folder_path=item_path)
                    | Q(folder_path__startswith=item_path + '/')
                ).delete()
            # A failed move rolls the permission delete back
            shutil.move(full_path, archive_dest)

        log_activity(
            request.user,
            os.path.basename(item_path),
            item_path,
            'delete',
            request.META.get('REMOTE_ADDR'),
            file_size,
        )

        return JsonResponse({'success': True})
    except OSError:
        logger.exception("Delete failed for %s", item_path)
        return JsonResponse({'error': 'Delete failed'}, status=500)


@login_required
def search_files(request):
    query = request.GET.get('q', '').strip()
    if not query:
        return JsonResponse({'results': []})

    file_type = request.GET.get('type', '')
    folder_filter = request.GET.get('folder', '')

    base_path = os.path.realpath(settings.FILE_STORAGE_ROOT)

    results = []
    seen = set()

    allowed_exts = FILE_TYPE_CATEGORIES.get(file_type, []) if file_type else []
    user_permissions = get_user_permissions(request.user)

    # -------------------------
    # Safe join (avoid path traversal + symlink escape)
    # -------------------------
    def safe_join(base, relative_path):
        target = os.path.realpath(os.path.join(base, relative_path.lstrip('/')))

        if target != base and not target.startswith(base + os.sep):
            return None

        return target

    # -------------------------
    # Collect results (safe walk)
    # -------------------------
    def collect_results(search_path):
        if not search_path or not os.path.exists(search_path):
            return

        for root, _, files in os.walk(search_path, followlinks=False):
            # Ensure traversal never escapes the storage root
            root_real_path = os.path.realpath(root)
            if root_real_path != base_path and not root_real_path.startswith(
                base_path + os.sep
            ):
                continue

            for file in files:
                if query.lower() not in file.lower():
                    continue

                file_path = os.path.join(root, file)
                file_real = os.path.realpath(file_path)

                # Ensure file remains inside the storage root
                if file_real != base_path and not file_real.startswith(
                    base_path + os.sep
                ):
                    continue

                rel_path = os.path.relpath(file_real, base_path).replace('\\', '/')

                # Prevent duplicate results
                if rel_path in seen:
                    continue

                # File type filtering
                ext = os.path.splitext(file)[1].lower()
                if allowed_exts and ext not in allowed_exts:
                    continue

                try:
                    stat = os.stat(file_real)
                except (FileNotFoundError, PermissionError, OSError):
                    continue

                seen.add(rel_path)

                results.append(
                    {
                        'name': file,
                        'path': rel_path,
                        'folder': '/' + os.path.dirname(rel_path),
                        'size': stat.st_size,
                        'formatted_size': format_file_size(stat.st_size),
                        'extension': ext,
                        'icon': get_file_icon(ext),
                        'modified': stat.st_mtime,
                        'created': stat.st_ctime,
                        **get_item_urls(rel_path, is_dir=False),
                    }
                )

    # -------------------------
    # Search within a specific folder
    # -------------------------
    if folder_filter:
        folder = normalize_path(folder_filter)

        if not has_permission(request.user, folder, 'read'):
            return JsonResponse({'error': 'Permission denied'}, status=403)

        collect_results(get_storage_path(folder))

    # -------------------------
    # Search across all permitted folders
    # -------------------------
    else:
        permitted = sorted(
            perm['folder_path'].rstrip('/')
            for perm in user_permissions
            if perm['permission'] in ['read', 'write', 'admin']
        )

        deduped = []

        for path in permitted:
            if any(
                path == parent or path.startswith(parent + '/') for parent in deduped
            ):
                continue

            deduped.append(path)

        for path in deduped:
            target_path = safe_join(base_path, path)

            if target_path:
                collect_results(target_path)

    return JsonResponse({'results': results})


@login_required
@require_POST
def create_folder(request):
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    folder_path = normalize_path(data.get('folder_path', ''))
    folder_name = data.get('folder_name', '')

    if not has_permission(request.user, folder_path, 'write'):
        return JsonResponse({'error': 'Permission denied'}, status=403)

    try:
        validate_name(folder_name)
    except ValidationError as e:
        return JsonResponse({'error': e.messages[0]}, status=400)

    full_path = get_storage_path(posixpath.join(folder_path, folder_name))

    if is_in_archive(full_path):
        return JsonResponse({'error': ARCHIVE_READ_ONLY}, status=403)

    # The parent may have been deleted while the user's page was open
    if not os.path.isdir(get_storage_path(folder_path)):
        return JsonResponse({'error': 'Folder not found'}, status=404)

    if os.path.exists(full_path):
        return JsonResponse(
            {'error': 'A folder with that name already exists'}, status=400
        )

    try:
        # mkdir, not makedirs: never recreate missing parent folders
        os.mkdir(full_path)
        return JsonResponse({'success': True})
    except OSError:
        logger.exception("Could not create folder %s", full_path)
        return JsonResponse({'error': 'Could not create folder'}, status=500)


@login_required
@require_POST
def rename_file(request):
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    old_path = normalize_path(data.get('old_path', ''))
    new_name = data.get('new_name', '').strip()

    if old_path == '/':
        return JsonResponse({'error': 'Old path is required'}, status=400)

    if not new_name:
        return JsonResponse({'error': 'New name is required'}, status=400)

    folder = os.path.dirname(old_path)

    if not has_permission(request.user, folder, 'write'):
        return JsonResponse({'error': 'Permission denied'}, status=403)

    full_old_path = get_storage_path(old_path)

    # Folders keep the user's name; files are sanitized like uploads
    if os.path.isdir(full_old_path):
        try:
            validate_name(new_name)
        except ValidationError as e:
            return JsonResponse({'error': e.messages[0]}, status=400)
    else:
        try:
            new_name = get_valid_filename(new_name)
        except SuspiciousFileOperation:
            return JsonResponse({'error': 'Invalid file name'}, status=400)

    new_path = posixpath.join(folder, new_name)
    full_new_path = get_storage_path(new_path)

    if is_in_archive(full_old_path) or is_in_archive(full_new_path):
        return JsonResponse({'error': ARCHIVE_READ_ONLY}, status=403)

    if not os.path.exists(full_old_path):
        return JsonResponse({'error': 'File/Folder not found'}, status=404)

    if full_old_path == full_new_path:
        return JsonResponse(
            {
                'success': True,
                'new_path': to_url_path(new_path),
                **get_item_urls(new_path, os.path.isdir(full_new_path)),
            }
        )

    # samefile: on case-insensitive filesystems a case-only rename "exists" already
    if os.path.exists(full_new_path) and not os.path.samefile(
        full_old_path, full_new_path
    ):
        return JsonResponse(
            {'error': 'A file/folder with that name already exists'}, status=400
        )

    try:
        with transaction.atomic():
            if os.path.isdir(full_old_path):
                old_folder_path = old_path
                new_folder_path = new_path

                permissions = FolderPermission.objects.select_for_update().filter(
                    Q(folder_path=old_folder_path)
                    | Q(folder_path__startswith=old_folder_path + '/')
                )

                perms_to_update = []

                for perm in permissions:
                    perm.folder_path = (
                        new_folder_path + perm.folder_path[len(old_folder_path) :]
                    )
                    perms_to_update.append(perm)

                if perms_to_update:
                    FolderPermission.objects.bulk_update(
                        perms_to_update, ['folder_path']
                    )

            os.rename(full_old_path, full_new_path)

        os.utime(full_new_path)
        modified_str = datetime.fromtimestamp(os.path.getmtime(full_new_path)).strftime(
            '%b %d, %Y %H:%M'
        )
        log_activity(
            request.user,
            f'{os.path.basename(old_path)} → {new_name}',
            new_path,
            'rename',
            request.META.get('REMOTE_ADDR'),
        )
        return JsonResponse(
            {
                'success': True,
                'new_path': to_url_path(new_path),
                'modified': modified_str,
                **get_item_urls(new_path, os.path.isdir(full_new_path)),
            }
        )

    except OSError:
        logger.exception("Rename failed for %s", old_path)
        return JsonResponse({'error': 'Rename failed'}, status=500)


@login_required
@require_POST
def bulk_download(request):
    file_paths = request.POST.getlist('paths')

    if not file_paths:
        return JsonResponse({'error': 'No files selected'}, status=400)

    file_paths = [normalize_path(p) for p in file_paths]

    # All-or-nothing: never hand out a zip that silently leaves files out
    for file_path in file_paths:
        if not has_permission(request.user, os.path.dirname(file_path), 'read'):
            raise PermissionDenied
        if not os.path.exists(get_storage_path(file_path)):
            raise Http404('File not found')

    zs = zipstream.ZipStream()
    total_size = 0
    used_names = set()

    def unique_name(name):
        # Search results can hold same-named files from different folders
        base, ext = os.path.splitext(name)
        candidate, counter = name, 1
        while candidate.lower() in used_names:
            candidate = f'{base}_{counter}{ext}'
            counter += 1
        used_names.add(candidate.lower())
        return candidate

    # Selected items go at the top level of the zip, as they appear on screen
    for file_path in file_paths:
        full_path = get_storage_path(file_path)
        top_name = unique_name(posixpath.basename(file_path))
        if os.path.isfile(full_path):
            zs.add_path(full_path, arcname=top_name)
            total_size += os.path.getsize(full_path)
        elif os.path.isdir(full_path):
            for root, _, files in os.walk(full_path):
                for filename in files:
                    file_full = os.path.join(root, filename)
                    rel = os.path.relpath(file_full, full_path).replace('\\', '/')
                    zs.add_path(file_full, arcname=f'{top_name}/{rel}', recurse=False)
                    total_size += os.path.getsize(file_full)

    current_folder = os.path.dirname(file_paths[0].rstrip('/\\')) or '/'

    MAX_NAMES_SHOWN = 5
    names = [os.path.basename(p.rstrip('/\\')) for p in file_paths[:MAX_NAMES_SHOWN]]
    name_summary = ', '.join(names)
    if len(file_paths) > MAX_NAMES_SHOWN:
        name_summary += f', +{len(file_paths) - MAX_NAMES_SHOWN} more'

    log_activity(
        request.user,
        name_summary,
        current_folder,
        'download',
        request.META.get('REMOTE_ADDR'),
        total_size,
    )

    response = StreamingHttpResponse(zs, content_type='application/zip')
    response['Content-Disposition'] = 'attachment; filename="download.zip"'
    return response


RANGE_HEADER_RE = re.compile(r'bytes=(\d*)-(\d*)')


def needs_sandbox(content_type):
    """Previewed user content gets a sandbox CSP so it cannot run scripts on this
    site's origin. PDF, video and audio are exempt: they cannot run scripts, and
    browsers' built-in viewers/players stop working in a sandboxed document."""
    mime = content_type.split(';')[0]
    return mime != 'application/pdf' and not mime.startswith(('video/', 'audio/'))


def _iter_file_chunk(f, length, chunk_size=8192):
    remaining = length
    try:
        while remaining > 0:
            data = f.read(min(chunk_size, remaining))
            if not data:
                break
            remaining -= len(data)
            yield data
    finally:
        f.close()


@login_required
def file_preview(request, file_path):
    def get_value(cell, wb):
        if cell.ctype == xlrd.XL_CELL_DATE:
            return xldate_as_datetime(cell.value, wb.datemode).strftime(
                '%Y-%m-%d %H:%M:%S'
            )
        return cell.value

    def render_row(row, i):
        tag = 'th' if i == 0 else 'td'
        return ''.join(
            f'<{tag}>{escape(cell) if cell is not None else ""}</{tag}>' for cell in row
        )

    file_path = normalize_path(file_path)
    if not has_permission(request.user, os.path.dirname(file_path), 'read'):
        raise PermissionDenied

    full_path = get_storage_path(file_path)

    if not os.path.isfile(full_path):
        raise Http404('File not found')

    # Log once here so every preview type (docx/xlsx return early) is recorded
    log_activity(
        request.user,
        os.path.basename(file_path),
        file_path,
        'view',
        request.META.get('REMOTE_ADDR'),
        os.path.getsize(full_path),
    )

    ext = os.path.splitext(full_path)[1].lower()
    if ext == '.docx':
        try:
            with open(full_path, 'rb') as f:
                result = mammoth.convert_to_html(f)
        except Exception:
            # Uploaded files are untrusted and the parsers raise many error types
            logger.exception("Preview failed for %s", full_path)
            return render(request, 'filemanager/preview_unavailable.html', status=422)
        html = (
            '<!DOCTYPE html><html><head><meta charset="utf-8">'
            '<style>body{font-family:sans-serif;max-width:900px;margin:40px auto;padding:0 20px}</style>'
            f'</head><body>{result.value}</body></html>'
        )
        response = HttpResponse(html, content_type='text/html; charset=utf-8')
        response['Content-Security-Policy'] = 'sandbox'
        return response

    if ext in ('.xlsx', '.xls'):
        sheets_html = []
        try:
            if ext == '.xls':
                wb = xlrd.open_workbook(full_path)
                for sheet_name in wb.sheet_names():
                    ws = wb.sheet_by_name(sheet_name)
                    rows_html = []
                    for i in range(ws.nrows):
                        row = [get_value(ws.cell(i, j), wb) for j in range(ws.ncols)]
                        if all(cell in ('', None) for cell in row):
                            continue
                        rows_html.append(f'<tr>{render_row(row, i)}</tr>')

                    sheets_html.append(
                        f'<h3>{escape(sheet_name)}</h3>'
                        f'<div class="table-wrap"><table>{"".join(rows_html)}</table></div>'
                    )
            else:
                wb = openpyxl.load_workbook(full_path, read_only=True, data_only=True)
                # read_only keeps the file open; close it even if reading fails
                try:
                    for sheet_name in wb.sheetnames:
                        ws = wb[sheet_name]
                        rows_html = []
                        for i, row in enumerate(ws.iter_rows(values_only=True)):
                            if all(cell in ('', None) for cell in row):
                                continue
                            rows_html.append(f'<tr>{render_row(row, i)}</tr>')

                        sheets_html.append(
                            f'<h3>{escape(sheet_name)}</h3>'
                            f'<div class="table-wrap"><table>{"".join(rows_html)}</table></div>'
                        )
                finally:
                    wb.close()
        except Exception:
            # Uploaded files are untrusted and the parsers raise many error types
            logger.exception("Preview failed for %s", full_path)
            return render(request, 'filemanager/preview_unavailable.html', status=422)

        html = (
            '<!DOCTYPE html><html><head><meta charset="utf-8"><style>'
            'body{font-family:sans-serif;padding:20px 40px}'
            'h3{margin:24px 0 8px}'
            '.table-wrap{overflow-x:auto}'
            'table{border-collapse:collapse;min-width:100%}'
            'th,td{border:1px solid #d1d5db;padding:6px 12px;white-space:nowrap}'
            'th{background:#f3f4f6;font-weight:600}'
            'tr:nth-child(even) td{background:#f9fafb}'
            '</style></head><body>'
            f'{"".join(sheets_html)}'
            '</body></html>'
        )
        response = HttpResponse(html, content_type='text/html; charset=utf-8')
        response['Content-Security-Policy'] = 'sandbox'
        return response

    content_type, _ = mimetypes.guess_type(full_path)
    if ext in ('.csv', '.tsv', '.log', '.md'):
        content_type = 'text/plain; charset=utf-8'
    elif not content_type:
        content_type = 'application/octet-stream'

    file_size = os.path.getsize(full_path)
    range_match = RANGE_HEADER_RE.fullmatch(request.META.get('HTTP_RANGE', ''))

    if range_match and (range_match.group(1) or range_match.group(2)):
        start_str, end_str = range_match.groups()
        if start_str:
            start = int(start_str)
            end = int(end_str) if end_str else file_size - 1
        else:
            start = max(file_size - int(end_str), 0)
            end = file_size - 1

        if file_size == 0 or start > end or start >= file_size:
            response = HttpResponse(status=416)
            response['Content-Range'] = f'bytes */{file_size}'
            return response

        end = min(end, file_size - 1)
        length = end - start + 1

        f = open(full_path, 'rb')
        f.seek(start)
        response = StreamingHttpResponse(
            _iter_file_chunk(f, length), status=206, content_type=content_type
        )
        response['Content-Range'] = f'bytes {start}-{end}/{file_size}'
        response['Content-Length'] = str(length)
    else:
        response = FileResponse(open(full_path, 'rb'), content_type=content_type)

    response['Accept-Ranges'] = 'bytes'
    if needs_sandbox(content_type):
        response['Content-Security-Policy'] = 'sandbox'
    disposition = content_disposition_header(False, os.path.basename(file_path))
    if disposition:
        response['Content-Disposition'] = disposition

    return response
