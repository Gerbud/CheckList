from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('warranty', '0029_warrantycustomerbotsettings_yandex_review_url')]

    operations = [
        migrations.AddField(
            model_name='warrantytelegramthread',
            name='deleted_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='warrantytelegramthread',
            name='deleted_topic_ids',
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AlterField(
            model_name='warrantytelegramthread',
            name='state',
            field=models.CharField(choices=[
                ('planned', 'Ожидает создания'),
                ('creating', 'Создаётся'),
                ('active', 'Активна'),
                ('close_pending', 'Ожидает закрытия'),
                ('archived', 'Архивирована'),
                ('deleted', 'Удалена из Telegram'),
                ('restore_pending', 'Ожидает восстановления'),
                ('status_update_pending', 'Ожидает обновления статуса'),
                ('error', 'Ошибка'),
            ], default='planned', max_length=24),
        ),
    ]
