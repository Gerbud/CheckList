from django.db import migrations, models


def queue_existing_active_topics(apps, schema_editor):
    Thread = apps.get_model('warranty', 'WarrantyTelegramThread')
    Thread.objects.filter(state='active').exclude(topic_id='').update(
        state='status_update_pending',
    )


class Migration(migrations.Migration):
    dependencies = [('warranty', '0007_warrantytelegramstatusbutton')]

    operations = [
        migrations.AlterField(
            model_name='warrantytelegramthread',
            name='state',
            field=models.CharField(
                choices=[
                    ('planned', 'Ожидает создания'),
                    ('creating', 'Создаётся'),
                    ('active', 'Активна'),
                    ('close_pending', 'Ожидает закрытия'),
                    ('archived', 'Архивирована'),
                    ('restore_pending', 'Ожидает восстановления'),
                    ('status_update_pending', 'Ожидает обновления статуса'),
                    ('error', 'Ошибка'),
                ],
                default='planned',
                max_length=24,
            ),
        ),
        migrations.RunPython(queue_existing_active_topics, migrations.RunPython.noop),
    ]
