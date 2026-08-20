from django.db import migrations


def seed_settings(apps, schema_editor):
    Settings = apps.get_model('warranty', 'WarrantyTelegramSettings')
    Settings.objects.update_or_create(
        pk=1,
        defaults={
            'peer_id': '3894555747',
            'chat_id': '-1003894555747',
            'use_forum_topics': True,
            'is_enabled': True,
        },
    )


class Migration(migrations.Migration):
    dependencies = [('warranty', '0002_warrantytelegramsettings')]
    operations = [migrations.RunPython(seed_settings, migrations.RunPython.noop)]
