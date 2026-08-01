from django.db import migrations, models
import django.core.validators
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('checklists', '0026_alter_checklistnotification_notification_type'),
    ]

    operations = [
        migrations.CreateModel(
            name='StorePriceTagTemplate',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('heading', models.CharField(blank=True, default='', max_length=80, verbose_name='подпись на ценнике')),
                ('primary_color', models.CharField(default='#172554', max_length=7, validators=[django.core.validators.RegexValidator('^#[0-9A-Fa-f]{6}$', 'Укажите цвет в формате #112233.')], verbose_name='основной цвет')),
                ('accent_color', models.CharField(default='#f97316', max_length=7, validators=[django.core.validators.RegexValidator('^#[0-9A-Fa-f]{6}$', 'Укажите цвет в формате #112233.')], verbose_name='акцентный цвет')),
                ('show_image', models.BooleanField(default=True, verbose_name='показывать фото')),
                ('show_sku', models.BooleanField(default=True, verbose_name='показывать артикул')),
                ('show_properties', models.BooleanField(default=True, verbose_name='показывать характеристики')),
                ('max_properties', models.PositiveSmallIntegerField(default=5, validators=[django.core.validators.MinValueValidator(1), django.core.validators.MaxValueValidator(8)], verbose_name='максимум характеристик')),
                ('footer', models.CharField(blank=True, default='', max_length=120, verbose_name='текст внизу')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='изменён')),
                ('store', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='price_tag_template', to='checklists.store', verbose_name='магазин')),
            ],
            options={
                'verbose_name': 'шаблон ценника',
                'verbose_name_plural': 'шаблоны ценников',
            },
        ),
    ]
