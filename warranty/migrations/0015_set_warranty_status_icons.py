from django.db import migrations


STATUS_ICONS = {
    'new': ('📰', '5434144690511290129'),
    'service_decision': ('❓', '5377316857231450742'),
    'in_progress': ('💼', '5348227245599105972'),
    'customer_wait': ('👀', '5357121491508928442'),
    'diagnostics': ('🔎', '5309965701241379366'),
    'parts_wait': ('🛒', '5431492767249342908'),
    'ready': ('✅', '5237699328843200968'),
    'closed': ('🏁', '5408906741125490282'),
}


def set_status_icons(apps, schema_editor):
    StatusIcon = apps.get_model('warranty', 'WarrantyTelegramStatusIcon')
    for status, (emoji, custom_emoji_id) in STATUS_ICONS.items():
        StatusIcon.objects.update_or_create(
            status=status,
            defaults={
                'emoji': emoji,
                'custom_emoji_id': custom_emoji_id,
            },
        )


class Migration(migrations.Migration):
    dependencies = [
        ('warranty', '0014_warrantytelegramstatusicon_custom_emoji_id'),
    ]

    operations = [
        migrations.RunPython(set_status_icons, migrations.RunPython.noop),
    ]
