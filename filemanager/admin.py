import logging
import os

from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from django.contrib.auth.models import User
from django.http import JsonResponse
from django.shortcuts import render
from django.urls import path

from .models import FileActivity, FolderPermission, UserProfile
from .utils import format_file_size, get_storage_path

logger = logging.getLogger(__name__)


class FolderPermissionInline(admin.TabularInline):
    model = FolderPermission
    extra = 1
    fields = ['folder_path', 'permission']
    verbose_name = "Folder Access"
    verbose_name_plural = "Folder Access Permissions"

    class Media:
        css = {'all': ('admin/css/folder-browser.css',)}
        js = ('admin/js/folder-browser.js',)


class UserProfileInline(admin.StackedInline):
    model = UserProfile
    can_delete = False
    verbose_name_plural = 'User Profile'


class CustomUserAdmin(UserAdmin):
    inlines = [UserProfileInline, FolderPermissionInline]
    list_display = [
        'username',
        'email',
        'first_name',
        'last_name',
        'is_staff',
        'password_changed',
        'folder_permissions',
    ]
    list_filter = [
        'is_staff',
        'is_superuser',
        'is_active',
        'userprofile__password_changed',
    ]
    actions = ['grant_full_access', 'grant_read_access', 'reset_password_required']

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .select_related('userprofile')
            .prefetch_related('folderpermission_set')
        )

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                'folder-browser/',
                self.admin_site.admin_view(self.folder_browser),
                name='folder_browser',
            ),
            path(
                'get-folders/',
                self.admin_site.admin_view(self.get_folders),
                name='get_folders',
            ),
        ]
        return custom_urls + urls

    def folder_browser(self, request):
        """Admin view for folder browser"""
        return render(request, 'admin/folder_browser.html')

    def get_folders(self, request):
        """API endpoint to get folders"""
        folder_path = request.GET.get('path', '')

        full_path = get_storage_path(folder_path)

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
                            ),
                        }
                    )
        except OSError:
            logger.exception("Error reading folders in %s", full_path)
            return JsonResponse({'error': 'Could not read folders'}, status=500)

        folders.sort(key=lambda x: x['name'].lower())
        return JsonResponse({'folders': folders})

    @admin.display(boolean=True, description='Password Changed')
    def password_changed(self, obj):
        return (
            obj.userprofile.password_changed if hasattr(obj, 'userprofile') else False
        )

    @admin.display(description='Folder Access')
    def folder_permissions(self, obj):
        perms = obj.folderpermission_set.all()
        return (
            ", ".join([f"{perm.folder_path} ({perm.permission})" for perm in perms])
            or "No access"
        )

    @admin.action(description="Grant full access to root folder")
    def grant_full_access(self, request, queryset):
        for user in queryset:
            FolderPermission.objects.get_or_create(
                user=user, folder_path='/', defaults={'permission': 'admin'}
            )

    @admin.action(description="Grant read access to root folder")
    def grant_read_access(self, request, queryset):
        for user in queryset:
            FolderPermission.objects.get_or_create(
                user=user, folder_path='/', defaults={'permission': 'read'}
            )

    @admin.action(description="Force password change on next login")
    def reset_password_required(self, request, queryset):
        for user in queryset:
            profile, created = UserProfile.objects.get_or_create(user=user)
            profile.password_changed = False
            profile.save()


class FolderPermissionAdmin(admin.ModelAdmin):
    list_display = ['user', 'folder_path', 'permission']
    list_select_related = ['user']
    list_filter = ['permission', 'user']
    search_fields = ['user__username', 'folder_path']
    list_editable = ['permission']

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "user":
            kwargs["queryset"] = User.objects.filter(is_superuser=False)
        return super().formfield_for_foreignkey(db_field, request, **kwargs)


class FileActivityAdmin(admin.ModelAdmin):
    list_display = [
        'user',
        'filename',
        'activity_type',
        'file_size_display',
        'timestamp',
        'ip_address',
    ]
    list_select_related = ['user']
    list_filter = ['activity_type', 'timestamp', 'user']
    search_fields = ['user__username', 'filename', 'filepath']
    readonly_fields = [
        'user',
        'filename',
        'filepath',
        'activity_type',
        'timestamp',
        'ip_address',
        'file_size',
    ]

    @admin.display(description='File Size')
    def file_size_display(self, obj):
        return format_file_size(obj.file_size) if obj.file_size else "-"


admin.site.unregister(User)
admin.site.register(User, CustomUserAdmin)
admin.site.register(FolderPermission, FolderPermissionAdmin)
admin.site.register(FileActivity, FileActivityAdmin)

# Custom admin site header
admin.site.site_header = "SNSeaFile Administration"
admin.site.site_title = "SNC Admin"
admin.site.index_title = "Welcome to SNSeaFile Admin Portal"
