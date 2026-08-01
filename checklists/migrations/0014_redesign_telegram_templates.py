from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


EVENT_NAMES = {
    'test_message': 'Тестовое сообщение',
    'stage_reminder_30': 'Напоминание за 30 минут',
    'stage_reminder_10': 'Напоминание за 10 минут',
    'stage_closed': 'Этап закрыт',
    'stage_overdue': 'Этап просрочен',
    'incomplete_tasks': 'Невыполненные задачи',
    'task_created': 'Разовая задача создана',
    'task_completed': 'Разовая задача выполнена',
    'task_failed': 'Разовая задача не выполнена',
    'telegram_binding_pending': 'Привязка ожидает подтверждения',
    'telegram_binding_approved': 'Привязка подтверждена',
    'telegram_delivery_failed': 'Ошибка доставки',
}


def populate_template_names(apps, schema_editor):
    Template = apps.get_model('checklists', 'TelegramMessageTemplate')
    for template in Template.objects.order_by('pk').iterator():
        template.name = EVENT_NAMES.get(template.event_code, template.title)
        template.save(update_fields=('name',))


class Migration(migrations.Migration):
    dependencies = [
        ('checklists', '0013_expand_default_telegram_task_templates'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name='telegrammessagetemplate',
            name='unique_store_telegram_template',
        ),
        migrations.RenameField(
            model_name='telegrammessagetemplate',
            old_name='template_type',
            new_name='event_code',
        ),
        migrations.AddField(
            model_name='telegrammessagetemplate',
            name='name',
            field=models.CharField(
                blank=True,
                max_length=255,
                null=True,
                verbose_name='название',
            ),
        ),
        migrations.AddField(
            model_name='telegrammessagetemplate',
            name='created_by',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='created_telegram_templates',
                to=settings.AUTH_USER_MODEL,
                verbose_name='создал',
            ),
        ),
        migrations.RunPython(
            populate_template_names,
            migrations.RunPython.noop,
        ),
        migrations.AlterField(
            model_name='telegrammessagetemplate',
            name='name',
            field=models.CharField(max_length=255, verbose_name='название'),
        ),
        migrations.AlterModelOptions(
            name='telegrammessagetemplate',
            options={
                'ordering': ('store', 'event_code'),
                'verbose_name': 'шаблон сообщения Telegram',
                'verbose_name_plural': 'шаблоны сообщений Telegram',
            },
        ),
    ]
