import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'oakmaritime.settings')
django.setup()

from django.contrib.auth.models import User
from filemanager.models import UserProfile, FolderPermission

users_data = [
    ('SNC', 'taipei88'),
    ('VBS', '174977'),
    ('VCN', '175538'),
    ('VCS', '175538'),
    ('VHNA', '176389'),
    ('VMK', '177921'),
    ('VONA', '081594'),
    ('VPNA', '081676'),
    ('VQNA', '082082'),
    ('VRK', '082113'),
    ('VSRA', '810820'),
    ('VTN', '250327'),
    ('VTPO', '250342'),
    ('VTS', '176469'),
    ('VWS', '179565'),
    ('VYS', '177798'),
]

for username, password in users_data:
    if not User.objects.filter(username=username).exists():
        user = User.objects.create_user(username=username, password=password)
        UserProfile.objects.create(user=user, vessel_name=username)
        
        # Give read access to root folder by default
        FolderPermission.objects.create(
            user=user,
            folder_path='/',
            permission='read'
        )
        
        print(f'Created user: {username}')

# Create superuser (you'll need to set password manually)
if not User.objects.filter(username='admin').exists():
    User.objects.create_superuser('admin', 'admin@oakmaritime.com', 'ChangeThisPassword123!')
    print('Created superuser: admin')
    print('IMPORTANT: Change the admin password immediately!')