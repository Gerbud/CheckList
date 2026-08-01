from django.db import migrations, models


def assign_site_layouts(apps, schema_editor):
    Profile = apps.get_model('checklists', 'StorePriceTagTemplate')
    Profile.objects.filter(site_domain__icontains='pinel.ru').update(
        layout_template='pinel',
    )


def restore_default_layout(apps, schema_editor):
    Profile = apps.get_model('checklists', 'StorePriceTagTemplate')
    Profile.objects.update(layout_template='es_auto')


class Migration(migrations.Migration):

    dependencies = [
        ('checklists', '0035_remove_storepricetagtemplate_heading_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='storepricetagtemplate',
            name='layout_template',
            field=models.CharField(
                choices=[
                    ('es_auto', 'ES-AUTO — текущий шаблон'),
                    ('pinel', 'PINEL — отдельный шаблон'),
                ],
                default='es_auto',
                help_text=(
                    'Выберите оформление, которое используется для товаров '
                    'этого сайта.'
                ),
                max_length=20,
                verbose_name='шаблон оформления ценника',
            ),
        ),
        migrations.RunPython(assign_site_layouts, restore_default_layout),
        migrations.AddField(
            model_name='pricetaggeneration',
            name='sales_tip',
            field=models.CharField(
                blank=True,
                default='',
                max_length=400,
                verbose_name='совет продавцу',
            ),
        ),
        migrations.AddField(
            model_name='pricetaggeneration',
            name='seller_praise',
            field=models.CharField(
                blank=True,
                default='',
                max_length=160,
                verbose_name='похвала продавцу',
            ),
        ),
    ]
