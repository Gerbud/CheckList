from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('warranty', '0028_update_customer_bot_welcome')]

    operations = [
        migrations.AddField(
            model_name='warrantycustomerbotsettings',
            name='yandex_review_url',
            field=models.URLField(
                default='https://yandex.ru/maps/-/CTsGeI~a',
                help_text='Показывается клиенту после нажатия «Ответ помог».',
                verbose_name='ссылка для отзыва на Яндекс Картах',
            ),
        ),
    ]
