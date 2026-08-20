from django.db import migrations, models


def copy_original_text(apps, schema_editor):
    Message = apps.get_model('warranty', 'WarrantyTelegramMessage')
    Message.objects.filter(original_text='').update(original_text=models.F('text'))


class Migration(migrations.Migration):
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
        migrations.RunPython(copy_original_text, migrations.RunPython.noop),
    ]
