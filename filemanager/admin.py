import posixpath

from django import forms
from django.contrib import admin, messages
from django.contrib.admin import helpers
from django.contrib.auth.admin import UserAdmin
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import transaction
from django.template.response import TemplateResponse

from .models import (
    File,
    FileActivity,
    Folder,
    FolderPermission,
    Signer,
    SigningActivity,
    SigningRequest,
    UserProfile,
)
from .services import filesystem
from .services import signing as signing_service
from .utils import format_file_size, get_storage_path, is_in_archive, normalize_path


def get_folder_choices():
    """Every existing folder, except the archive."""
    paths = [
        path
        for path in filesystem.get_folder_paths().values()
        if not is_in_archive(get_storage_path(path))
    ]
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
            if filesystem.resolve_folder(folder_path) is None:
                raise ValidationError('Folder does not exist')
            if is_in_archive(get_storage_path(folder_path)):
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


class ReadOnlyAdmin(admin.ModelAdmin):
    # View-only: changes go through the site, which keeps names unique,
    # moves permissions on rename and only soft-deletes
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class FolderAdmin(ReadOnlyAdmin):
    list_display = ['name', 'path', 'created_by', 'created_at', 'deleted_at']
    list_select_related = ['parent', 'created_by']
    list_filter = [('deleted_at', admin.EmptyFieldListFilter)]
    search_fields = ['name']

    @admin.display(description='Path')
    def path(self, obj):
        return filesystem.get_path(obj)


class FileAdmin(ReadOnlyAdmin):
    list_display = [
        'name',
        'path',
        'size_display',
        'created_by',
        'created_at',
        'deleted_at',
    ]
    list_select_related = ['folder', 'created_by']
    list_filter = [('deleted_at', admin.EmptyFieldListFilter)]
    search_fields = ['name']
    # 'stored_as' replaces the file field, whose link would point under MEDIA_URL
    fields = [
        'folder',
        'name',
        'stored_as',
        'size',
        'created_by',
        'created_at',
        'updated_at',
        'deleted_by',
        'deleted_at',
    ]
    readonly_fields = ['stored_as']

    @admin.display(description='Path')
    def path(self, obj):
        return posixpath.join(filesystem.get_path(obj.folder), obj.name)

    @admin.display(description='Size')
    def size_display(self, obj):
        return format_file_size(obj.size)

    @admin.display(description='Stored as')
    def stored_as(self, obj):
        return obj.file.name


class SignerAdminForm(forms.ModelForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # A signature already made stays with the person who made it
        if self.instance.status == Signer.Status.SIGNED:
            self.fields['user'].disabled = True


class SignerInline(admin.TabularInline):
    """Only lets an admin replace who signs (e.g. someone who left). Order and
    status are kept by the signing flow, so editing them could break it."""

    model = Signer
    form = SignerAdminForm
    extra = 0
    can_delete = False
    fields = ['signing_order', 'user', 'status', 'signed_at']
    readonly_fields = ['signing_order', 'status', 'signed_at']

    def has_add_permission(self, request, obj=None):
        return False


class SigningActivityInline(admin.TabularInline):
    model = SigningActivity
    extra = 0
    fields = ['created_at', 'actor', 'action', 'detail']
    readonly_fields = fields
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


class SigningRequestAdmin(admin.ModelAdmin):
    """Lets an admin repair a stuck request (e.g. replace a signer who left), or
    reset it to undo the signing entirely."""

    actions = ['reset_signing']

    list_display = ['path', 'created_by', 'status', 'sent_at', 'completed_at']
    list_select_related = ['created_by', 'file__folder']
    list_filter = ['status']
    search_fields = ['file__name', 'created_by__username']
    readonly_fields = [
        'created_by',
        'file',
        'original_file',
        'original_sha',
        'signed_file',
        'signed_sha',
        'created_at',
        'sent_at',
        'completed_at',
    ]
    inlines = [SignerInline, SigningActivityInline]

    @admin.display(description='Path')
    def path(self, obj):
        return posixpath.join(filesystem.get_path(obj.file.folder), obj.file.name)

    @admin.action(
        description='Reset signing (restore the original file and delete the request)',
        permissions=['delete'],
    )
    def reset_signing(self, request, queryset):
        # Restoring overwrites files, so confirm first, as "Delete selected" does
        if not request.POST.get('post'):
            return TemplateResponse(
                request,
                'admin/filemanager/signingrequest/reset_confirmation.html',
                {
                    **self.admin_site.each_context(request),
                    'title': 'Are you sure?',
                    'opts': self.model._meta,
                    'queryset': queryset,
                    'action_checkbox_name': helpers.ACTION_CHECKBOX_NAME,
                },
            )

        reset = 0
        for signing_request in queryset:
            try:
                # log_deletions() must run before the delete; a failed reset
                # rolls the log entry back with it
                with transaction.atomic():
                    self.log_deletions(
                        request, SigningRequest.objects.filter(pk=signing_request.pk)
                    )
                    signing_service.reset(signing_request)
            except ValidationError as e:
                self.message_user(request, e.messages[0], messages.ERROR)
            else:
                reset += 1
        if reset:
            self.message_user(request, f'Reset {reset} signing request(s).')


admin.site.unregister(User)
admin.site.register(User, CustomUserAdmin)
admin.site.register(FolderPermission, FolderPermissionAdmin)
admin.site.register(FileActivity, FileActivityAdmin)
admin.site.register(Folder, FolderAdmin)
admin.site.register(File, FileAdmin)
admin.site.register(SigningRequest, SigningRequestAdmin)

# Custom admin site header
admin.site.site_header = "SNSeaFile Administration"
admin.site.site_title = "SNC Admin"
admin.site.index_title = "Welcome to SNSeaFile Admin Portal"
