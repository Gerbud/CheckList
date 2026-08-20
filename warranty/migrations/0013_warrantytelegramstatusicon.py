from django.db import migrations, models


STATUS_EMOJI = {
    'new': '🆕',
    'service_decision': '❓',
    'in_progress': '🛠',
    'customer_wait': '👤',
    'diagnostics': '🔍',
    'parts_wait': '📦',
    'ready': '✅',
    'closed': '🔒',
}


def seed_status_icons(apps, schema_editor):
    StatusIcon = apps.get_model('warranty', 'WarrantyTelegramStatusIcon')
    StatusIcon.objects.bulk_create([
        StatusIcon(status=status, emoji=emoji)
        for status, emoji in STATUS_EMOJI.items()
    ])


def use_utf8mb4_for_emoji(apps, schema_editor):
    if schema_editor.connection.vendor != 'mysql':
        return
    schema_editor.execute(
        'ALTER TABLE warranty_warrantytelegramstatusicon '
        'MODIFY emoji VARCHAR(32) CHARACTER SET utf8mb4 '
        'COLLATE utf8mb4_unicode_ci NOT NULL'
    )


class Migration(migrations.Migration):
    dependencies = [('warranty', '0012_greenworksdrawing')]

    operations = [
        migrations.CreateModel(
            name='WarrantyTelegramStatusIcon',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('status', models.CharField(choices=[('new', 'Новый'), ('service_decision', 'Ожидает решение СЦ'), ('in_progress', 'В работе'), ('customer_wait', 'Ожидаем клиента'), ('diagnostics', 'Диагностика'), ('parts_wait', 'Ожидаем запчасти'), ('ready', 'Готов к выдаче'), ('closed', 'Закрыт')], max_length=32, unique=True, verbose_name='статус')),
                ('emoji', models.CharField(help_text='Смайлик должен быть доступен среди иконок тем Telegram.', max_length=32, verbose_name='смайлик')),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'verbose_name': 'иконка Telegram для статуса гарантии',
                'verbose_name_plural': 'иконки Telegram для статусов гарантии',
                'ordering': ('status',),
            },
        ),
        migrations.RunPython(use_utf8mb4_for_emoji, migrations.RunPython.noop),
        migrations.RunPython(seed_status_icons, migrations.RunPython.noop),
    ]
