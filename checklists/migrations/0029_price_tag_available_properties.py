from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('checklists', '0028_price_tag_profiles_and_categories'),
    ]

    operations = [
        migrations.AddField(
            model_name='storepricetagtemplate',
            name='available_property_names',
            field=models.JSONField(blank=True, default=list, verbose_name='найденные свойства'),
        ),
    ]
