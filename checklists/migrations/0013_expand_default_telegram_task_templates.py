from django.db import migrations


CHANGES = {
    'incomplete_tasks': (
        '{store_name}, {date}, {stage_name}: осталось {remaining_count}, ошибок {failed_count}.',
        '{store_name}, {date}, {stage_name}: осталось {remaining_count}, ошибок {failed_count}.\n{comment}\n{checklist_url}',
    ),
    'task_created': (
        '{task_text}\nЭтап: {stage_name}\nДата: {date}\n{task_description}\n{task_url}',
        '{store_name}\n{task_text}\nЭтап: {stage_name}\nДата: {date}\n{task_description}\n{task_url}',
    ),
    'task_completed': (
        '{task_text}\nСотрудник: {employee_name}\nКомментарий: {comment}\n{task_url}',
        '{store_name}\n{task_text}\nЭтап: {stage_name}\nДата: {date}\nСотрудник: {employee_name}\nКомментарий: {comment}\n{task_url}',
    ),
    'task_failed': (
        '{task_text}\nСотрудник: {employee_name}\nКомментарий: {comment}\n{task_url}',
        '{store_name}\n{task_text}\nЭтап: {stage_name}\nДата: {date}\nСотрудник: {employee_name}\nКомментарий: {comment}\n{task_url}',
    ),
}


def update_untouched_defaults(apps, schema_editor):
    Template = apps.get_model('checklists', 'TelegramMessageTemplate')
    for template_type, (old_body, new_body) in CHANGES.items():
        Template.objects.filter(
            template_type=template_type,
            body=old_body,
        ).update(body=new_body)


def reverse_untouched_defaults(apps, schema_editor):
    Template = apps.get_model('checklists', 'TelegramMessageTemplate')
    for template_type, (old_body, new_body) in CHANGES.items():
        Template.objects.filter(
            template_type=template_type,
            body=new_body,
        ).update(body=old_body)


class Migration(migrations.Migration):
    dependencies = [
        ('checklists', '0012_create_default_telegram_templates'),
    ]

    operations = [
        migrations.RunPython(
            update_untouched_defaults,
            reverse_untouched_defaults,
        ),
    ]
