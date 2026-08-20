from django.db import migrations, models


def copy_original_text(apps, schema_editor):
    Message = apps.get_model('warranty', 'WarrantyTelegramMessage')
    Message.objects.filter(original_text='').update(original_text=models.F('text'))


def use_utf8mb4_for_original_text(apps, schema_editor):
    if schema_editor.connection.vendor != 'mysql':
        return
    schema_editor.execute(
        'ALTER TABLE warranty_warrantytelegrammessage '
        'MODIFY original_text LONGTEXT CHARACTER SET utf8mb4 '
        'COLLATE utf8mb4_unicode_ci NOT NULL'
    )


class Migration(migrations.Migration):
    atomic = False
    dependencies = [('warranty', '0010_warrantytelegrammessage_text_utf8mb4')]
    operations = [
        migrations.AddField(
            model_name='warrantytelegrammessage',
            name='original_text',
            field=models.TextField(blank=True, verbose_name='исходный текст'),
        ),
        migrations.AddField(
            model_name='warrantytelegrammessage',
            name='edited_at',
            field=models.DateTimeField(blank=True, null=True, verbose_name='изменено в Telegram'),
        ),
        migrations.RunPython(use_utf8mb4_for_original_text, migrations.RunPython.noop),
        migrations.RunPython(copy_original_text, migrations.RunPython.noop),
    ]
