import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('checklists', '0036_price_tag_layouts_and_seller_tips'),
    ]

    operations = [
        migrations.AddField(
            model_name='storepricetagtemplate',
            name='promotion_background_color',
            field=models.CharField(
                default='#fff7ed',
                help_text='Используется для блока акции в шаблоне ES-AUTO.',
                max_length=7,
                validators=[
                    django.core.validators.RegexValidator(
                        '^#[0-9A-Fa-f]{6}$',
                        'Укажите цвет в формате #112233.',
                    ),
                ],
                verbose_name='цвет фона акции',
            ),
        ),
    ]
