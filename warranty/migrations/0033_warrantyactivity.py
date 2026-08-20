from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [('warranty', '0032_admin_log_utf8mb4')]

    operations = [
        migrations.AlterModelOptions(
            name='warrantyclaim',
            options={
                'ordering': ('-source_created_at', '-external_id'),
                'verbose_name': 'гарантийное обращение',
                'verbose_name_plural': 'обращения',
            },
        ),
        migrations.AlterModelOptions(
            name='warrantycustomerprofile',
            options={
                'verbose_name': 'клиент гарантийного отдела',
                'verbose_name_plural': 'клиенты',
            },
        ),
        migrations.AlterModelOptions(
            name='warrantyproductregistration',
            options={
                'verbose_name': 'зарегистрированный товар',
                'verbose_name_plural': 'зарегистрированные товары',
            },
        ),
        migrations.CreateModel(
            name='WarrantyActivity',
            fields=[],
            options={
                'verbose_name': 'история обращения',
                'verbose_name_plural': 'история работы',
                'proxy': True,
                'indexes': [],
                'constraints': [],
            },
            bases=('warranty.warrantyclaim',),
        ),
    ]
