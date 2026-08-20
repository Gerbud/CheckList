from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('warranty', '0034_webhook_status')]

    operations = [
        migrations.AddField(
            model_name='warrantycustomerupdate',
            name='attempts',
            field=models.PositiveIntegerField(default=1),
        ),
        migrations.AddField(
            model_name='warrantycustomerupdate',
            name='completed_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='warrantycustomerupdate',
            name='last_error',
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name='warrantycustomerupdate',
            name='status',
            field=models.CharField(
                choices=[
                    ('processing', 'Обрабатывается'),
                    ('succeeded', 'Обработано'),
                    ('retry', 'Ожидает повтора'),
                    ('ignored', 'Пропущено'),
                ],
                default='succeeded',
                max_length=16,
            ),
        ),
    ]
