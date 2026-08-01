from django.urls import NoReverseMatch, reverse

from checklists.access_control import (
    get_portal_home_url,
    get_user_store,
    is_store_director,
    is_system_admin,
    resolve_managed_store,
)
from checklists.models import (
    ChecklistItem,
    Store,
    StoreAdHocTask,
    StoreEmployee,
    TelegramMessageTemplate,
)


SECTION_TITLES = {
    'director_dashboard': 'Обзор',
    'director_employees': 'Сотрудники',
    'director_employee_add': 'Добавить сотрудника',
    'director_shifts': 'Смены',
    'director_shift_date': 'Смены',
    'director_questions': 'Вопросы',
    'director_question_add': 'Добавить вопрос',
    'director_schedule': 'Расписание',
    'director_notifications': 'Уведомления',
    'director_tasks': 'Задачи',
    'director_task_create': 'Добавить задачу',
    'director_reports': 'Отчёты',
    'director_report_daily': 'Ежедневный отчёт',
    'director_report_employees': 'Отчёт по сотрудникам',
    'director_report_revisions': 'История изменений',
    'director_report_employee_detail': 'Карточка сотрудника',
    'director_report_tasks': 'Отчёт по задачам',
    'director_report_problems': 'Что требует внимания',
    'director_report_recurring': 'Повторяющиеся проблемы',
    'director_checklist_detail': 'Чек-лист',
    'employee_schedule': 'Мой график',
    'system_admin_dashboard': 'Главная',
    'system_reports': 'Сводка магазинов',
    'system_tasks': 'Все задачи',
    'system_task_edit': 'Изменить задачу',
    'system_task_delete': 'Удалить задачу',
    'system_stores': 'Магазины',
    'system_store_add': 'Добавить магазин',
    'system_users': 'Пользователи',
    'system_user_add': 'Добавить пользователя',
    'system_audit': 'Журнал действий',
    'telegram_settings': 'Telegram',
    'telegram_templates': 'Шаблоны',
    'telegram_chats': 'Чаты и Topics',
    'telegram_users': 'Привязки',
    'telegram_queue': 'Очередь',
}


def _safe_reverse(name, kwargs=None):
    try:
        return reverse(f'checklists:{name}', kwargs=kwargs)
    except NoReverseMatch:
        return None


def _object_title(match):
    kwargs = match.kwargs
    name = match.url_name or ''
    if 'store_id' in kwargs:
        return Store.objects.filter(pk=kwargs['store_id']).values_list(
            'name', flat=True
        ).first()
    if 'employee_id' in kwargs:
        return StoreEmployee.objects.filter(pk=kwargs['employee_id']).values_list(
            'display_name', flat=True
        ).first()
    if 'question_id' in kwargs:
        return ChecklistItem.objects.filter(pk=kwargs['question_id']).values_list(
            'text', flat=True
        ).first()
    if 'task_id' in kwargs:
        return StoreAdHocTask.objects.filter(pk=kwargs['task_id']).values_list(
            'text', flat=True
        ).first()
    if 'template_id' in kwargs and name.startswith('telegram_template'):
        return TelegramMessageTemplate.objects.filter(
            pk=kwargs['template_id']
        ).values_list('name', flat=True).first()
    return None


def _breadcrumbs(request, store, home_url):
    match = getattr(request, 'resolver_match', None)
    if match is None:
        return []
    name = match.url_name or ''
    system = is_system_admin(request.user)
    crumbs = [{'title': 'Главная', 'url': home_url}]
    if system and store and name.startswith(('director_', 'telegram_')):
        crumbs.append(
            {
                'title': store.name,
                'url': _safe_reverse('director_dashboard'),
            }
        )

    parent_name = None
    parent_title = None
    if name.startswith('system_store') and name != 'system_stores':
        parent_name, parent_title = 'system_stores', 'Магазины'
    elif name.startswith('system_user') and name != 'system_users':
        parent_name, parent_title = 'system_users', 'Пользователи'
    elif name.startswith('director_employee') and name != 'director_employees':
        parent_name, parent_title = 'director_employees', 'Сотрудники'
    elif name.startswith('director_shift') and name not in {
        'director_shifts',
        'director_shift_date',
    }:
        parent_name, parent_title = 'director_shifts', 'Смены'
    elif name.startswith('director_question') and name != 'director_questions':
        parent_name, parent_title = 'director_questions', 'Вопросы'
    elif name.startswith('director_task') and name != 'director_tasks':
        parent_name, parent_title = 'director_tasks', 'Задачи'
    elif name.startswith('director_report') and name != 'director_reports':
        parent_name, parent_title = 'director_reports', 'Отчёты'
    elif name.startswith('telegram_') and name != 'telegram_settings':
        crumbs.append(
            {'title': 'Telegram', 'url': _safe_reverse('telegram_settings')}
        )
        if name.startswith('telegram_template') and name != 'telegram_templates':
            parent_name, parent_title = 'telegram_templates', 'Шаблоны'

    if parent_name:
        crumbs.append({'title': parent_title, 'url': _safe_reverse(parent_name)})

    title = _object_title(match)
    if not title:
        title = SECTION_TITLES.get(name)
    if not title:
        if name.endswith(('_add', '_create')):
            title = 'Добавить'
        elif name.endswith('_edit'):
            title = 'Изменить'
        elif name.endswith(('_delete', '_remove')):
            title = 'Удалить'
        else:
            title = 'Страница'
    if request.path == home_url:
        crumbs[0]['url'] = None
    else:
        crumbs.append({'title': title, 'url': None})
    return crumbs


def portal_context(request):
    if not getattr(request.user, 'is_authenticated', False):
        return {}
    system = is_system_admin(request.user)
    director = is_store_director(request.user)
    store = resolve_managed_store(request) if system or director else None
    portal_store = store or get_user_store(request.user)
    home_url = get_portal_home_url(request.user)
    personal_schedule_available = StoreEmployee.objects.filter(
        user=request.user,
        is_active=True,
        store__is_active=True,
    ).exists()
    return {
        'portal_is_system_admin': system,
        'portal_is_store_director': director,
        'managed_store': store,
        'portal_store': portal_store,
        'managed_store_home_url': (
            _safe_reverse('director_dashboard') if store else None
        ),
        'portal_home_url': home_url,
        'personal_schedule_available': personal_schedule_available,
        'breadcrumbs': _breadcrumbs(request, store, home_url),
    }
