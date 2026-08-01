from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('checklists', '0027_storepricetagtemplate'),
    ]

    operations = [
        migrations.AddField(
            model_name='storepricetagtemplate',
            name='logo',
            field=models.ImageField(blank=True, null=True, upload_to='stores/price_tag_logo/', verbose_name='логотип для ценников'),
        ),
        migrations.CreateModel(
            name='StorePriceTagCategory',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=120, verbose_name='категория')),
                ('keywords', models.TextField(help_text='Через запятую: газонокосилка, lawn mower.', verbose_name='слова для распознавания')),
                ('property_names', models.TextField(help_text='Каждое свойство с новой строки, в нужном порядке.', verbose_name='свойства на ценнике')),
                ('sort_order', models.PositiveIntegerField(default=0, verbose_name='порядок')),
                ('is_active', models.BooleanField(default=True, verbose_name='активна')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='изменена')),
                ('store', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='price_tag_categories', to='checklists.store', verbose_name='магазин')),
            ],
            options={
                'verbose_name': 'категория ценников',
                'verbose_name_plural': 'категории ценников',
                'ordering': ('sort_order', 'name', 'id'),
                'constraints': [models.UniqueConstraint(fields=('store', 'name'), name='unique_price_tag_category_store')],
            },
        ),
    ]
