from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('checklists', '0017_telegram_bot_commands'),
    ]

    operations = [
        migrations.AddField(
            model_name='telegramupdatelog',
            name='command',
            field=models.CharField(blank=True, max_length=64, verbose_name='команда'),
        ),
        migrations.AddField(
            model_name='telegramupdatelog',
            name='responded_at',
            field=models.DateTimeField(blank=True, null=True, verbose_name='ответ отправлен в'),
        ),
        migrations.AddField(
            model_name='telegramupdatelog',
            name='response_error',
            field=models.TextField(blank=True, verbose_name='ошибка ответа'),
        ),
        migrations.AddField(
            model_name='telegramupdatelog',
            name='response_status',
            field=models.CharField(
                blank=True,
                choices=[
                    ('sent', 'Отправлен'),
                    ('queued', 'Поставлен в очередь'),
                    ('failed', 'Ошибка'),
                    ('background', 'Фоновая обработка'),
                    ('ignored', 'Игнорирован'),
                ],
                max_length=16,
                verbose_name='статус ответа',
            ),
        ),
    ]
