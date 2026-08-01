from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import migrations, models


def clamp_price_tag_property_limit(apps, schema_editor):
    Profile = apps.get_model('checklists', 'StorePriceTagTemplate')
    Profile.objects.filter(max_properties__gt=5).update(max_properties=5)


class Migration(migrations.Migration):

    dependencies = [
        ('checklists', '0032_price_tag_category_strategies'),
    ]

    operations = [
        migrations.RunPython(
            clamp_price_tag_property_limit,
            migrations.RunPython.noop,
        ),
        migrations.AlterField(
            model_name='storepricetagtemplate',
            name='max_properties',
            field=models.PositiveSmallIntegerField(
                default=5,
                validators=[MinValueValidator(1), MaxValueValidator(5)],
                verbose_name='максимум характеристик',
            ),
        ),
    ]
