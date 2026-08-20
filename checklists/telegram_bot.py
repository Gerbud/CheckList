import secrets
from datetime import date, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.db import IntegrityError, transaction
from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from checklists.ad_hoc_tasks import (
    available_ad_hoc_sections,
    create_ad_hoc_task,
    is_ad_hoc_stage_closed,
)
from checklists.models import (
    StoreAdHocTask,
    TelegramConversationState,
    TelegramPendingBinding,
    TelegramStoreBinding,
    TelegramUserProfile,
    UserStoreMembership,
    TelegramUpdateLog,
)
from checklists.telegram_client import send_telegram_request
from checklists.telegram_actions import (
    TelegramAction,
    collect_telegram_actions,
    emit_telegram_action,
)
from checklists.telegram_update_processor import (
    TelegramProcessResult,
    UpdateMode,
    classify_telegram_update,
)


CONVERSATION_TTL = timedelta(minutes=30)


def _store_today(store):
    try:
        tz = ZoneInfo(store.timezone)
    except ZoneInfoNotFoundError:
        tz = ZoneInfo('UTC')
    return timezone.now().astimezone(tz).date()


def _button(text, callback_data):
    return {'text': text, 'callback_data': callback_data}


def _reply(chat_id, text, update_id, suffix, *, keyboard=None, store=None):
    payload = {
        'chat_id': str(chat_id),
        'text': text,
        'disable_web_page_preview': True,
    }
    if keyboard:
        payload['reply_markup'] = {'inline_keyboard': keyboard}
    return emit_telegram_action(TelegramAction(
        store=store,
        chat_id=str(chat_id),
        message_type='bot_reply',
        idempotency_key=f'update:{update_id}:{suffix}',
        method='sendMessage',
        payload=payload,
    ))


def _answer_callback(callback_id, chat_id, update_id):
    return emit_telegram_action(TelegramAction(
        chat_id=str(chat_id),
        message_type='callback_answer',
        idempotency_key=f'update:{update_id}:callback-answer',
        method='answerCallbackQuery',
        payload={'callback_query_id': callback_id},
    ))


def _safe_update(update):
    message = update.get('message') or {}
    callback = update.get('callback_query') or {}
    source = callback.get('from') or message.get('from') or {}
    chat = message.get('chat') or (callback.get('message') or {}).get('chat') or {}
    safe = {
        'update_id': update.get('update_id'),
        'message_text': str(message.get('text', ''))[:2000],
        'callback_data': str(callback.get('data', ''))[:256],
        'callback_query_id': str(callback.get('id', ''))[:128],
        'telegram_user_id': source.get('id'),
        'telegram_chat_id': chat.get('id'),
    }
    if message:
        safe['message'] = {
            'text': safe['message_text'],
            'from': {
                'id': source.get('id'),
                'username': str(source.get('username', ''))[:64],
                'first_name': str(source.get('first_name', ''))[:128],
                'last_name': str(source.get('last_name', ''))[:128],
                'is_bot': bool(source.get('is_bot')),
            },
            'chat': {
                'id': chat.get('id'),
                'type': str(chat.get('type', ''))[:32],
            },
        }
    if callback:
        safe['callback_query'] = {
            'id': safe['callback_query_id'],
            'data': safe['callback_data'],
            'from': {
                'id': source.get('id'),
                'username': str(source.get('username', ''))[:64],
                'first_name': str(source.get('first_name', ''))[:128],
                'last_name': str(source.get('last_name', ''))[:128],
                'is_bot': bool(source.get('is_bot')),
            },
            'message': {
                'chat': {
                    'id': chat.get('id'),
                    'type': str(chat.get('type', ''))[:32],
                }
            },
        }
    return safe


def _identity(update):
    message = update.get('message') or {}
    callback = update.get('callback_query') or {}
    source = callback.get('from') or message.get('from') or {}
    chat = message.get('chat') or (callback.get('message') or {}).get('chat') or {}
    return source, chat, message, callback


def _pending_code():
    return f'{secrets.randbelow(1_000_000):06d}'


def _create_pending(update_id, source, chat):
    TelegramPendingBinding.objects.filter(
        telegram_user_id=source['id'],
        status=TelegramPendingBinding.Status.PENDING,
    ).update(status=TelegramPendingBinding.Status.EXPIRED)
    for _ in range(10):
        try:
            return TelegramPendingBinding.objects.create(
                telegram_user_id=source['id'],
                telegram_chat_id=chat['id'],
                username=str(source.get('username', ''))[:64],
                first_name=str(source.get('first_name', ''))[:128],
                last_name=str(source.get('last_name', ''))[:128],
                one_time_code=_pending_code(),
                expires_at=timezone.now() + CONVERSATION_TTL,
                update_id=update_id,
            )
        except IntegrityError:
            continue
    raise RuntimeError('Не удалось создать одноразовый код.')


def _binding(source):
    return TelegramStoreBinding.objects.select_related('store', 'user').filter(
        telegram_user_id=source.get('id'),
        is_active=True,
        store__is_active=True,
    ).first()


def _set_state(binding, state, data=None):
    value, _ = TelegramConversationState.objects.update_or_create(
        telegram_binding=binding,
        defaults={
            'state': state,
            'data': data or {},
            'expires_at': timezone.now() + CONVERSATION_TTL,
        },
    )
    return value


def _get_state(binding):
    state = TelegramConversationState.objects.filter(
        telegram_binding=binding
    ).first()
    if state and state.expires_at <= timezone.now():
        state.delete()
        return None
    return state


def _date_keyboard():
    return [
        [
            _button('Сегодня', 'task:date:today'),
            _button('Завтра', 'task:date:tomorrow'),
        ],
        [_button('Выбрать дату', 'task:date:custom')],
        [_button('Отмена', 'task:cancel'), _button('Главное меню', 'menu:main')],
    ]


def _section_keyboard(store, work_date):
    labels = {
        'morning': 'Утро',
        'day': 'День',
        'evening': 'Вечер',
    }
    available = available_ad_hoc_sections(store, work_date)
    return [
        [_button(labels[code], f'task:section:{code}')]
        for code in available
    ] + [
        [_button('Назад', 'task:modify'), _button('Отмена', 'task:cancel')],
        [_button('Главное меню', 'menu:main')],
    ]


def _main_menu_keyboard():
    return [
        [_button('➕ Поставить задачу', 'menu:task')],
        [_button('📋 Задачи магазина', 'tasks:active')],
        [_button('⚠️ Проблемные задачи', 'tasks:problem')],
        [_button('❓ Помощь', 'menu:help')],
    ]


def _navigation_keyboard(*, back_callback='menu:main'):
    return [[
        _button('Назад', back_callback),
        _button('Отмена', 'task:cancel'),
        _button('Главное меню', 'menu:main'),
    ]]


def _show_main_menu(update_id, binding, chat_id, suffix='menu'):
    TelegramConversationState.objects.filter(telegram_binding=binding).delete()
    memberships = _quick_task_memberships(binding)
    store_names = ', '.join(
        membership.store.name for membership in memberships
    ) or binding.store.name
    return _reply(
        chat_id,
        f'Магазины: {store_names}\n\nВыберите действие',
        update_id,
        suffix,
        keyboard=_main_menu_keyboard(),
        store=binding.store,
    )


def _help_text():
    return (
        'Команды бота:\n'
        '/start, /menu — главное меню\n'
        '/newtask — поставить задачу\n'
        '/tasks — задачи магазина\n'
        '/status — статус магазина и задач\n'
        '/myid — ваш Telegram ID\n'
        '/cancel — отменить текущее действие\n'
        '/help — эта справка'
    )


def _task_list_keyboard():
    return [
        [
            _button('Сегодня', 'tasks:today'),
            _button('Завтра', 'tasks:tomorrow'),
        ],
        [
            _button('Активные', 'tasks:active'),
            _button('Проблемные', 'tasks:problem'),
        ],
        [_button('Все', 'tasks:all')],
        [_button('Главное меню', 'menu:main')],
    ]


def _task_status_icon(task, today):
    if task.status == StoreAdHocTask.Status.COMPLETED:
        return '✅'
    if task.status == StoreAdHocTask.Status.CANCELLED:
        return '➖'
    if task.status == StoreAdHocTask.Status.FAILED or (
        task.date < today
        and task.status in {
            StoreAdHocTask.Status.PLANNED,
            StoreAdHocTask.Status.ACTIVE,
        }
    ):
        return '⚠️'
    return '🕘'


def _show_tasks(update_id, binding, chat_id, task_filter='active'):
    today = _store_today(binding.store)
    query = StoreAdHocTask.objects.filter(store=binding.store).select_related(
        'completed_by_employee'
    )
    if task_filter == 'today':
        query = query.filter(date=today)
    elif task_filter == 'tomorrow':
        query = query.filter(date=today + timedelta(days=1))
    elif task_filter == 'problem':
        query = query.filter(
            Q(status=StoreAdHocTask.Status.FAILED)
            | Q(
                date__lt=today,
                status__in=(
                    StoreAdHocTask.Status.PLANNED,
                    StoreAdHocTask.Status.ACTIVE,
                ),
            )
        )
    elif task_filter == 'active':
        query = query.filter(
            Q(
                status__in=(
                    StoreAdHocTask.Status.PLANNED,
                    StoreAdHocTask.Status.ACTIVE,
                    StoreAdHocTask.Status.FAILED,
                )
            )
            | Q(
                status=StoreAdHocTask.Status.COMPLETED,
                date__in=(today, today + timedelta(days=1)),
            )
        )
    tasks = list(query.order_by('date', 'section_code', 'id')[:30])
    groups = []
    for work_date, title in (
        (today, 'Сегодня'),
        (today + timedelta(days=1), 'Завтра'),
    ):
        rows = [task for task in tasks if task.date == work_date]
        if rows:
            groups.append((title, rows))
    remaining = [
        task for task in tasks
        if task.date not in {today, today + timedelta(days=1)}
    ]
    if remaining:
        groups.append(('Другие даты', remaining))
    lines = [f'Задачи магазина «{binding.store.name}»']
    for title, rows in groups:
        lines.extend(('', f'{title}:'))
        for task in rows:
            detail = (
                f'{_task_status_icon(task, today)} '
                f'{task.get_section_code_display()} · {task.text}'
            )
            if title == 'Другие даты':
                detail = f'{task.date:%d.%m} · {detail}'
            lines.append(detail)
            if task.description and task.status == StoreAdHocTask.Status.FAILED:
                lines.append(f'  Причина: {task.completion_comment[:180]}')
            if task.completed_by_employee:
                lines.append(
                    f'  Исполнитель: {task.completed_by_employee.display_name}'
                )
            if getattr(settings, 'SITE_URL', ''):
                lines.append(
                    f"  {settings.SITE_URL}/director/tasks/{task.pk}/"
                )
    if not tasks:
        lines.append('\nПо выбранному фильтру задач нет.')
    return _reply(
        chat_id,
        '\n'.join(lines),
        update_id,
        f'tasks-{task_filter}',
        keyboard=_task_list_keyboard(),
        store=binding.store,
    )


def _show_status(update_id, binding, chat_id):
    today = _store_today(binding.store)
    tasks = StoreAdHocTask.objects.filter(store=binding.store)
    active = tasks.filter(
        status__in=(
            StoreAdHocTask.Status.PLANNED,
            StoreAdHocTask.Status.ACTIVE,
        )
    ).count()
    problems = tasks.filter(
        Q(status=StoreAdHocTask.Status.FAILED)
        | Q(
            date__lt=today,
            status__in=(
                StoreAdHocTask.Status.PLANNED,
                StoreAdHocTask.Status.ACTIVE,
            ),
        )
    ).count()
    return _reply(
        chat_id,
        (
            f'Статус магазина «{binding.store.name}»\n'
            f'Активных задач: {active}\n'
            f'Требуют внимания: {problems}'
        ),
        update_id,
        'status',
        keyboard=_navigation_keyboard(),
        store=binding.store,
    )


def _show_identity(update_id, source, chat, binding=None):
    username = str(source.get('username', '')).strip()
    lines = [
        f"Telegram ID: {source['id']}",
        f"Username: @{username}" if username else 'Username: не указан',
    ]
    if binding:
        memberships = _quick_task_memberships(binding)
        if memberships:
            lines.append('Магазины:')
            lines.extend(
                (
                    f'• {membership.store.name} — '
                    f'{membership.get_role_in_store_display()}'
                )
                for membership in memberships
            )
        else:
            lines.append(f'Магазин: {binding.store.name}')
    else:
        lines.append('Магазин: привязка не подтверждена')
    return _reply(
        chat['id'],
        '\n'.join(lines),
        update_id,
        'identity',
        keyboard=_navigation_keyboard() if binding else None,
        store=binding.store if binding else None,
    )


def _closed_stage_text(section_code, available):
    labels = {'morning': 'утренний', 'day': 'дневной', 'evening': 'вечерний'}
    if section_code == 'evening':
        return 'Вечерний этап уже закрыт. Выберите другую дату.'
    offered = ', '.join(labels[code] for code in available) or 'нет доступных'
    return f'{labels[section_code].capitalize()} этап уже закрыт. Доступны: {offered}.'


def _handle_start(update_id, source, chat):
    existing = _binding(source)
    if existing:
        if existing.user_id is None:
            return _reply(
                chat['id'],
                'Ваш Telegram не привязан к аккаунту.',
                update_id,
                'account-not-linked',
                store=existing.store,
            )
        TelegramUserProfile.objects.update_or_create(
            user=existing.user,
            defaults={
                'telegram_user_id': existing.telegram_user_id,
                'telegram_chat_id': chat['id'],
                'telegram_username': str(source.get('username', ''))[:64],
                'first_name': str(source.get('first_name', ''))[:128],
                'last_name': str(source.get('last_name', ''))[:128],
                'is_verified': True,
            },
        )
        return _show_main_menu(update_id, existing, chat['id'], 'start-menu')
    pending = _create_pending(update_id, source, chat)
    username = f"@{pending.username}" if pending.username else 'не указан'
    return _reply(
        chat['id'],
        (
            'Заявка на привязку создана.\n'
            f'Telegram ID: {pending.telegram_user_id}\n'
            f'Username: {username}\n'
            f'Одноразовый код: {pending.one_time_code}\n'
            'Передайте код системному администратору.'
        ),
        update_id,
        'pending-binding',
    )


def _start_task(update_id, binding):
    _set_state(binding, 'choose_date')
    return _reply(
        binding.telegram_chat_id,
        'На какую дату создать задачу?',
        update_id,
        'task-date',
        keyboard=_date_keyboard(),
        store=binding.store,
    )


def _quick_task_memberships(binding):
    if binding.user_id is None:
        return []
    return list(
        UserStoreMembership.objects.filter(
            user_id=binding.user_id,
            is_active=True,
            store__is_active=True,
        )
        .select_related('store')
        .order_by('store__name', 'store_id')
    )


def _quick_task_target(store):
    work_date = _store_today(store)
    available = available_ad_hoc_sections(store, work_date)
    if not available:
        work_date += timedelta(days=1)
        available = available_ad_hoc_sections(store, work_date)
    return work_date, available[0]


def _create_quick_task(update_id, binding, membership, text, chat_id):
    work_date, section_code = _quick_task_target(membership.store)
    task = create_ad_hoc_task(
        store=membership.store,
        date=work_date,
        section_code=section_code,
        text=text,
        source=StoreAdHocTask.Source.TELEGRAM,
        created_by=binding.user,
        created_by_telegram_binding=binding,
    )
    TelegramConversationState.objects.filter(telegram_binding=binding).delete()
    return _reply(
        chat_id,
        (
            '✅ Задача создана.\n'
            f'Магазин: {membership.store.name}\n'
            f'Дата: {task.date:%d.%m.%Y}\n'
            f'Этап: {task.get_section_code_display()}\n'
            f'Задача: {task.text}'
        ),
        update_id,
        f'quick-created-{membership.pk}',
        keyboard=_main_menu_keyboard(),
        store=membership.store,
    )


def _start_quick_task(update_id, binding, text, chat_id):
    memberships = _quick_task_memberships(binding)
    if not memberships:
        return _reply(
            chat_id,
            'У пользователя нет активных связей с магазинами.',
            update_id,
            'no-store-memberships',
        )
    if len(memberships) == 1:
        return _create_quick_task(
            update_id,
            binding,
            memberships[0],
            text,
            chat_id,
        )
    _set_state(binding, 'quick_store', {'text': text})
    return _reply(
        chat_id,
        'Выберите магазин:',
        update_id,
        'quick-store',
        keyboard=[
            [_button(
                f'{number}. {membership.store.name}',
                f'quicktask:store:{membership.pk}',
            )]
            for number, membership in enumerate(memberships, start=1)
        ] + [[_button('Отмена', 'task:cancel')]],
    )


def _handle_callback(update_id, binding, callback, chat):
    data = str(callback.get('data', ''))
    _answer_callback(callback.get('id', ''), chat['id'], update_id)
    state = _get_state(binding)
    if data.startswith('quicktask:store:'):
        if state is None or state.state != 'quick_store':
            return _reply(
                chat['id'],
                'Выбор магазина устарел. Отправьте команду ещё раз.',
                update_id,
                'quick-store-expired',
            )
        try:
            membership_id = int(data.rsplit(':', 1)[-1])
        except ValueError:
            membership_id = 0
        membership = UserStoreMembership.objects.select_related('store').filter(
            pk=membership_id,
            user_id=binding.user_id,
            is_active=True,
            store__is_active=True,
        ).first()
        if membership is None:
            return _reply(
                chat['id'],
                'Магазин недоступен.',
                update_id,
                'quick-store-forbidden',
            )
        return _create_quick_task(
            update_id,
            binding,
            membership,
            state.data['text'],
            chat['id'],
        )
    if data == 'menu:main':
        return _show_main_menu(update_id, binding, chat['id'], 'main-menu')
    if data == 'menu:task':
        return _start_task(update_id, binding)
    if data == 'menu:help':
        return _reply(
            chat['id'],
            _help_text(),
            update_id,
            'help-menu',
            keyboard=_navigation_keyboard(),
            store=binding.store,
        )
    if data.startswith('tasks:'):
        return _show_tasks(
            update_id,
            binding,
            chat['id'],
            data.split(':', 1)[1],
        )
    if data == 'task:cancel':
        if state:
            state.delete()
        return _reply(
            chat['id'],
            'Создание задачи отменено.\n\nВыберите действие',
            update_id,
            'cancel',
            keyboard=_main_menu_keyboard(),
            store=binding.store,
        )
    if data == 'task:modify':
        return _start_task(update_id, binding)
    if state is None:
        return _reply(
            chat['id'],
            'Диалог истёк. Отправьте /task ещё раз.',
            update_id,
            'expired',
            keyboard=_main_menu_keyboard(),
            store=binding.store,
        )
    if data.startswith('task:date:') and state.state == 'choose_date':
        choice = data.rsplit(':', 1)[-1]
        if choice == 'custom':
            _set_state(binding, 'custom_date')
            return _reply(
                chat['id'],
                'Введите дату в формате ГГГГ-ММ-ДД.',
                update_id,
                'custom-date',
                keyboard=_navigation_keyboard(back_callback='task:modify'),
                store=binding.store,
            )
        today = _store_today(binding.store)
        work_date = today if choice == 'today' else today + timedelta(days=1)
        _set_state(binding, 'choose_section', {'date': work_date.isoformat()})
        keyboard = _section_keyboard(binding.store, work_date)
        return _reply(
            chat['id'],
            f'Выберите этап на {work_date:%d.%m.%Y}.',
            update_id,
            'section',
            keyboard=keyboard,
            store=binding.store,
        )
    if data.startswith('task:section:') and state.state == 'choose_section':
        section_code = data.rsplit(':', 1)[-1]
        if section_code not in StoreAdHocTask.SectionCode.values:
            return _reply(
                chat['id'],
                'Недопустимый этап.',
                update_id,
                'bad-section',
                keyboard=_navigation_keyboard(back_callback='task:modify'),
                store=binding.store,
            )
        work_date = date.fromisoformat(state.data['date'])
        if is_ad_hoc_stage_closed(binding.store, work_date, section_code):
            available = available_ad_hoc_sections(binding.store, work_date)
            return _reply(
                chat['id'],
                _closed_stage_text(section_code, available),
                update_id,
                'closed-section',
                keyboard=_section_keyboard(binding.store, work_date),
                store=binding.store,
            )
        data_value = {**state.data, 'section_code': section_code}
        _set_state(binding, 'task_text', data_value)
        return _reply(
            chat['id'],
            'Введите текст задачи.',
            update_id,
            'task-text',
            keyboard=_navigation_keyboard(back_callback='task:modify'),
            store=binding.store,
        )
    if data == 'task:skip-description' and state.state == 'description':
        data_value = {**state.data, 'description': ''}
        _set_state(binding, 'confirm', data_value)
        return _confirmation(update_id, binding, data_value, chat['id'])
    if data == 'task:confirm' and state.state == 'confirm':
        work_date = date.fromisoformat(state.data['date'])
        section_code = state.data['section_code']
        if is_ad_hoc_stage_closed(binding.store, work_date, section_code):
            available = available_ad_hoc_sections(binding.store, work_date)
            _set_state(binding, 'choose_section', {'date': work_date.isoformat()})
            return _reply(
                chat['id'],
                _closed_stage_text(section_code, available),
                update_id,
                'closed-confirm',
                keyboard=_section_keyboard(binding.store, work_date),
                store=binding.store,
            )
        task = create_ad_hoc_task(
            store=binding.store,
            date=work_date,
            section_code=section_code,
            text=state.data['text'],
            description=state.data.get('description', ''),
            source=StoreAdHocTask.Source.TELEGRAM,
            created_by=binding.user,
            created_by_telegram_binding=binding,
        )
        state.delete()
        return _reply(
            chat['id'],
            (
                '✅ Задача создана.\n'
                f'Дата: {task.date:%d.%m.%Y}\n'
                f'Этап: {task.get_section_code_display()}\n'
                f'Задача: {task.text}'
            ),
            update_id,
            'created',
            keyboard=[
                [_button('Создать ещё', 'menu:task')],
                [_button('Задачи магазина', 'tasks:active')],
                [_button('Главное меню', 'menu:main')],
            ],
            store=binding.store,
        )
    return _reply(
        chat['id'],
        'Команда диалога устарела.',
        update_id,
        'stale',
        keyboard=_main_menu_keyboard(),
        store=binding.store,
    )


def _confirmation(update_id, binding, data, chat_id):
    description = data.get('description') or '—'
    return _reply(
        chat_id,
        (
            'Проверьте задачу:\n'
            f"Дата: {data['date']}\n"
            f"Этап: {data['section_code']}\n"
            f"Текст: {data['text']}\n"
            f'Описание: {description}'
        ),
        update_id,
        'confirm',
        keyboard=[
            [_button('Подтвердить', 'task:confirm')],
            [_button('Изменить', 'task:modify'), _button('Отмена', 'task:cancel')],
            [_button('Главное меню', 'menu:main')],
        ],
        store=binding.store,
    )


def _handle_message_state(update_id, binding, text):
    state = _get_state(binding)
    if state is None:
        return None
    if state.state == 'custom_date':
        try:
            work_date = date.fromisoformat(text.strip())
        except ValueError:
            return _reply(
                binding.telegram_chat_id,
                'Дата не распознана. Используйте ГГГГ-ММ-ДД.',
                update_id,
                'bad-date',
                keyboard=_navigation_keyboard(back_callback='task:modify'),
                store=binding.store,
            )
        _set_state(binding, 'choose_section', {'date': work_date.isoformat()})
        return _reply(
            binding.telegram_chat_id,
            f'Выберите этап на {work_date:%d.%m.%Y}.',
            update_id,
            'section-custom',
            keyboard=_section_keyboard(binding.store, work_date),
            store=binding.store,
        )
    if state.state == 'task_text':
        value = text.strip()
        if not value:
            return _reply(
                binding.telegram_chat_id,
                'Текст задачи не может быть пустым.',
                update_id,
                'empty-text',
                keyboard=_navigation_keyboard(back_callback='task:modify'),
                store=binding.store,
            )
        _set_state(binding, 'description', {**state.data, 'text': value})
        return _reply(
            binding.telegram_chat_id,
            'Введите описание или нажмите «Без описания».',
            update_id,
            'description',
            keyboard=[
                [_button('Без описания', 'task:skip-description')],
                *_navigation_keyboard(back_callback='task:modify'),
            ],
            store=binding.store,
        )
    if state.state == 'description':
        data_value = {**state.data, 'description': text.strip()}
        _set_state(binding, 'confirm', data_value)
        return _confirmation(
            update_id,
            binding,
            data_value,
            binding.telegram_chat_id,
        )
    return None


def _handle_bound(update_id, binding, message, callback, chat):
    callback_data = str(callback.get('data', ''))
    text = str(message.get('text', '')).strip()
    command = text.split()[0].split('@')[0].lower() if text.startswith('/') else ''
    starts_task = (
        command in {'/task', '/newtask'}
        or callback_data.startswith('task:')
        or callback_data.startswith('quicktask:')
        or callback_data == 'menu:task'
    )
    if starts_task and binding.user_id is None:
        if callback.get('id'):
            _answer_callback(callback.get('id', ''), chat['id'], update_id)
        return _reply(
            chat['id'],
            'Ваш Telegram не привязан к аккаунту.',
            update_id,
            'account-not-linked',
            store=binding.store,
        )
    if callback:
        return _handle_callback(update_id, binding, callback, chat)
    if command in {'/task', '/newtask'}:
        direct_text = text.split(maxsplit=1)[1].strip() if ' ' in text else ''
        if direct_text:
            return _start_quick_task(
                update_id,
                binding,
                direct_text,
                chat['id'],
            )
        return _start_task(update_id, binding)
    if command in {'/start', '/menu'}:
        return _show_main_menu(update_id, binding, chat['id'], 'command-menu')
    if command == '/cancel':
        TelegramConversationState.objects.filter(telegram_binding=binding).delete()
        return _reply(
            chat['id'],
            'Диалог отменён.\n\nВыберите действие',
            update_id,
            'cancel-command',
            keyboard=_main_menu_keyboard(),
            store=binding.store,
        )
    if command == '/tasks':
        return _show_tasks(update_id, binding, chat['id'])
    if command == '/status':
        return _show_status(update_id, binding, chat['id'])
    if command in {'/myid', '/whoami'}:
        return _show_identity(update_id, message.get('from') or {}, chat, binding)
    if command == '/help':
        return _reply(
            chat['id'],
            _help_text(),
            update_id,
            'help',
            keyboard=_navigation_keyboard(),
            store=binding.store,
        )
    state_result = _handle_message_state(update_id, binding, text)
    if state_result:
        return state_result
    return _reply(
        chat['id'],
        'Команда не распознана. Выберите действие.',
        update_id,
        'unknown',
        keyboard=_main_menu_keyboard(),
        store=binding.store,
    )


def _dispatch_logged_update(log):
    update = log.payload
    update_id = log.update_id
    source, chat, message, callback = _identity(update)
    try:
        if not source.get('id') or not chat.get('id'):
            raise ValueError('Update не содержит идентификаторы.')
        if source.get('is_bot'):
            raise ValueError('Сообщения бота не обрабатываются.')
        text = str(message.get('text', '')).strip()
        command = (
            text.split()[0].split('@')[0].lower()
            if text.startswith('/')
            else ''
        )
        if command == '/start':
            _handle_start(update_id, source, chat)
        elif command in {'/myid', '/whoami'}:
            _show_identity(update_id, source, chat, _binding(source))
        else:
            binding = _binding(source)
            if binding is None:
                if callback.get('id'):
                    _answer_callback(callback.get('id', ''), chat['id'], update_id)
                if command == '/reg_user':
                    _reply(
                        chat['id'],
                        'Доступ не подтверждён. Отправьте /start.',
                        update_id,
                        'not-bound',
                    )
            else:
                _handle_bound(update_id, binding, message, callback, chat)
    except Exception:
        log.processing_error = 'Update processing error.'
        log.processed = False
        log.processed_at = timezone.now()
        log.save(
            update_fields=('processing_error', 'processed', 'processed_at')
        )
        return 'failed'
    log.processed = True
    log.processed_at = timezone.now()
    log.save(update_fields=('processed', 'processed_at'))
    return 'processed'


def process_logged_telegram_update(log):
    if log.processed:
        return 'duplicate'
    return _dispatch_logged_update(log)


def process_telegram_update(update, *, collect_actions=False):
    update_id = int(update['update_id'])
    processing_mode = classify_telegram_update(update)
    safe = _safe_update(update)
    update_type = 'callback_query' if update.get('callback_query') else 'message'
    text = str((safe.get('message') or {}).get('text', '')).strip()
    command = (
        text.split()[0].split('@')[0].lower()[:64]
        if text.startswith('/')
        else ''
    )
    if not command and safe.get('callback_data'):
        command = f"callback:{safe['callback_data']}"[:64]
    try:
        with transaction.atomic():
            log = TelegramUpdateLog.objects.create(
                update_id=update_id,
                telegram_user_id=safe['telegram_user_id'],
                telegram_chat_id=safe['telegram_chat_id'],
                update_type=update_type,
                command=command,
                payload=safe,
            )
    except IntegrityError:
        return (
            TelegramProcessResult('duplicate', processing_mode)
            if collect_actions
            else 'duplicate'
        )
    from warranty.telegram import record_warranty_update
    if record_warranty_update(update):
        log.processed = True
        log.processed_at = timezone.now()
        log.save(update_fields=('processed', 'processed_at'))
        return (
            TelegramProcessResult('ignored', processing_mode)
            if collect_actions
            else 'processed'
        )
    if processing_mode == UpdateMode.IGNORED:
        log.processed = True
        log.processed_at = timezone.now()
        log.save(update_fields=('processed', 'processed_at'))
        return (
            TelegramProcessResult('ignored', processing_mode)
            if collect_actions
            else 'processed'
        )
    if not collect_actions:
        return _dispatch_logged_update(log)
    with collect_telegram_actions() as actions:
        outcome = _dispatch_logged_update(log)
    return TelegramProcessResult(
        outcome=outcome,
        processing_mode=processing_mode,
        actions=tuple(actions),
    )


def poll_telegram_updates(*, limit=100, timeout=0):
    limit = max(1, min(int(limit), 100))
    timeout = max(0, min(int(timeout), 50))
    last_update = (
        TelegramUpdateLog.objects.order_by('-update_id')
        .values_list('update_id', flat=True)
        .first()
    )
    payload = {'limit': limit, 'timeout': timeout}
    if last_update is not None:
        payload['offset'] = last_update + 1
    response = send_telegram_request('getUpdates', payload, incoming=True)
    updates = response.data.get('result', [])
    result = {'received': len(updates), 'processed': 0, 'duplicate': 0, 'failed': 0}
    for update in updates[:limit]:
        outcome = process_telegram_update(update)
        result[outcome] += 1
    return result
