from datetime import timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.db.models import Q
from django.urls import NoReverseMatch, reverse
from django.utils import timezone

from checklists.access_control import (
    get_portal_home_url,
    get_user_store,
    is_store_director,
    is_system_admin,
    resolve_managed_store,
)
from checklists.models import (
    ChecklistItem,
    DailyChecklistStage,
    Store,
    StoreAdHocTask,
    StoreChecklistSchedule,
    StoreEmployee,
    TelegramMessageTemplate,
)
from checklists.services import build_stage_schedule


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
    'director_price_tags': 'Печать ценников',
    'price_tag_profile': 'Профиль магазина для ценников',
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
    checklist_store = get_user_store(request.user)
    checklist_warning = _header_checklist_warning(
        request.user,
        checklist_store,
    )
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
        'header_checklist_available': checklist_store is not None,
        **checklist_warning,
        'breadcrumbs': _breadcrumbs(request, store, home_url),
    }


def _header_checklist_warning(user, store):
    result = {
        'header_checklist_urgent': False,
        'header_checklist_can_warn': False,
        'header_checklist_warning_at': '',
        'header_checklist_deadline_at': '',
    }
    if store is None:
        return result
    schedule = StoreChecklistSchedule.objects.filter(
        store=store,
        is_active=True,
    ).first()
    if schedule is None:
        return result
    try:
        store_tz = ZoneInfo(store.timezone)
    except ZoneInfoNotFoundError:
        return result
    now = timezone.now()
    checklist_date = now.astimezone(store_tz).date()
    try:
        stages = build_stage_schedule(store, checklist_date)
    except Exception:
        return result
    current = next(
        (
            (code, bounds) for code, bounds in stages.items()
            if bounds['opens_at'] <= now < bounds['deadline_at']
        ),
        None,
    )
    if current is None:
        return result
    section_code, bounds = current
    warning_at = bounds['deadline_at'] - timedelta(
        minutes=schedule.warning_minutes_before,
    )
    result['header_checklist_warning_at'] = warning_at.isoformat()
    result['header_checklist_deadline_at'] = bounds['deadline_at'].isoformat()
    stage_query = DailyChecklistStage.objects.filter(
        daily_checklist__store=store,
        daily_checklist__checklist_date=checklist_date,
        section_code=section_code,
    ).filter(
        Q(daily_checklist__employee__user=user)
        | Q(daily_checklist__terminal_account__user=user)
    )
    result['header_checklist_can_warn'] = not stage_query.filter(
        status__in=(
            DailyChecklistStage.Status.COMPLETED,
            DailyChecklistStage.Status.COMPLETED_LATE,
        ),
    ).exists()
    result['header_checklist_urgent'] = (
        result['header_checklist_can_warn'] and now >= warning_at
    )
    return result
