import io
import json
import mimetypes
import os
import shutil
import tempfile
import urllib.parse
import uuid
import zipfile
from datetime import datetime
from wsgiref.util import FileWrapper

import mammoth
import pythoncom
import win32com.client
from django.conf import settings
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth import REDIRECT_FIELD_NAME, authenticate
from django.contrib.auth import login
from django.contrib.auth import login as auth_login
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import PasswordChangeForm
from django.db.models import Q
from django.http import HttpResponse, JsonResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.text import get_valid_filename
from django.views.decorators.csrf import csrf_exempt
from docx2pdf import convert

from .models import FileActivity, FolderPermission, UploadSession, UserProfile
from .utils import (
    ZipStream,
    format_file_size,
    get_user_permissions,
    has_permission,
    log_activity,
)


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
                        'size': '-',
                        'size_bytes': 0,
                        'modified': datetime.fromtimestamp(os.path.getmtime(item_path)),
                        'created_at': datetime.fromtimestamp(
                            os.path.getctime(item_path)
                        ),
                        'icon': '📁',
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
        'total_size': format_file_size(total_size),
        'file_count': file_count,
        'folder_count': folder_count,
    }
    return render(request, 'filemanager/file_browser.html', context)


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
        '.jpg': '🖼️',
        '.jpeg': '🖼️',
        '.png': '🖼️',
        '.gif': '🖼️',
        '.mp4': '🎬',
        '.avi': '🎬',
        '.mov': '🎬',
        '.mp3': '🎵',
        '.wav': '🎵',
    }
    return icon_map.get(extension, '📄')


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

        wrapper = FileWrapper(open(full_path, 'rb'))
        content_type, encoding = mimetypes.guess_type(full_path)
        response = HttpResponse(wrapper, content_type=content_type)
        response['Content-Disposition'] = (
            f'attachment; filename="{os.path.basename(file_path)}"'
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

        print(f"DEBUG: Base path: {base_path}")
        print(f"DEBUG: Folder path: {folder_path}")
        print(f"DEBUG: Full path: {full_path}")

        # Ensure directory exists
        os.makedirs(full_path, exist_ok=True)

        files = request.FILES.getlist('files')
        uploaded_files = []
        total_size = 0

        for file in files:
            try:
                filename = get_valid_filename(file.name)
                file_path = os.path.join(full_path, filename)

                print(f"DEBUG: Saving file: {file_path}")

                # Check if file already exists
                counter = 1
                name, ext = os.path.splitext(filename)
                while os.path.exists(file_path):
                    filename = f"{name}_{counter}{ext}"
                    file_path = os.path.join(full_path, filename)
                    counter += 1

                # Save file
                with open(file_path, 'wb+') as destination:
                    for chunk in file.chunks():
                        destination.write(chunk)

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

                print(f"DEBUG: Successfully uploaded: {filename}")

            except Exception as e:
                print(f"DEBUG: Upload error: {str(e)}")
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
    query = request.GET.get('q', '')
    file_type = request.GET.get('type', '')

    if not query:
        return JsonResponse({'results': []})

    base_path = settings.FILE_STORAGE_ROOT
    results = []

    # Search in all accessible folders
    user_permissions = get_user_permissions(request.user)

    for perm in user_permissions:
        if perm['permission'] in ['read', 'write', 'admin']:
            folder_path = perm['folder_path']
            full_search_path = os.path.join(base_path, folder_path.lstrip('/'))

            if os.path.exists(full_search_path):
                for root, dirs, files in os.walk(full_search_path):
                    for file in files:
                        if query.lower() in file.lower():
                            file_path = os.path.join(root, file)
                            rel_path = os.path.relpath(file_path, base_path).replace(
                                '\\', '/'
                            )

                            file_ext = os.path.splitext(file)[1].lower()

                            # Filter by file type if specified
                            if file_type and file_ext != f'.{file_type}':
                                continue

                            results.append(
                                {
                                    'name': file,
                                    'path': rel_path,
                                    'folder': os.path.dirname(rel_path),
                                    'size': os.path.getsize(file_path),
                                    'formatted_size': format_file_size(
                                        os.path.getsize(file_path)
                                    ),
                                    'extension': file_ext,
                                    'icon': get_file_icon(file_ext),
                                    'modified': os.path.getmtime(file_path),
                                    'created': os.path.getctime(file_path),
                                }
                            )

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

        try:
            os.makedirs(full_path, exist_ok=True)
            return JsonResponse({'success': True})
        except Exception as e:
            return JsonResponse({'error': str(e)}, status=500)

    return JsonResponse({'error': 'Invalid request'}, status=400)


@login_required
def rename_file(request):
    if request.method == 'POST':
        data = json.loads(request.body)
        old_path = data.get('old_path', '')
        new_name = data.get('new_name', '').strip()

        if not new_name:
            return JsonResponse({'error': 'New name is required'}, status=400)

        folder = os.path.dirname(old_path)
        if not has_permission(request.user, folder, 'write'):
            return JsonResponse({'error': 'Permission denied'}, status=403)

        new_name = get_valid_filename(new_name)
        base_path = settings.FILE_STORAGE_ROOT
        full_old = os.path.join(base_path, old_path.lstrip('/'))
        full_new = os.path.join(base_path, folder.lstrip('/'), new_name)

        if not os.path.exists(full_old):
            return JsonResponse({'error': 'File not found'}, status=404)

        if full_old != full_new and os.path.exists(full_new):
            return JsonResponse(
                {'error': 'A file with that name already exists'}, status=400
            )

        if full_old == full_new:
            return JsonResponse(
                {
                    'success': True,
                    'new_path': os.path.join(folder, new_name).replace('\\', '/'),
                }
            )

        try:
            os.rename(full_old, full_new)
            os.utime(full_new, None)
            modified_ts = os.path.getmtime(full_new)
            modified_str = datetime.fromtimestamp(modified_ts).strftime(
                '%b %d, %Y %H:%M'
            )
            new_path = os.path.join(folder, new_name).replace('\\', '/')
            log_activity(
                request.user,
                f"{os.path.basename(old_path)} → {new_name}",
                new_path,
                'rename',
                request.META.get('REMOTE_ADDR'),
            )
            return JsonResponse(
                {'success': True, 'new_path': new_path, 'modified': modified_str}
            )
        except Exception as e:
            return JsonResponse({'error': str(e)}, status=500)

    return JsonResponse({'error': 'Invalid request'}, status=400)


@login_required
def bulk_download(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'Invalid request'}, status=400)

    file_paths = request.POST.getlist('paths')

    if not file_paths:
        return JsonResponse({'error': 'No files selected'}, status=400)

    base_path = settings.FILE_STORAGE_ROOT
    buffer = io.BytesIO()

    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_STORED) as zf:
        for file_path in file_paths:
            if not has_permission(request.user, os.path.dirname(file_path), 'read'):
                continue
            full_path = os.path.join(base_path, file_path.lstrip('/'))
            if not os.path.abspath(full_path).startswith(os.path.abspath(base_path)):
                continue
            if os.path.exists(full_path) and os.path.isfile(full_path):
                zf.write(full_path, os.path.basename(full_path))
                log_activity(
                    request.user,
                    os.path.basename(file_path),
                    file_path,
                    'download',
                    request.META.get('REMOTE_ADDR'),
                    os.path.getsize(full_path),
                )

    buffer.seek(0)
    response = HttpResponse(buffer.read(), content_type='application/zip')
    response['Content-Disposition'] = 'attachment; filename="download.zip"'
    return response


# @login_required
# def bulk_download(request):
#     if request.method != 'POST':
#         return JsonResponse({'error': 'Invalid request'}, status=400)

#     file_paths = request.POST.getlist('paths')
#     if not file_paths:
#         return JsonResponse({'error': 'No files selected'}, status=400)

#     base_path = settings.FILE_STORAGE_ROOT
#     valid_files = []
#     for file_path in file_paths:
#         if not has_permission(request.user, os.path.dirname(file_path), 'read'):
#             continue
#         full_path = os.path.join(base_path, file_path.lstrip('/'))
#         if not os.path.abspath(full_path).startswith(os.path.abspath(base_path)):
#             continue
#         if os.path.exists(full_path) and os.path.isfile(full_path):
#             valid_files.append((file_path, full_path))

#     if not valid_files:
#         return JsonResponse({'error': 'No accessible files'}, status=404)

#     for file_path, full_path in valid_files:
#         log_activity(
#             request.user,
#             os.path.basename(file_path),
#             file_path,
#             'download',
#             request.META.get('REMOTE_ADDR'),
#             os.path.getsize(full_path),
#         )

#     files_to_zip = [
#         (os.path.basename(file_path), full_path) for file_path, full_path in valid_files
#     ]
#     response = StreamingHttpResponse(
#         ZipStream(files_to_zip), content_type='application/zip'
#     )
#     response['Content-Disposition'] = 'attachment; filename="download.zip"'
#     return response


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


@login_required
def file_preview(request, file_path):
    if not has_permission(request.user, os.path.dirname(file_path), 'read'):
        return JsonResponse({'error': 'Permission denied'}, status=403)

    base_path = settings.FILE_STORAGE_ROOT
    full_path = os.path.join(base_path, file_path.lstrip('/'))

    if not os.path.exists(full_path) or not os.path.isfile(full_path):
        return JsonResponse({'error': 'File not found'}, status=404)

    log_activity(
        request.user,
        os.path.basename(file_path),
        file_path,
        'view',
        request.META.get('REMOTE_ADDR'),
        os.path.getsize(full_path),
    )

    ext = os.path.splitext(full_path)[1].lower()
    if ext in ('.docx', '.doc'):
        # tmp_path = None
        # pythoncom.CoInitialize()
        # try:
        #     with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as tmp:
        #         tmp_path = tmp.name
        #     convert(full_path, tmp_path)
        # except Exception:
        #     if tmp_path and os.path.exists(tmp_path):
        #         os.unlink(tmp_path)
        #     return JsonResponse(
        #         {'error': 'Word preview unavailable: Microsoft Word is not installed on this server.'},
        #         status=503,
        #     )
        # finally:
        #     pythoncom.CoUninitialize()
        # with open(tmp_path, 'rb') as f:
        #     pdf_bytes = f.read()
        # os.unlink(tmp_path)
        # stem = os.path.splitext(os.path.basename(file_path))[0]
        # response = HttpResponse(pdf_bytes, content_type='application/pdf')
        # response['Content-Disposition'] = f'inline; filename="{stem}.pdf"'
        # return response
        with open(full_path, 'rb') as f:
            result = mammoth.convert_to_html(f)
        html = (
            '<!DOCTYPE html><html><head><meta charset="utf-8">'
            '<style>body{font-family:sans-serif;max-width:900px;margin:40px auto;padding:0 20px}</style>'
            f'</head><body>{result.value}</body></html>'
        )
        return HttpResponse(html, content_type='text/html; charset=utf-8')

    # if ext in ('.pptx', '.ppt'):
    #     tmp_path = None
    #     pythoncom.CoInitialize()
    #     try:
    #         with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as tmp:
    #             tmp_path = tmp.name
    #         ppt = win32com.client.Dispatch('PowerPoint.Application')
    #         try:
    #             deck = ppt.Presentations.Open(
    #                 full_path, ReadOnly=True, Untitled=False, WithWindow=False
    #             )
    #             deck.SaveAs(tmp_path, 32)  # 32 = ppSaveAsPDF
    #             deck.Close()
    #         finally:
    #             ppt.Quit()
    #     except Exception:
    #         if tmp_path and os.path.exists(tmp_path):
    #             os.unlink(tmp_path)
    #         return JsonResponse(
    #             {
    #                 'error': 'PowerPoint preview unavailable: Microsoft PowerPoint is not installed on this server.'
    #             },
    #             status=503,
    #         )
    #     finally:
    #         pythoncom.CoUninitialize()
    #     with open(tmp_path, 'rb') as f:
    #         pdf_bytes = f.read()
    #     os.unlink(tmp_path)
    #     stem = os.path.splitext(os.path.basename(file_path))[0]
    #     response = HttpResponse(pdf_bytes, content_type='application/pdf')
    #     response['Content-Disposition'] = f'inline; filename="{stem}.pdf"'
    #     return response

    content_type, _ = mimetypes.guess_type(full_path)
    if ext in ('.csv', '.tsv', '.log', '.md'):
        content_type = 'text/plain; charset=utf-8'
    elif not content_type:
        content_type = 'application/octet-stream'

    wrapper = FileWrapper(open(full_path, 'rb'))
    response = HttpResponse(wrapper, content_type=content_type)
    response['Content-Disposition'] = (
        f'inline; filename="{os.path.basename(file_path)}"'
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
        except Exception as e:
            print(f"Error reading directory {full_path}: {e}")

        return tree

    tree = build_tree('')
    return JsonResponse({'tree': tree})
