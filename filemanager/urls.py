from django.urls import path
from django.contrib.auth import views as auth_views
from . import views

urlpatterns = [
    path('', views.custom_login, name='login'),
    path('logout/', auth_views.LogoutView.as_view(template_name='filemanager/logout.html'), name='logout'),
    path('change-password/', views.change_password, name='change_password'),
    path('dashboard/', views.dashboard, name='dashboard'),
    path('browser/', views.file_browser, name='file_browser'),
    path('browser/<path:folder_path>/', views.file_browser, name='file_browser'),
    path('download/<path:file_path>/', views.download_file, name='download_file'),
    path('preview/<path:file_path>/', views.file_preview, name='file_preview'),
    path('upload/', views.upload_file, name='upload_file'),
    path('upload/progress/<str:session_id>/', views.get_upload_progress, name='upload_progress'),
    path('delete/', views.delete_file, name='delete_file'),
    path('search/', views.search_files, name='search_files'),
    path('create-folder/', views.create_folder, name='create_folder'),
    # Admin folder browser
    path('admin/folder-browser/', views.admin_folder_browser, name='admin_folder_browser'),
    path('admin/get-folders/', views.admin_get_folder_tree, name='admin_get_folders'),
]