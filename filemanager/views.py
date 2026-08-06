import io
import json
import logging
import mimetypes
import os
import re
import shutil
import urllib.parse
import uuid
import zipfile
from datetime import datetime
from pathlib import Path

import mammoth
import openpyxl
import xlrd
import zipstream
from django.conf import settings
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth import REDIRECT_FIELD_NAME, authenticate
from django.contrib.auth import login
from django.contrib.auth import login as auth_login
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import PasswordChangeForm
from django.db import transaction
from django.db.models import Q
from django.http import (
    FileResponse,
    HttpResponse,
    JsonResponse,
    StreamingHttpResponse,
)
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.http import (
    content_disposition_header,
    url_has_allowed_host_and_scheme,
)
from django.utils.text import get_valid_filename
from django.views.decorators.csrf import csrf_exempt
from xlrd.xldate import xldate_as_datetime

from .models import FileActivity, FolderPermission, UploadSession, UserProfile
from .utils import (
    format_file_size,
    get_user_permissions,
    has_permission,
    log_activity,
)

logger = logging.getLogger(__name__)

FILE_TYPE_CATEGORIES = {
    'document': ['.doc', '.docx', '.txt'],
    'pdf': ['.pdf'],
    'spreadsheet': ['.xls', '.xlsx'],
    'presentation': ['.ppt', '.pptx'],
    'image': ['.jpg', '.jpeg', '.png', '.gif', '.svg', '.webp', '.bmp', '.ico'],
    'video': ['.mp4', '.webm', '.avi', '.mov', '.wmv'],
    'audio': ['.mp3', '.wav', '.ogg', '.flac', '.m4a'],
    'zip': ['.zip', '.rar', '.7z', '.tar', '.gz'],
}


def get_file_icon(extension):
    icon_map = {
        '.pdf': '📕',
        '.doc': '📘',
        '.docx': '📘',
        '.xls': '📗',
        '.xlsx': '📗',
        '.ppt': '📙',
        '.pptx': '📙',
        '.txt': '📄',
        '.zip': '📦',
        '.rar': '📦',
        '.7z': '📦',
        '.tar': '📦',
        '.gz': '📦',
        '.jpg': '🖼️',
        '.jpeg': '🖼️',
        '.png': '🖼️',
        '.gif': '🖼️',
        '.svg': '🖼️',
        '.webp': '🖼️',
        '.bmp': '🖼️',
        '.ico': '🖼️',
        '.mp4': '🎬',
        '.webm': '🎬',
        '.avi': '🎬',
        '.mov': '🎬',
        '.wmv': '🎬',
        '.mp3': '🎵',
        '.wav': '🎵',
        '.ogg': '🎵',
        '.flac': '🎵',
        '.m4a': '🎵',
        '.aac': '🎵',
    }
    return icon_map.get(extension, '📄')


def custom_login(request):
    if request.user.is_authenticated:
        return redirect('dashboard')

    if request.method == 'POST':
        username = request.POST.get('username')
        password = request.POST.get('password')
        user = authenticate(request, username=username, password=password)

        if user is not None:
            auth_login(request, user)
            profile, created = UserProfile.objects.get_or_create(user=user)
            if not profile.password_changed:
                return redirect('change_password')
            next_url = request.POST.get(REDIRECT_FIELD_NAME) or request.GET.get(
                REDIRECT_FIELD_NAME
            )
            if next_url and url_has_allowed_host_and_scheme(
                next_url, allowed_hosts={request.get_host()}
            ):
                return redirect(next_url)
            return redirect('dashboard')
        else:
            messages.error(request, 'Invalid username or password')

    return render(
        request,
        'filemanager/login.html',
        {
            REDIRECT_FIELD_NAME: request.GET.get(REDIRECT_FIELD_NAME, ''),
        },
    )


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
                    'path': folder_path,
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
    if not has_permission(request.user, folder_path, 'read'):
        messages.error(request, 'You do not have permission to access this folder')
        return redirect('dashboard')

    base_path = settings.FILE_STORAGE_ROOT
    full_path = os.path.join(base_path, folder_path.lstrip('/'))

    archive_path = Path(settings.ARCHIVE_ROOT)
    full_path_obj = Path(full_path)
    in_archive = full_path_obj == archive_path or full_path_obj.is_relative_to(
        archive_path
    )

    if not os.path.exists(full_path):
        os.makedirs(full_path, exist_ok=True)

    # Get files and folders
    items = []
    total_size = 0
    file_count = 0
    folder_count = 0

    try:
        for item in os.listdir(full_path):
            item_path = os.path.join(full_path, item)
            rel_path = os.path.join(folder_path, item).replace('\\', '/')

            if os.path.isdir(item_path):
                items.append(
                    {
                        'name': item,
                        'type': 'folder',
                        'path': rel_path,
                        'size': f'{len(os.listdir(item_path))} items',
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
    except Exception as e:
        messages.error(request, f'Error accessing folder: {str(e)}')

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
    if not has_permission(request.user, os.path.dirname(file_path), 'read'):
        return JsonResponse({'error': 'Permission denied'}, status=403)

    base_path = settings.FILE_STORAGE_ROOT
    full_path = os.path.join(base_path, file_path.lstrip('/'))

    if os.path.exists(full_path) and os.path.isfile(full_path):
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

    return JsonResponse({'error': 'File not found'}, status=404)


@login_required
@csrf_exempt
def upload_file(request):
    if request.method == 'POST':
        folder_path = request.POST.get('folder_path', '')

        if not has_permission(request.user, folder_path, 'write'):
            return JsonResponse({'error': 'Permission denied'}, status=403)

        base_path = settings.FILE_STORAGE_ROOT
        # Normalize path for Windows
        if folder_path.startswith('/'):
            folder_path = folder_path[1:]
        full_path = os.path.join(base_path, folder_path)

        logger.info("Base path: %s", base_path)
        logger.info("Folder path: %s", folder_path)
        logger.info("Full path: %s", full_path)

        # Ensure directory exists
        os.makedirs(full_path, exist_ok=True)

        files = request.FILES.getlist('files')
        uploaded_files = []
        total_size = 0

        for file in files:
            try:
                filename = get_valid_filename(file.name)
                file_path = os.path.join(full_path, filename)

                logger.info("Saving file: %s", file_path)

                # Check if file already exists
                counter = 1
                name, ext = os.path.splitext(filename)
                while os.path.exists(file_path):
                    filename = f"{name}_{counter}{ext}"
                    file_path = os.path.join(full_path, filename)
                    counter += 1

                # Save file
                logger.info("Original size: %s", file.size)

                written = 0

                with open(file_path, "wb+") as destination:
                    for chunk in file.chunks():
                        destination.write(chunk)
                        written += len(chunk)

                logger.info("Written size: %s", written)
                logger.info("Saved size: %s", os.path.getsize(file_path))

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

                logger.info("Successfully uploaded: %s", filename)

            except Exception as e:
                logger.exception("Upload error for %s", file.name)
                return JsonResponse(
                    {'error': f'Error uploading {file.name}: {str(e)}'}, status=500
                )

        return JsonResponse(
            {
                'success': True,
                'uploaded_files': uploaded_files,
                'total_size': total_size,
                'formatted_total_size': format_file_size(total_size),
            }
        )

    return JsonResponse({'error': 'Invalid request'}, status=400)


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


# @login_required
# def delete_file(request):
#     if request.method == 'POST':
#         data = json.loads(request.body)
#         item_path = data.get('path', '')
#
#         if not has_permission(request.user, os.path.dirname(item_path), 'admin'):
#             return JsonResponse({'error': 'Permission denied'}, status=403)
#
#         base_path = settings.FILE_STORAGE_ROOT
#         full_path = os.path.join(base_path, item_path.lstrip('/'))
#
#         try:
#             if os.path.exists(full_path):
#                 if os.path.isdir(full_path):
#                     # Check if folder is empty
#                     if len(os.listdir(full_path)) > 0:
#                         return JsonResponse(
#                             {'error': 'Folder is not empty'}, status=400
#                         )
#                     os.rmdir(full_path)
#                 else:
#                     file_size = os.path.getsize(full_path)
#                     os.remove(full_path)
#
#                 # Log delete activity
#                 log_activity(
#                     request.user,
#                     os.path.basename(item_path),
#                     item_path,
#                     'delete',
#                     request.META.get('REMOTE_ADDR'),
#                     file_size if not os.path.isdir(full_path) else 0,
#                 )
#
#                 return JsonResponse({'success': True})
#             else:
#                 return JsonResponse({'error': 'File/folder not found'}, status=404)
#         except Exception as e:
#             return JsonResponse({'error': str(e)}, status=500)
#
#     return JsonResponse({'error': 'Invalid request'}, status=400)


@login_required
def delete_file(request):
    if request.method == 'POST':
        data = json.loads(request.body)
        item_path = data.get('path', '')

        if not has_permission(request.user, os.path.dirname(item_path), 'admin'):
            return JsonResponse({'error': 'Permission denied'}, status=403)

        base_path = settings.FILE_STORAGE_ROOT
        full_path = os.path.join(base_path, item_path.lstrip('/'))

        try:
            if not os.path.exists(full_path):
                return JsonResponse({'error': 'File/folder not found'}, status=404)

            file_size = os.path.getsize(full_path) if os.path.isfile(full_path) else 0

            # Build archive destination path, mirroring the original structure
            archive_root = str(settings.ARCHIVE_ROOT)
            archive_dest = os.path.join(archive_root, item_path.lstrip('/'))

            # Avoid overwriting existing archive entries by appending a timestamp
            if os.path.exists(archive_dest):
                name, ext = os.path.splitext(archive_dest)
                timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                archive_dest = f'{name}_{timestamp}{ext}'

            os.makedirs(os.path.dirname(archive_dest), exist_ok=True)
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
        except Exception as e:
            return JsonResponse({'error': str(e)}, status=500)

    return JsonResponse({'error': 'Invalid request'}, status=400)


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
                        'path': '/' + rel_path,
                        'folder': '/' + os.path.dirname(rel_path),
                        'size': stat.st_size,
                        'formatted_size': format_file_size(stat.st_size),
                        'extension': ext,
                        'icon': get_file_icon(ext),
                        'modified': stat.st_mtime,
                        'created': stat.st_ctime,
                    }
                )

    # -------------------------
    # Search within a specific folder
    # -------------------------
    if folder_filter:
        folder = folder_filter.rstrip('/')

        target_path = safe_join(base_path, folder)

        if not target_path:
            return JsonResponse({'error': 'Invalid path'}, status=400)

        can_access = request.user.is_superuser

        if not can_access:
            for perm in user_permissions:
                if perm['permission'] not in ['read', 'write', 'admin']:
                    continue

                perm_path = perm['folder_path'].rstrip('/')

                if folder == perm_path or folder.startswith(perm_path + '/'):
                    can_access = True
                    break

        if not can_access:
            return JsonResponse({'error': 'Permission denied'}, status=403)

        collect_results(target_path)

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
def create_folder(request):
    if request.method == 'POST':
        data = json.loads(request.body)
        folder_path = data.get('folder_path', '')
        folder_name = data.get('folder_name', '')

        if not has_permission(request.user, folder_path, 'write'):
            return JsonResponse({'error': 'Permission denied'}, status=403)

        base_path = settings.FILE_STORAGE_ROOT
        full_path = os.path.join(base_path, folder_path.lstrip('/'), folder_name)

        if os.path.exists(full_path):
            return JsonResponse(
                {'error': 'A folder with that name already exists'}, status=400
            )

        try:
            os.makedirs(full_path)
            return JsonResponse({'success': True})
        except Exception as e:
            return JsonResponse({'error': str(e)}, status=500)

    return JsonResponse({'error': 'Invalid request'}, status=400)


@login_required
def rename_file(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'Invalid request'}, status=400)

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    old_path = data.get('old_path', '')
    new_name = data.get('new_name', '').strip()

    if not old_path:
        return JsonResponse({'error': 'Old path is required'}, status=400)

    if not new_name:
        return JsonResponse({'error': 'New name is required'}, status=400)

    folder = os.path.dirname(old_path)

    if not has_permission(request.user, folder, 'write'):
        return JsonResponse({'error': 'Permission denied'}, status=403)

    new_name = get_valid_filename(new_name)
    new_path = os.path.join(folder, new_name).replace('\\', '/')

    base_path = settings.FILE_STORAGE_ROOT
    full_old_path = os.path.join(base_path, old_path.lstrip('/'))
    full_new_path = os.path.join(base_path, new_path.lstrip('/'))

    if not os.path.exists(full_old_path):
        return JsonResponse({'error': 'File/Folder not found'}, status=404)

    if full_old_path == full_new_path:
        return JsonResponse({'success': True, 'new_path': new_path})

    if os.path.exists(full_new_path):
        return JsonResponse(
            {'error': 'A file/folder with that name already exists'}, status=400
        )

    try:
        with transaction.atomic():
            if os.path.isdir(full_old_path):
                old_folder_path = '/' + old_path.lstrip('/')
                new_folder_path = '/' + new_path.lstrip('/')

                permissions = FolderPermission.objects.select_for_update().filter(
                    folder_path__startswith=old_folder_path
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
            {'success': True, 'new_path': new_path, 'modified': modified_str}
        )

    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


# @login_required
# def bulk_download(request):
#     if request.method != 'POST':
#         return JsonResponse({'error': 'Invalid request'}, status=400)

#     file_paths = request.POST.getlist('paths')

#     if not file_paths:
#         return JsonResponse({'error': 'No files selected'}, status=400)

#     base_path = settings.FILE_STORAGE_ROOT
#     buffer = io.BytesIO()

#     with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_STORED) as zf:
#         for file_path in file_paths:
#             if not has_permission(
#                 request.user, os.path.dirname(file_path.rstrip('/\\')), 'read'
#             ):
#                 continue
#             full_path = os.path.join(base_path, file_path.lstrip('/'))
#             if not os.path.abspath(full_path).startswith(os.path.abspath(base_path)):
#                 continue
#             if os.path.isfile(full_path):
#                 zf.write(full_path, os.path.basename(full_path))
#                 log_activity(
#                     request.user,
#                     os.path.basename(file_path),
#                     file_path,
#                     'download',
#                     request.META.get('REMOTE_ADDR'),
#                     os.path.getsize(full_path),
#                 )
#             elif os.path.isdir(full_path):
#                 parent_path = os.path.dirname(full_path.rstrip('/\\'))
#                 for root, _, files in os.walk(full_path):
#                     for filename in files:
#                         file_full = os.path.join(root, filename)
#                         arcname = os.path.relpath(file_full, parent_path)
#                         zf.write(file_full, arcname)
#                         log_activity(
#                             request.user,
#                             filename,
#                             os.path.relpath(file_full, base_path),
#                             'download',
#                             request.META.get('REMOTE_ADDR'),
#                             os.path.getsize(file_full),
#                         )

#     buffer.seek(0)
#     response = HttpResponse(buffer.read(), content_type='application/zip')
#     response['Content-Disposition'] = 'attachment; filename="download.zip"'
#     return response


@login_required
def bulk_download(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'Invalid request'}, status=400)

    file_paths = request.POST.getlist('paths')

    if not file_paths:
        return JsonResponse({'error': 'No files selected'}, status=400)

    base_path = settings.FILE_STORAGE_ROOT
    base_path_abs = os.path.abspath(base_path)
    zs = zipstream.ZipStream()
    total_size = 0

    for file_path in file_paths:
        if not has_permission(
            request.user, os.path.dirname(file_path.rstrip('/\\')), 'read'
        ):
            continue
        full_path = os.path.join(base_path, file_path.lstrip('/'))
        full_path_abs = os.path.abspath(full_path)
        try:
            within_base = (
                os.path.commonpath([base_path_abs, full_path_abs]) == base_path_abs
            )
        except ValueError:
            within_base = False
        if not within_base:
            continue
        if os.path.isfile(full_path):
            zs.add_path(full_path, arcname=os.path.relpath(full_path, base_path))
            total_size += os.path.getsize(full_path)
        elif os.path.isdir(full_path):
            parent_path = os.path.dirname(full_path.rstrip('/\\'))
            for root, _, files in os.walk(full_path):
                for filename in files:
                    file_full = os.path.join(root, filename)
                    arcname = os.path.relpath(file_full, parent_path)
                    zs.add_path(file_full, arcname=arcname, recurse=False)
                    total_size += os.path.getsize(file_full)

    current_folder = os.path.dirname(file_paths[0].rstrip('/\\')) or '/'

    MAX_NAMES_SHOWN = 5
    names = [os.path.basename(p.rstrip('/\\')) for p in file_paths[:MAX_NAMES_SHOWN]]
    name_summary = ', '.join(names)
    if len(file_paths) > MAX_NAMES_SHOWN:
        name_summary += f', +{len(file_paths) - MAX_NAMES_SHOWN} more'
    name_summary = name_summary[:255]

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


# @login_required
# def file_preview(request, file_path):
#     if not has_permission(request.user, os.path.dirname(file_path), 'read'):
#         return JsonResponse({'error': 'Permission denied'}, status=403)
#
#     base_path = settings.FILE_STORAGE_ROOT
#     full_path = os.path.join(base_path, file_path.lstrip('/'))
#
#     if os.path.exists(full_path) and os.path.isfile(full_path):
#         # Log view activity
#         log_activity(
#             request.user,
#             os.path.basename(file_path),
#             file_path,
#             'view',
#             request.META.get('REMOTE_ADDR'),
#             os.path.getsize(full_path),
#         )
#
#         # For now, just return file info. You can extend this for actual previews
#         file_info = {
#             'name': os.path.basename(file_path),
#             'path': file_path,
#             'size': os.path.getsize(full_path),
#             'formatted_size': format_file_size(os.path.getsize(full_path)),
#             'modified': os.path.getmtime(full_path),
#             'extension': os.path.splitext(file_path)[1].lower(),
#         }
#
#         return JsonResponse({'file': file_info})
#
#     return JsonResponse({'error': 'File not found'}, status=404)


RANGE_HEADER_RE = re.compile(r'bytes=(\d*)-(\d*)')


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
            f'<{tag}>{cell if cell is not None else ""}</{tag}>' for cell in row
        )

    if not has_permission(request.user, os.path.dirname(file_path), 'read'):
        return JsonResponse({'error': 'Permission denied'}, status=403)

    base_path = settings.FILE_STORAGE_ROOT
    full_path = os.path.join(base_path, file_path.lstrip('/'))

    if not os.path.exists(full_path) or not os.path.isfile(full_path):
        return JsonResponse({'error': 'File not found'}, status=404)

    ext = os.path.splitext(full_path)[1].lower()
    if ext == '.docx':
        with open(full_path, 'rb') as f:
            result = mammoth.convert_to_html(f)
        html = (
            '<!DOCTYPE html><html><head><meta charset="utf-8">'
            '<style>body{font-family:sans-serif;max-width:900px;margin:40px auto;padding:0 20px}</style>'
            f'</head><body>{result.value}</body></html>'
        )
        return HttpResponse(html, content_type='text/html; charset=utf-8')

    if ext in ('.xlsx', '.xls'):
        sheets_html = []
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
                    f'<h3>{sheet_name}</h3>'
                    f'<div class="table-wrap"><table>{"".join(rows_html)}</table></div>'
                )
        else:
            wb = openpyxl.load_workbook(full_path, read_only=True, data_only=True)
            for sheet_name in wb.sheetnames:
                ws = wb[sheet_name]
                rows_html = []
                for i, row in enumerate(ws.iter_rows(values_only=True)):
                    if all(cell in ('', None) for cell in row):
                        continue
                    rows_html.append(f'<tr>{render_row(row, i)}</tr>')

                sheets_html.append(
                    f'<h3>{sheet_name}</h3>'
                    f'<div class="table-wrap"><table>{"".join(rows_html)}</table></div>'
                )
            wb.close()

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
        return HttpResponse(html, content_type='text/html; charset=utf-8')

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
    disposition = content_disposition_header(False, os.path.basename(file_path))
    if disposition:
        response['Content-Disposition'] = disposition

    log_activity(
        request.user,
        os.path.basename(file_path),
        file_path,
        'view',
        request.META.get('REMOTE_ADDR'),
        os.path.getsize(full_path),
    )

    return response


@staff_member_required
def admin_folder_browser(request):
    """Admin view to browse folders for permission assignment"""
    base_path = settings.FILE_STORAGE_ROOT
    folder_path = request.GET.get('path', '')

    full_path = os.path.join(base_path, folder_path)

    if not os.path.exists(full_path):
        return JsonResponse({'error': 'Path does not exist'}, status=404)

    folders = []
    try:
        for item in os.listdir(full_path):
            item_path = os.path.join(full_path, item)
            if os.path.isdir(item_path):
                rel_path = os.path.join(folder_path, item).replace('\\', '/')
                folders.append(
                    {
                        'name': item,
                        'path': rel_path,
                        'has_children': any(
                            os.path.isdir(os.path.join(item_path, subitem))
                            for subitem in os.listdir(item_path)
                            if os.path.isdir(os.path.join(item_path, subitem))
                        ),
                    }
                )
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)

    # Sort folders alphabetically
    folders.sort(key=lambda x: x['name'].lower())

    return JsonResponse({'folders': folders})


@staff_member_required
def admin_get_folder_tree(request):
    """Get complete folder tree for admin panel"""
    base_path = settings.FILE_STORAGE_ROOT

    def build_tree(current_path):
        tree = []
        full_path = os.path.join(base_path, current_path.lstrip('/'))

        if not os.path.exists(full_path):
            return tree

        try:
            for item in os.listdir(full_path):
                item_path = os.path.join(full_path, item)
                if os.path.isdir(item_path):
                    rel_path = os.path.join(current_path, item).replace('\\', '/')
                    node = {
                        'name': item,
                        'path': rel_path,
                        'children': build_tree(rel_path),
                    }
                    tree.append(node)
        except Exception:
            logger.exception("Error reading directory %s", full_path)

        return tree

    tree = build_tree('')
    return JsonResponse({'tree': tree})
