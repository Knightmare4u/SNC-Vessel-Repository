import os

from django import forms
from django.conf import settings
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError

from .models import FileActivity, FolderPermission, UserProfile
from .utils import format_file_size, get_storage_path, is_in_archive, normalize_path


def get_folder_choices():
    """Every existing folder under the storage root, except the archive."""
    root = os.path.abspath(settings.FILE_STORAGE_ROOT)
    paths = ['/']
    for dirpath, dirnames, _ in os.walk(root):
        # Pruning dirnames in place also stops os.walk descending into them
        dirnames[:] = [
            name
            for name in dirnames
            if not is_in_archive(os.path.join(dirpath, name))
        ]
        for name in dirnames:
            rel = os.path.relpath(os.path.join(dirpath, name), root)
            paths.append(normalize_path(rel))
    return sorted(paths, key=str.lower)


class FolderPermissionForm(forms.ModelForm):
    """Folder path is picked from a searchable list of existing folders."""

    class Meta:
        model = FolderPermission
        fields = ['user', 'folder_path', 'permission']

    class Media:
        # Same select2 assets Django's admin autocomplete uses, in the same order
        css = {
            'screen': (
                'admin/css/vendor/select2/select2.css',
                'admin/css/autocomplete.css',
            )
        }
        js = (
            'admin/js/vendor/jquery/jquery.js',
            'admin/js/vendor/select2/select2.full.js',
            'admin/js/jquery.init.js',
            'js/folder_select.js',
        )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        choices = get_folder_choices()
        current = self.initial.get('folder_path')
        if current and current not in choices:
            # Keep an existing (now missing) folder visible so the row still saves
            choices.append(current)
        self.fields['folder_path'].widget = forms.Select(
            choices=[('', '---------')] + [(p, p) for p in choices],
            attrs={'class': 'folder-select'},
        )

    def clean_folder_path(self):
        folder_path = normalize_path(self.cleaned_data['folder_path'])
        if 'folder_path' in self.changed_data:
            full_path = get_storage_path(folder_path)
            if not os.path.isdir(full_path):
                raise ValidationError('Folder does not exist')
            if is_in_archive(full_path):
                raise ValidationError('Archive folders cannot be selected')
        return folder_path


class FolderPermissionInline(admin.TabularInline):
    model = FolderPermission
    form = FolderPermissionForm
    extra = 1
    fields = ['folder_path', 'permission']
    verbose_name = "Folder Access"
    verbose_name_plural = "Folder Access Permissions"


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
        count = 0
        for user in queryset:
            # update_or_create: also upgrades an existing read/write root grant
            FolderPermission.objects.update_or_create(
                user=user, folder_path='/', defaults={'permission': 'admin'}
            )
            count += 1
        self.message_user(request, f'Granted full access to {count} user(s).')

    @admin.action(description="Grant read access to root folder")
    def grant_read_access(self, request, queryset):
        granted = skipped = 0
        for user in queryset:
            # get_or_create: a grant never downgrades an existing higher one
            _, created = FolderPermission.objects.get_or_create(
                user=user, folder_path='/', defaults={'permission': 'read'}
            )
            if created:
                granted += 1
            else:
                skipped += 1
        message = f'Granted read access to {granted} user(s).'
        if skipped:
            message += f' {skipped} already had root access and were left unchanged.'
        self.message_user(request, message)

    # Takes effect on the user's next page load, not only at their next login
    @admin.action(description="Force password change")
    def reset_password_required(self, request, queryset):
        count = 0
        for user in queryset:
            profile, created = UserProfile.objects.get_or_create(user=user)
            profile.password_changed = False
            profile.save()
            count += 1
        self.message_user(request, f'{count} user(s) must change their password.')


class FolderPermissionAdmin(admin.ModelAdmin):
    form = FolderPermissionForm
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

    def has_add_permission(self, request):
        # Records are written by the app; a hand-made one can't even be saved
        return False

    def has_change_permission(self, request, obj=None):
        # View-only: no Save buttons on an audit record
        return False

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
