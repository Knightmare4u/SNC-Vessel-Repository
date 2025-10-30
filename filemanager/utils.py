import os
from django.conf import settings
from .models import FolderPermission, FileActivity

def get_user_permissions(user):
    if user.is_superuser:
        return [{'folder_path': '/', 'permission': 'admin'}]
    
    permissions = FolderPermission.objects.filter(user=user)
    return [{'folder_path': perm.folder_path, 'permission': perm.permission} for perm in permissions]

def has_permission(user, folder_path, required_permission):
    if user.is_superuser:
        return True
    
    permissions = get_user_permissions(user)
    
    for perm in permissions:
        if folder_path.startswith(perm['folder_path']) or perm['folder_path'] == '/':
            if required_permission == 'read':
                return perm['permission'] in ['read', 'write', 'admin']
            elif required_permission == 'write':
                return perm['permission'] in ['write', 'admin']
            elif required_permission == 'admin':
                return perm['permission'] == 'admin'
    
    return False

def log_activity(user, filename, filepath, activity_type, ip_address, file_size=None):
    FileActivity.objects.create(
        user=user,
        filename=filename,
        filepath=filepath,
        activity_type=activity_type,
        ip_address=ip_address,
        file_size=file_size
    )

def format_file_size(size_bytes):
    if size_bytes == 0:
        return "0 B"
    
    size_names = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    while size_bytes >= 1024 and i < len(size_names) - 1:
        size_bytes /= 1024.0
        i += 1
    
    return f"{size_bytes:.2f} {size_names[i]}"

def get_folder_size(folder_path):
    total_size = 0
    for dirpath, dirnames, filenames in os.walk(folder_path):
        for filename in filenames:
            filepath = os.path.join(dirpath, filename)
            total_size += os.path.getsize(filepath)
    return total_size