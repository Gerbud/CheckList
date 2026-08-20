from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('warranty', '0013_warrantytelegramstatusicon')]

    operations = [
        migrations.AddField(
            model_name='warrantytelegramstatusicon',
            name='custom_emoji_id',
            field=models.CharField(
                blank=True, max_length=128,
                verbose_name='Telegram custom emoji ID',
            ),
        ),
    ]
