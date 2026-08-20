from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('warranty', '0024_warrantycustomerbotsettings_consent_text_template')]

    operations = [
        migrations.AddField(
            model_name='warrantycustomerbotsettings',
            name='product_consultation_enabled',
            field=models.BooleanField(default=True, verbose_name='консультации по товарам включены'),
        ),
        migrations.AlterField(
            model_name='warrantycustomerbotsettings',
            name='ocr_api_key',
            field=models.CharField(blank=True, help_text='Используется для распознавания и консультаций по товарам. Если ключ пуст, консультации недоступны.', max_length=255, verbose_name='ключ OpenAI для распознавания'),
        ),
        migrations.AlterField(
            model_name='warrantycustomersession',
            name='mode',
            field=models.CharField(choices=[('claim', 'Рекламация'), ('registration', 'Регистрация покупки'), ('consultation', 'Консультация по товару')], default='claim', max_length=24),
        ),
        migrations.AlterField(
            model_name='warrantycustomersession',
            name='step',
            field=models.CharField(choices=[('menu', 'Главное меню'), ('consent', 'Согласие на обработку данных'), ('product', 'Выбор зарегистрированного товара'), ('label', 'Фото этикетки'), ('warranty_card', 'Фото гарантийного талона'), ('receipt', 'Фото чека'), ('phone', 'Телефон'), ('full_name', 'ФИО'), ('article', 'Артикул вручную'), ('serial', 'Серийный номер вручную'), ('purchase_date', 'Дата покупки вручную'), ('ready', 'Подтверждение'), ('submitted', 'Оформлено'), ('consultation', 'Консультация по товару')], default='label', max_length=32),
        ),
    ]
