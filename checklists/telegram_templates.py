import html
import string

from django.core.exceptions import ValidationError

from checklists.models import TelegramMessageTemplate
from checklists.telegram_events import (
    VARIABLES,
    get_telegram_event,
)


ALLOWED_TEMPLATE_VARIABLES = frozenset(VARIABLES)

DEFAULT_TELEGRAM_TEMPLATES = {
    'test_message': ('Тест Telegram', 'Связь с магазином {store_name} работает.'),
    'stage_reminder_30': (
        'До дедлайна 30 минут',
        '{store_name}: этап «{stage_name}», срок {deadline}. Осталось: {remaining_count}.',
    ),
    'stage_reminder_10': (
        'До дедлайна 10 минут',
        '{store_name}: этап «{stage_name}», срок {deadline}. Осталось: {remaining_count}.',
    ),
    'stage_closed': (
        'Этап закрыт',
        '{store_name}: этап «{stage_name}» за {date} закрыт.',
    ),
    'stage_overdue': (
        'Этап просрочен',
        '{store_name}: этап «{stage_name}» просрочен. Не завершено: {remaining_count}.',
    ),
    'incomplete_tasks': (
        'Невыполненные задачи',
        '{store_name}, {date}, {stage_name}: осталось {remaining_count}, ошибок {failed_count}.\n{comment}\n{checklist_url}',
    ),
    'task_created': (
        'Новая разовая задача',
        '{store_name}\n{task_text}\nЭтап: {stage_name}\nДата: {date}\n{task_description}\n{task_url}',
    ),
    'task_completed': (
        'Разовая задача выполнена',
        '{store_name}\n{task_text}\nЭтап: {stage_name}\nДата: {date}\nСотрудник: {employee_name}\nКомментарий: {comment}\n{task_url}',
    ),
    'task_failed': (
        'Разовая задача не выполнена',
        '{store_name}\n{task_text}\nЭтап: {stage_name}\nДата: {date}\nСотрудник: {employee_name}\nКомментарий: {comment}\n{task_url}',
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

MARKDOWN_V2_SPECIALS = r'_*[]()~`>#+-=|{}.!'


def template_defaults(event_code):
    event = get_telegram_event(event_code)
    title, body = DEFAULT_TELEGRAM_TEMPLATES[event_code]
    return {
        'name': event.title,
        'title': title,
        'body': body,
        'parse_mode': TelegramMessageTemplate.ParseMode.HTML,
        'is_enabled': True,
        'send_to_private': False,
        'send_to_group': True,
    }


def default_template(store, event_code):
    return TelegramMessageTemplate(
        store=store,
        event_code=event_code,
        **template_defaults(event_code),
    )


def get_template_or_default(store, event_code):
    template = TelegramMessageTemplate.objects.filter(
        store=store,
        event_code=event_code,
    ).first()
    return template or default_template(store, event_code)


def validate_template_source(value, event_code=None):
    formatter = string.Formatter()
    try:
        fields = {
            field_name.split('.')[0].split('[')[0]
            for _, field_name, _, _ in formatter.parse(value)
            if field_name
        }
    except ValueError as exc:
        raise ValidationError('Некорректные фигурные скобки в шаблоне.') from exc
    if event_code:
        try:
            allowed = get_telegram_event(event_code).variable_codes
        except ValueError as exc:
            raise ValidationError('Неизвестное событие Telegram.') from exc
    else:
        allowed = ALLOWED_TEMPLATE_VARIABLES
    unknown = fields - allowed
    if unknown:
        raise ValidationError(
            'Для этого события недоступны переменные: '
            + ', '.join(sorted(unknown))
        )
    return value


def _escape(value, parse_mode):
    value = str(value if value is not None else '')
    if parse_mode == TelegramMessageTemplate.ParseMode.HTML:
        return html.escape(value, quote=True)
    if parse_mode == TelegramMessageTemplate.ParseMode.MARKDOWN_V2:
        return ''.join(
            f'\\{char}' if char in MARKDOWN_V2_SPECIALS else char
            for char in value
        )
    return value


def example_context(event_code):
    event = get_telegram_event(event_code)
    return {variable.code: variable.example for variable in event.variables}


def render_template(template, context):
    event_code = template.event_code
    event = get_telegram_event(event_code)
    validate_template_source(template.title, event_code)
    validate_template_source(template.body, event_code)
    safe_context = {
        name: _escape(context.get(name, ''), template.parse_mode)
        for name in event.variable_codes
    }
    title = template.title.format_map(safe_context).strip()
    body = template.body.format_map(safe_context).strip()
    return f'{title}\n\n{body}'.strip()
