from django.db import migrations, models


OLD = 'Здравствуйте! Я помогу оформить гарантийное обращение. Пришлите фото этикетки изделия.'
NEW = (
    'Здравствуйте! Я помогу подобрать товар Greenworks, отвечу на вопросы по его использованию, '
    'активирую электронную гарантию или помогу оформить рекламацию.'
)


def update_existing_welcome(apps, schema_editor):
    Settings = apps.get_model('warranty', 'WarrantyCustomerBotSettings')
    Settings.objects.filter(welcome_text=OLD).update(welcome_text=NEW)


class Migration(migrations.Migration):
    dependencies = [('warranty', '0027_move_privacy_policy_link')]

    operations = [
        migrations.AlterField(
            model_name='warrantycustomerbotsettings', name='welcome_text',
            field=models.TextField(default=NEW, verbose_name='приветствие'),
        ),
        migrations.RunPython(update_existing_welcome, migrations.RunPython.noop),
    ]
