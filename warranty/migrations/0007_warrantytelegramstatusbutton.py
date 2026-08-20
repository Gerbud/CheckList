from django.db import migrations, models


def seed_customer_handover_button(apps, schema_editor):
    Button = apps.get_model('warranty', 'WarrantyTelegramStatusButton')
    Button.objects.get_or_create(
        source_status='customer_wait',
        label='Выдано клиенту',
        defaults={'target_status': 'closed', 'position': 100, 'is_enabled': True},
    )


class Migration(migrations.Migration):
    dependencies = [('warranty', '0006_alter_warrantytelegramthread_state')]
    operations = [
        migrations.CreateModel(
            name='WarrantyTelegramStatusButton',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('source_status', models.CharField(choices=[('new', 'Новый'), ('service_decision', 'Ожидает решение СЦ'), ('in_progress', 'В работе'), ('customer_wait', 'Ожидаем клиента'), ('diagnostics', 'Диагностика'), ('parts_wait', 'Ожидаем запчасти'), ('ready', 'Готов к выдаче'), ('closed', 'Закрыт')], max_length=32, verbose_name='показывать при статусе')),
                ('label', models.CharField(max_length=64, verbose_name='текст кнопки')),
                ('target_status', models.CharField(choices=[('new', 'Новый'), ('service_decision', 'Ожидает решение СЦ'), ('in_progress', 'В работе'), ('customer_wait', 'Ожидаем клиента'), ('diagnostics', 'Диагностика'), ('parts_wait', 'Ожидаем запчасти'), ('ready', 'Готов к выдаче'), ('closed', 'Закрыт')], max_length=32, verbose_name='перевести в статус')),
                ('position', models.PositiveSmallIntegerField(default=100, verbose_name='порядок')),
                ('is_enabled', models.BooleanField(default=True, verbose_name='показывать')),
            ],
            options={'verbose_name': 'кнопка Telegram для статуса гарантии', 'verbose_name_plural': 'кнопки Telegram для статусов гарантии', 'ordering': ('source_status', 'position', 'id')},
        ),
        migrations.AddConstraint(
            model_name='warrantytelegramstatusbutton',
            constraint=models.UniqueConstraint(fields=('source_status', 'label'), name='unique_warranty_telegram_status_button'),
        ),
        migrations.RunPython(seed_customer_handover_button, migrations.RunPython.noop),
    ]
