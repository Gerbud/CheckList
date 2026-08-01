from urllib.parse import urlsplit

from django.db import migrations, models


def prepare_category_strategies(apps, schema_editor):
    Profile = apps.get_model('checklists', 'StorePriceTagTemplate')
    Category = apps.get_model('checklists', 'StorePriceTagCategory')

    Profile.objects.filter(site_domain='pinel.ru').update(
        category_detection_mode='property',
    )
    for category in Category.objects.select_related('profile'):
        value = category.source_url.strip()
        if value and not urlsplit(value).scheme:
            path = '/' + value.lstrip('/')
            category.source_url = f'https://{category.profile.site_domain}{path}'
            category.save(update_fields=('source_url',))


def restore_url_patterns(apps, schema_editor):
    Profile = apps.get_model('checklists', 'StorePriceTagTemplate')
    Category = apps.get_model('checklists', 'StorePriceTagCategory')

    Profile.objects.update(category_detection_mode='url')
    for category in Category.objects.all():
        parts = urlsplit(category.source_url)
        if parts.scheme:
            category.source_url = parts.path or '/'
            category.save(update_fields=('source_url',))


class Migration(migrations.Migration):

    dependencies = [
        ('checklists', '0031_price_tag_site_profiles'),
    ]

    operations = [
        migrations.AddField(
            model_name='storepricetagtemplate',
            name='category_detection_mode',
            field=models.CharField(
                choices=[
                    ('url', 'По адресу раздела'),
                    ('property', 'По свойству товара'),
                ],
                default='url',
                max_length=20,
                verbose_name='определение категории',
            ),
        ),
        migrations.RenameField(
            model_name='storepricetagcategory',
            old_name='url_patterns',
            new_name='source_url',
        ),
        migrations.AddField(
            model_name='storepricetagcategory',
            name='match_property_name',
            field=models.CharField(
                blank=True,
                default='',
                max_length=160,
                verbose_name='свойство для определения',
            ),
        ),
        migrations.AddField(
            model_name='storepricetagcategory',
            name='match_property_value',
            field=models.CharField(
                blank=True,
                default='',
                max_length=255,
                verbose_name='значение свойства',
            ),
        ),
        migrations.RunPython(
            prepare_category_strategies,
            restore_url_patterns,
        ),
        migrations.AlterField(
            model_name='storepricetagcategory',
            name='source_url',
            field=models.URLField(
                blank=True,
                default='',
                help_text='Например: https://es-auto.ru/car-box/.',
                max_length=500,
                verbose_name='ссылка на раздел сайта',
            ),
        ),
        migrations.AlterField(
            model_name='storepricetagcategory',
            name='property_names',
            field=models.TextField(
                blank=True,
                default='',
                help_text=(
                    'Каждое свойство с новой строки, '
                    'в нужном порядке.'
                ),
                verbose_name='свойства на ценнике',
            ),
        ),
    ]
