from django.contrib import admin
from django.contrib.auth.models import User
from django.contrib.auth.admin import UserAdmin
from django import forms
from django.utils.html import format_html
from django.urls import path
from django.shortcuts import render
from django.http import JsonResponse
import json
import os
from django.conf import settings
from .models import FolderPermission, UserProfile, FileActivity

class FolderPermissionForm(forms.ModelForm):
    class Meta:
        model = FolderPermission
        fields = '__all__'
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # This will be populated by JavaScript

class FolderPermissionInline(admin.TabularInline):
    model = FolderPermission
    form = FolderPermissionForm
    extra = 1
    fields = ['folder_path', 'permission']
    verbose_name = "Folder Access"
    verbose_name_plural = "Folder Access Permissions"
    
    class Media:
        css = {
            'all': ('admin/css/folder-browser.css',)
        }
        js = ('admin/js/folder-browser.js',)

class UserProfileInline(admin.StackedInline):
    model = UserProfile
    can_delete = False
    verbose_name_plural = 'User Profile'

class CustomUserAdmin(UserAdmin):
    inlines = [UserProfileInline, FolderPermissionInline]
    list_display = ['username', 'email', 'first_name', 'last_name', 'is_staff', 'password_changed', 'folder_permissions']
    list_filter = ['is_staff', 'is_superuser', 'is_active', 'userprofile__password_changed']
    
    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path('folder-browser/', self.admin_site.admin_view(self.folder_browser), name='folder_browser'),
            path('get-folders/', self.admin_site.admin_view(self.get_folders), name='get_folders'),
        ]
        return custom_urls + urls
    
    def folder_browser(self, request):
        """Admin view for folder browser"""
        return render(request, 'admin/folder_browser.html')
    
    def get_folders(self, request):
        """API endpoint to get folders"""
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
                    folders.append({
                        'name': item,
                        'path': rel_path,
                        'has_children': any(os.path.isdir(os.path.join(item_path, subitem)) for subitem in os.listdir(item_path))
                    })
        except Exception as e:
            return JsonResponse({'error': str(e)}, status=500)
        
        folders.sort(key=lambda x: x['name'].lower())
        return JsonResponse({'folders': folders})
    
    def password_changed(self, obj):
        return obj.userprofile.password_changed if hasattr(obj, 'userprofile') else False
    password_changed.boolean = True
    password_changed.short_description = 'Password Changed'
    
    def folder_permissions(self, obj):
        perms = FolderPermission.objects.filter(user=obj)
        return ", ".join([f"{perm.folder_path} ({perm.permission})" for perm in perms]) or "No access"
    folder_permissions.short_description = 'Folder Access'

class FolderPermissionAdmin(admin.ModelAdmin):
    list_display = ['user', 'folder_path', 'permission']
    list_filter = ['permission', 'user']
    search_fields = ['user__username', 'folder_path']
    list_editable = ['permission']
    
    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "user":
            kwargs["queryset"] = User.objects.filter(is_superuser=False)
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

class FileActivityAdmin(admin.ModelAdmin):
    list_display = ['user', 'filename', 'activity_type', 'file_size_display', 'timestamp', 'ip_address']
    list_filter = ['activity_type', 'timestamp', 'user']
    search_fields = ['user__username', 'filename', 'filepath']
    readonly_fields = ['user', 'filename', 'filepath', 'activity_type', 'timestamp', 'ip_address', 'file_size']
    
    def file_size_display(self, obj):
        if obj.file_size:
            size = obj.file_size
            for unit in ['B', 'KB', 'MB', 'GB']:
                if size < 1024.0:
                    return f"{size:.2f} {unit}"
                size /= 1024.0
            return f"{size:.2f} TB"
        return "-"
    file_size_display.short_description = 'File Size'

# Quick actions for admin
def grant_full_access(modeladmin, request, queryset):
    for user in queryset:
        FolderPermission.objects.get_or_create(
            user=user,
            folder_path='/',
            defaults={'permission': 'admin'}
        )
grant_full_access.short_description = "Grant full access to root folder"

def grant_read_access(modeladmin, request, queryset):
    for user in queryset:
        FolderPermission.objects.get_or_create(
            user=user,
            folder_path='/',
            defaults={'permission': 'read'}
        )
grant_read_access.short_description = "Grant read access to root folder"

def reset_password_required(modeladmin, request, queryset):
    for user in queryset:
        profile, created = UserProfile.objects.get_or_create(user=user)
        profile.password_changed = False
        profile.save()
reset_password_required.short_description = "Force password change on next login"

CustomUserAdmin.actions = [grant_full_access, grant_read_access, reset_password_required]

admin.site.unregister(User)
admin.site.register(User, CustomUserAdmin)
admin.site.register(FolderPermission, FolderPermissionAdmin)
admin.site.register(FileActivity, FileActivityAdmin)

# Custom admin site header
admin.site.site_header = "SNSeaFile Administration"
admin.site.site_title = "SNC Admin"
admin.site.index_title = "Welcome to SNSeaFile Admin Portal"