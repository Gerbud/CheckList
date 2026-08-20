from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('warranty', '0008_alter_warrantytelegramthread_state'),
    ]

    operations = [
        migrations.AlterField(
            model_name='warrantytelegramsettings',
            name='chat_id',
            field=models.CharField(
                blank=True,
                max_length=64,
                verbose_name='ID чата для Telegram Bot API',
            ),
        ),
        migrations.AlterField(
            model_name='warrantytelegramsettings',
            name='peer_id',
            field=models.CharField(
                blank=True,
                max_length=64,
                verbose_name='ID Telegram-группы',
            ),
        ),
    ]
