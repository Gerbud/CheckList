from django.db import migrations, models
import django.db.models.deletion


def split_price_tag_profiles(apps, schema_editor):
    Template = apps.get_model('checklists', 'StorePriceTagTemplate')
    Category = apps.get_model('checklists', 'StorePriceTagCategory')

    for template in Template.objects.all().order_by('id'):
        template.name = 'ES-AUTO'
        template.site_domain = 'es-auto.ru'
        template.save(update_fields=('name', 'site_domain'))
        categories = Category.objects.filter(store_id=template.store_id)
        for category in categories:
            category.profile_id = template.pk
            detection_source = f'{category.name} {category.url_patterns}'.casefold()
            if 'бокс' in detection_source or 'car-box' in detection_source:
                category.url_patterns = '/car-box/'
            category.save(update_fields=('profile', 'url_patterns'))
        Template.objects.get_or_create(
            store_id=template.store_id,
            site_domain='pinel.ru',
            defaults={
                'name': 'PINEL',
                'logo': None,
                'available_property_names': [],
            },
        )


def join_price_tag_profiles(apps, schema_editor):
    Template = apps.get_model('checklists', 'StorePriceTagTemplate')
    Category = apps.get_model('checklists', 'StorePriceTagCategory')

    for category in Category.objects.select_related('profile'):
        category.store_id = category.profile.store_id
        category.save(update_fields=('store',))
    for store_id in Template.objects.values_list('store_id', flat=True).distinct():
        templates = list(Template.objects.filter(store_id=store_id).order_by('id'))
        keep = next(
            (item for item in templates if item.site_domain == 'es-auto.ru'),
            templates[0],
        )
        Template.objects.filter(store_id=store_id).exclude(pk=keep.pk).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('checklists', '0030_storepricetagtemplate_print_mode_and_more'),
    ]

    operations = [
        migrations.AlterModelOptions(
            name='storepricetagtemplate',
            options={
                'ordering': ('name', 'id'),
                'verbose_name': 'шаблон ценника',
                'verbose_name_plural': 'шаблоны ценников',
            },
        ),
        migrations.AddField(
            model_name='storepricetagtemplate',
            name='name',
            field=models.CharField(
                default='ES-AUTO',
                max_length=120,
                verbose_name='название интернет-магазина',
            ),
        ),
        migrations.AddField(
            model_name='storepricetagtemplate',
            name='site_domain',
            field=models.CharField(
                default='es-auto.ru',
                help_text='Например: es-auto.ru или pinel.ru.',
                max_length=255,
                verbose_name='домен сайта',
            ),
        ),
        migrations.AddField(
            model_name='storepricetagtemplate',
            name='is_active',
            field=models.BooleanField(default=True, verbose_name='активен'),
        ),
        migrations.AlterField(
            model_name='storepricetagtemplate',
            name='store',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name='price_tag_templates',
                to='checklists.store',
                verbose_name='магазин',
            ),
        ),
        migrations.RemoveConstraint(
            model_name='storepricetagcategory',
            name='unique_price_tag_category_store',
        ),
        migrations.RenameField(
            model_name='storepricetagcategory',
            old_name='keywords',
            new_name='url_patterns',
        ),
        migrations.AlterField(
            model_name='storepricetagcategory',
            name='url_patterns',
            field=models.TextField(
                help_text='Через запятую: /car-box/, /snow-blowers/.',
                verbose_name='фрагменты адреса категории',
            ),
        ),
        migrations.AlterField(
            model_name='storepricetagcategory',
            name='store',
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name='price_tag_categories',
                to='checklists.store',
                verbose_name='магазин',
            ),
        ),
        migrations.AddField(
            model_name='storepricetagcategory',
            name='profile',
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name='categories',
                to='checklists.storepricetagtemplate',
                verbose_name='профиль интернет-магазина',
            ),
        ),
        migrations.RunPython(split_price_tag_profiles, join_price_tag_profiles),
        migrations.RemoveField(
            model_name='storepricetagcategory',
            name='store',
        ),
        migrations.AlterField(
            model_name='storepricetagcategory',
            name='profile',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name='categories',
                to='checklists.storepricetagtemplate',
                verbose_name='профиль интернет-магазина',
            ),
        ),
        migrations.AddConstraint(
            model_name='storepricetagtemplate',
            constraint=models.UniqueConstraint(
                fields=('store', 'site_domain'),
                name='unique_price_tag_profile_store_domain',
            ),
        ),
        migrations.AddConstraint(
            model_name='storepricetagcategory',
            constraint=models.UniqueConstraint(
                fields=('profile', 'name'),
                name='unique_price_tag_category_profile',
            ),
        ),
    ]
