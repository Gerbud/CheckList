from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('warranty', '0033_warrantyactivity')]

    operations = [
        migrations.AddField(
            model_name='warrantycustomerbotsettings',
            name='webhook_checked_at',
            field=models.DateTimeField(blank=True, null=True, verbose_name='webhook проверен'),
        ),
        migrations.AddField(
            model_name='warrantycustomerbotsettings',
            name='webhook_pending_updates',
            field=models.PositiveIntegerField(default=0, verbose_name='обновлений в очереди'),
        ),
    ]
