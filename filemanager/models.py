from django.db import models
from django.contrib.auth.models import User
from django.conf import settings
import os

class FolderPermission(models.Model):
    PERMISSION_CHOICES = [
        ('read', 'Read Only'),
        ('write', 'Read and Write'),
        ('admin', 'Full Access'),
    ]
    
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    folder_path = models.CharField(max_length=1000)
    permission = models.CharField(max_length=10, choices=PERMISSION_CHOICES)
    
    class Meta:
        unique_together = ['user', 'folder_path']

class UserProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    password_changed = models.BooleanField(default=False)
    vessel_name = models.CharField(max_length=100, blank=True)
    
    def __str__(self):
        return f"{self.user.username} - {self.vessel_name}"

class FileActivity(models.Model):
    ACTIVITY_CHOICES = [
        ('upload', 'File Upload'),
        ('download', 'File Download'),
        ('delete', 'File Delete'),
        ('view', 'File View'),
    ]
    
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    filename = models.CharField(max_length=255)
    filepath = models.CharField(max_length=1000)
    activity_type = models.CharField(max_length=10, choices=ACTIVITY_CHOICES)
    timestamp = models.DateTimeField(auto_now_add=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    file_size = models.BigIntegerField(null=True, blank=True)
    
    def __str__(self):
        return f"{self.user.username} - {self.activity_type} - {self.filename}"

class UploadSession(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    session_id = models.CharField(max_length=100)
    folder_path = models.CharField(max_length=1000)
    total_files = models.IntegerField(default=0)
    completed_files = models.IntegerField(default=0)
    total_size = models.BigIntegerField(default=0)
    uploaded_size = models.BigIntegerField(default=0)
    status = models.CharField(max_length=20, default='uploading')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)