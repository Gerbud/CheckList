from django.db import migrations


DEFAULTS = {
    'test_message': ('Тест Telegram', 'Связь с магазином {store_name} работает.'),
    'stage_reminder_30': (
        'До дедлайна 30 минут',
        '{store_name}: этап «{stage_name}», срок {deadline}. Осталось: {remaining_count}.',
    ),
    'stage_reminder_10': (
        'До дедлайна 10 минут',
        '{store_name}: этап «{stage_name}», срок {deadline}. Осталось: {remaining_count}.',
    ),
    'stage_closed': ('Этап закрыт', '{store_name}: этап «{stage_name}» за {date} закрыт.'),
    'stage_overdue': (
        'Этап просрочен',
        '{store_name}: этап «{stage_name}» просрочен. Не завершено: {remaining_count}.',
    ),
    'incomplete_tasks': (
        'Невыполненные задачи',
        '{store_name}, {date}, {stage_name}: осталось {remaining_count}, ошибок {failed_count}.',
    ),
    'task_created': (
        'Новая разовая задача',
        '{task_text}\nЭтап: {stage_name}\nДата: {date}\n{task_description}\n{task_url}',
    ),
    'task_completed': (
        'Разовая задача выполнена',
        '{task_text}\nСотрудник: {employee_name}\nКомментарий: {comment}\n{task_url}',
    ),
    'task_failed': (
        'Разовая задача не выполнена',
        '{task_text}\nСотрудник: {employee_name}\nКомментарий: {comment}\n{task_url}',
    ),
    'telegram_binding_pending': (
        'Привязка Telegram',
        'Ваш код: {comment}. Подтверждение выполняет администратор системы.',
    ),
    'telegram_binding_approved': (
        'Привязка подтверждена',
        'Telegram успешно привязан к магазину {store_name}.',
    ),
    'telegram_delivery_failed': (
        'Ошибка доставки Telegram',
        '{store_name}: сообщение не доставлено.',
    ),
}


def create_templates(apps, schema_editor):
    Store = apps.get_model('checklists', 'Store')
    Template = apps.get_model('checklists', 'TelegramMessageTemplate')
    for store in Store.objects.all().iterator():
        for template_type, (title, body) in DEFAULTS.items():
            Template.objects.get_or_create(
                store=store,
                template_type=template_type,
                defaults={
                    'title': title,
                    'body': body,
                    'parse_mode': 'HTML',
                    'is_enabled': True,
                    'send_to_private': False,
                    'send_to_group': True,
                },
            )


def remove_templates(apps, schema_editor):
    Template = apps.get_model('checklists', 'TelegramMessageTemplate')
    Template.objects.filter(template_type__in=DEFAULTS).delete()


class Migration(migrations.Migration):
    dependencies = [
        ('checklists', '0011_telegrampendingbinding_telegramupdatelog_and_more'),
    ]

    operations = [
        migrations.RunPython(create_templates, remove_templates),
    ]
