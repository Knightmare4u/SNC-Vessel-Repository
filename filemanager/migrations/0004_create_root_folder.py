from django.db import migrations


def create_root_folder(apps, schema_editor):
    Folder = apps.get_model("filemanager", "Folder")
    Folder.objects.create(name="")


def delete_root_folder(apps, schema_editor):
    # Undoing 0003 drops these tables anyway, so empty them first; files on
    # disk are left alone
    File = apps.get_model("filemanager", "File")
    Folder = apps.get_model("filemanager", "Folder")
    File.objects.all().delete()
    Folder.objects.update(parent=None)  # parent is PROTECT
    Folder.objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ("filemanager", "0003_folder_file_folder_unique_active_folder_name_and_more"),
    ]

    operations = [
        migrations.RunPython(create_root_folder, delete_root_folder),
    ]
