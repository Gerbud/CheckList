from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('warranty', '0005_warrantybitrix_sync')]

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
                    ('error', 'Ошибка'),
                ],
                default='planned',
                max_length=24,
            ),
        ),
    ]
