from collections import defaultdict
from datetime import date, timedelta
from functools import wraps
import secrets

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import Avg, Count, ExpressionWrapper, F, fields
from django.http import Http404, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.urls import reverse

from checklists.access_control import (
    get_portal_home_url,
    get_user_store,
    is_store_director,
    is_system_admin,
    resolve_managed_store,
)
from checklists.exceptions import ChecklistServiceError
from checklists.models import (
    AuditLog,
    Store,
    TelegramMessageTemplate,
    TelegramInboundJob,
    TelegramOutboundMessage,
    TelegramPendingBinding,
    TelegramStoreBinding,
    TelegramStoreChat,
    TelegramSystemSettings,
    TelegramUpdateLog,
    TelegramUserProfile,
)
from checklists.portal_forms import (
    TelegramBindingApprovalForm,
    TelegramMessageTemplateCreateForm,
    TelegramMessageTemplateForm,
    TelegramProfileUserForm,
    TelegramStoreChatForm,
    TelegramSystemSettingsForm,
)
from checklists.telegram_client import TelegramAPIError, send_telegram_request
from checklists.telegram_commands import get_bot_commands, register_bot_commands
from checklists.telegram_queue import (
    delete_telegram_message,
    enqueue_template_message,
    enqueue_telegram_message,
)
from checklists.telegram_services import (
    approve_pending_binding,
    disable_telegram_binding,
    disconnect_telegram_profile,
    link_telegram_user,
    reassign_telegram_profile,
    transfer_telegram_binding,
    update_telegram_system_settings,
)
from checklists.telegram_templates import (
    default_template,
    example_context,
    render_template,
    template_defaults,
)
from checklists.telegram_events import (
    TELEGRAM_CATEGORY_LABELS,
    TELEGRAM_EVENTS,
    TELEGRAM_EVENTS_BY_CODE,
    TelegramEventCategory,
    get_telegram_event,
)


def telegram_settings_required(view):
    @wraps(view)
    @login_required
    def wrapped(request, *args, **kwargs):
        if not (is_system_admin(request.user) or is_store_director(request.user)):
            return HttpResponseForbidden('Настройки Telegram недоступны.')
        request.telegram_is_system_admin = is_system_admin(request.user)
        return view(request, *args, **kwargs)

    return wrapped


def _selected_store(request, *, required=True):
    store = resolve_managed_store(request)
    if required and store is None:
        raise Http404('Сначала выберите магазин.')
    return store


def _base_context(request, store, *, active_tab='summary', breadcrumb_tail=()):
    home_url = get_portal_home_url(request.user)
    home_title = 'Главная'
    breadcrumbs = [
        {'title': home_title, 'url': home_url},
        {'title': 'Telegram', 'url': reverse('checklists:telegram_settings')},
        *breadcrumb_tail,
    ]
    store_summary = None
    if store:
        disabled_count = TelegramMessageTemplate.objects.filter(
            store=store,
            is_enabled=False,
        ).count()
        store_summary = {
            'active_templates': len(TELEGRAM_EVENTS) - disabled_count,
            'active_chats': TelegramStoreChat.objects.filter(
                store=store,
                is_active=True,
            ).count(),
            'pending_messages': TelegramOutboundMessage.objects.filter(
                store=store,
                status=TelegramOutboundMessage.Status.PENDING,
            ).count(),
            'failed_messages': TelegramOutboundMessage.objects.filter(
                store=store,
                status=TelegramOutboundMessage.Status.FAILED,
            ).count(),
        }
    return {
        'portal': (
            'system_admin'
            if request.telegram_is_system_admin
            else 'director'
        ),
        'store': store,
        'selected_store_obj': store,
        'telegram_is_system_admin': request.telegram_is_system_admin,
        'telegram_stores': (
            Store.objects.order_by('name')
            if request.telegram_is_system_admin
            else Store.objects.none()
        ),
        'telegram_active_tab': active_tab,
        'telegram_breadcrumbs': breadcrumbs,
        'telegram_store_summary': store_summary,
    }


@telegram_settings_required
def telegram_settings(request):
    store = _selected_store(request, required=False)
    config = TelegramSystemSettings.get_solo()
    system_form = (
        TelegramSystemSettingsForm(instance=config)
        if request.telegram_is_system_admin
        else None
    )
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'system_settings':
            if not request.telegram_is_system_admin:
                return HttpResponseForbidden('Недостаточно прав.')
            system_form = TelegramSystemSettingsForm(request.POST, instance=config)
            if system_form.is_valid():
                update_telegram_system_settings(
                    actor=request.user,
                    data=system_form.cleaned_data,
                    new_token=system_form.cleaned_data.get('new_token', ''),
                    clear_token=system_form.cleaned_data.get('clear_token', False),
                    new_webhook_secret=system_form.cleaned_data.get(
                        'new_webhook_secret', ''
                    ),
                    clear_webhook_secret=system_form.cleaned_data.get(
                        'clear_webhook_secret', False
                    ),
                )
                messages.success(request, 'Системные настройки Telegram сохранены.')
                return redirect('checklists:telegram_settings')
        elif action == 'get_me':
            if not request.telegram_is_system_admin:
                return HttpResponseForbidden('Недостаточно прав.')
            try:
                response = send_telegram_request('getMe', {})
            except TelegramAPIError as exc:
                messages.error(request, str(exc))
            else:
                username = (response.data.get('result') or {}).get('username', 'без username')
                messages.success(request, f'Бот отвечает: @{username}.')
            return redirect('checklists:telegram_settings')
        elif action in {
            'webhook_register',
            'webhook_check',
            'webhook_delete',
            'webhook_polling',
            'inbound_retry_failed',
            'commands_register',
            'commands_check',
        }:
            if not request.telegram_is_system_admin:
                return HttpResponseForbidden('Недостаточно прав.')
            now = timezone.now()
            if action == 'commands_register':
                try:
                    register_bot_commands(config)
                except TelegramAPIError as exc:
                    messages.error(request, str(exc))
                else:
                    messages.success(request, 'Команды бота зарегистрированы.')
            elif action == 'commands_check':
                try:
                    commands = get_bot_commands(config)
                except TelegramAPIError as exc:
                    messages.error(request, str(exc))
                else:
                    messages.success(
                        request,
                        f'Команды бота проверены: {len(commands)}.',
                    )
            elif action == 'webhook_register':
                if not config.webhook_secret_token:
                    config.webhook_secret_token = secrets.token_urlsafe(32)
                config.webhook_url = (
                    f"{settings.SITE_URL}"
                    f"{reverse('checklists:telegram_webhook')}"
                )
                try:
                    send_telegram_request(
                        'setWebhook',
                        {
                            'url': config.webhook_url,
                            'secret_token': config.webhook_secret_token,
                            'max_connections': config.webhook_max_connections,
                            'allowed_updates': config.webhook_allowed_updates or [],
                        },
                    )
                except TelegramAPIError as exc:
                    config.webhook_last_error = str(exc)
                    messages.error(request, str(exc))
                else:
                    config.incoming_mode = TelegramSystemSettings.IncomingMode.WEBHOOK
                    config.webhook_is_enabled = True
                    config.webhook_registered_at = now
                    config.webhook_last_error = ''
                    AuditLog.objects.create(
                        actor=request.user,
                        object_type=config._meta.label_lower,
                        object_id='1',
                        action=AuditLog.Action.TELEGRAM_WEBHOOK_REGISTERED,
                        new_value={'webhook_configured': True},
                    )
                    messages.success(request, 'Webhook зарегистрирован.')
                config.updated_by = request.user
                config.save()
            elif action == 'webhook_check':
                try:
                    response = send_telegram_request('getWebhookInfo', {})
                except TelegramAPIError as exc:
                    config.webhook_last_error = str(exc)
                    messages.error(request, str(exc))
                else:
                    result = response.data.get('result') or {}
                    config.webhook_is_enabled = bool(result.get('url'))
                    config.webhook_last_error = str(
                        result.get('last_error_message', '')
                    )[:1000]
                    AuditLog.objects.create(
                        actor=request.user,
                        object_type=config._meta.label_lower,
                        object_id='1',
                        action=AuditLog.Action.TELEGRAM_WEBHOOK_CHECKED,
                        new_value={
                            'registered': config.webhook_is_enabled,
                            'pending_update_count': result.get(
                                'pending_update_count', 0
                            ),
                        },
                    )
                    messages.success(request, 'Состояние webhook обновлено.')
                config.webhook_last_checked_at = now
                config.updated_by = request.user
                config.save()
            elif action == 'webhook_delete':
                try:
                    send_telegram_request('deleteWebhook', {'drop_pending_updates': False})
                except TelegramAPIError as exc:
                    messages.error(request, str(exc))
                else:
                    config.webhook_is_enabled = False
                    config.webhook_registered_at = None
                    config.save()
                    AuditLog.objects.create(
                        actor=request.user,
                        object_type=config._meta.label_lower,
                        object_id='1',
                        action=AuditLog.Action.TELEGRAM_WEBHOOK_DELETED,
                        new_value={'registered': False},
                    )
                    messages.success(request, 'Webhook удалён.')
            elif action == 'webhook_polling':
                old_mode = config.incoming_mode
                config.incoming_mode = TelegramSystemSettings.IncomingMode.POLLING
                config.webhook_is_enabled = False
                config.updated_by = request.user
                config.save()
                AuditLog.objects.create(
                    actor=request.user,
                    object_type=config._meta.label_lower,
                    object_id='1',
                    action=AuditLog.Action.TELEGRAM_INCOMING_MODE_CHANGED,
                    old_value={'incoming_mode': old_mode},
                    new_value={'incoming_mode': config.incoming_mode},
                )
                messages.success(request, 'Включён резервный polling.')
            else:
                failed = TelegramInboundJob.objects.filter(
                    status=TelegramInboundJob.Status.FAILED
                )
                count = failed.update(
                    status=TelegramInboundJob.Status.PENDING,
                    available_at=now,
                    last_error='',
                )
                AuditLog.objects.create(
                    actor=request.user,
                    object_type=TelegramInboundJob._meta.label_lower,
                    object_id='failed',
                    action=AuditLog.Action.TELEGRAM_INBOUND_JOB_RETRIED,
                    new_value={'count': count},
                )
                messages.success(request, f'Возвращено в очередь: {count}.')
            return redirect('checklists:telegram_settings')
        elif action == 'test':
            if store is None:
                messages.error(request, 'Сначала выберите магазин.')
            else:
                queued = enqueue_template_message(
                    store,
                    'test_message',
                    {
                        'store_name': store.name,
                        'date': timezone.localdate().strftime('%d.%m.%Y'),
                    },
                    idempotency_key=(
                        f'test:{store.pk}:{request.user.pk}:'
                        f'{timezone.now().isoformat()}'
                    ),
                )
                AuditLog.objects.create(
                    actor=request.user,
                    store=store,
                    object_type=Store._meta.label_lower,
                    object_id=str(store.pk),
                    action=AuditLog.Action.TELEGRAM_TEST_SENT,
                    new_value={'queued_count': len(queued)},
                )
                messages.success(
                    request,
                    f'Тестовых сообщений поставлено в очередь: {len(queued)}.',
                )
            return redirect('checklists:telegram_settings')
        else:
            return HttpResponseForbidden('Неизвестное действие.')
    queue_stats = {}
    query = TelegramOutboundMessage.objects.all()
    if store:
        query = query.filter(store=store)
    for row in query.values('status').annotate(total=Count('id')):
        queue_stats[row['status']] = row['total']
    context = {
        **_base_context(request, store, active_tab='summary'),
        'config': config,
        'system_form': system_form,
        'queue_stats': queue_stats,
        'chat_count': store.telegram_chats.count() if store else 0,
        'binding_count': store.telegram_bindings.filter(is_active=True).count()
        if store
        else 0,
    }
    inbound_query = TelegramInboundJob.objects.all()
    if not request.telegram_is_system_admin:
        inbound_query = inbound_query.filter(store=store)
    duration = ExpressionWrapper(
        F('completed_at') - F('created_at'),
        output_field=fields.DurationField(),
    )
    context['inbound_stats'] = {
        'updates_24h': TelegramUpdateLog.objects.filter(
            created_at__gte=timezone.now() - timedelta(hours=24)
        ).count(),
        'pending': inbound_query.filter(
            status=TelegramInboundJob.Status.PENDING
        ).count(),
        'failed': inbound_query.filter(
            status=TelegramInboundJob.Status.FAILED
        ).count(),
        'average_duration': inbound_query.filter(
            status=TelegramInboundJob.Status.COMPLETED
        ).aggregate(value=Avg(duration))['value'],
        'last_received': inbound_query.order_by('-created_at').first(),
        'last_completed': inbound_query.filter(
            status=TelegramInboundJob.Status.COMPLETED
        ).order_by('-completed_at').first(),
        'last_failed': inbound_query.filter(
            status=TelegramInboundJob.Status.FAILED
        ).order_by('-updated_at').first(),
    }
    return render(request, 'checklists/telegram/settings.html', context)


@telegram_settings_required
def telegram_chats(request):
    store = _selected_store(request)
    if store is None:
        raise Http404
    form = TelegramStoreChatForm(request.POST or None, store=store)
    if request.method == 'POST':
        if form.is_valid():
            with transaction.atomic():
                chat = form.save()
                AuditLog.objects.create(
                    actor=request.user,
                    store=store,
                    object_type=chat._meta.label_lower,
                    object_id=str(chat.pk),
                    action=AuditLog.Action.TELEGRAM_STORE_CHAT_CREATED,
                    new_value={
                        'title': chat.title,
                        'chat_type': chat.chat_type,
                        'purpose': chat.purpose,
                        'has_topic': chat.message_thread_id is not None,
                    },
                )
            messages.success(request, 'Telegram-чат добавлен.')
            return redirect('checklists:telegram_chats')
    return render(
        request,
        'checklists/telegram/chats.html',
        {
            **_base_context(
                request,
                store,
                active_tab='chats',
                breadcrumb_tail=({'title': 'Чаты и Topics', 'url': None},),
            ),
            'form': form,
            'chats': TelegramStoreChat.objects.filter(store=store),
        },
    )


@telegram_settings_required
def telegram_chat_edit(request, chat_id):
    store = _selected_store(request)
    chat = get_object_or_404(TelegramStoreChat, pk=chat_id, store=store)
    form = TelegramStoreChatForm(
        request.POST or None,
        instance=chat,
        store=store,
    )
    if request.method == 'POST' and form.is_valid():
        with transaction.atomic():
            form.save()
            AuditLog.objects.create(
                actor=request.user,
                store=store,
                object_type=chat._meta.label_lower,
                object_id=str(chat.pk),
                action=AuditLog.Action.TELEGRAM_STORE_CHAT_UPDATED,
                new_value={
                    'title': chat.title,
                    'purpose': chat.purpose,
                    'is_active': chat.is_active,
                    'has_topic': chat.message_thread_id is not None,
                },
            )
        messages.success(request, 'Telegram-чат сохранён.')
        return redirect('checklists:telegram_chats')
    return render(
        request,
        'checklists/telegram/form.html',
        {
            **_base_context(
                request,
                store,
                active_tab='chats',
                breadcrumb_tail=(
                    {
                        'title': 'Чаты и Topics',
                        'url': reverse('checklists:telegram_chats'),
                    },
                    {'title': 'Изменение чата', 'url': None},
                ),
            ),
            'title': 'Изменить Telegram-чат',
            'form': form,
        },
    )


@telegram_settings_required
def telegram_chat_action(request, chat_id, action):
    if request.method != 'POST':
        return HttpResponseForbidden('Действие доступно только через POST.')
    store = _selected_store(request)
    chat = get_object_or_404(TelegramStoreChat, pk=chat_id, store=store)
    if action == 'delete':
        with transaction.atomic():
            object_id = str(chat.pk)
            chat.delete()
            AuditLog.objects.create(
                actor=request.user,
                store=store,
                object_type=TelegramStoreChat._meta.label_lower,
                object_id=object_id,
                action=AuditLog.Action.TELEGRAM_STORE_CHAT_DELETED,
            )
        messages.success(request, 'Telegram-чат удалён.')
    elif action == 'test':
        enqueue_telegram_message(
            store=store,
            chat_id=chat.chat_id,
            message_thread_id=chat.message_thread_id,
            message_type='test_message',
            idempotency_key=(
                f'chat-test:{chat.pk}:{request.user.pk}:{timezone.now().isoformat()}'
            ),
            payload={'text': f'✅ Тест Telegram: {store.name}'},
        )
        messages.success(request, 'Тест поставлен в очередь.')
    elif action == 'get-chat':
        try:
            result = send_telegram_request('getChat', {'chat_id': chat.chat_id})
        except TelegramAPIError as exc:
            messages.error(request, str(exc))
        else:
            title = (result.data.get('result') or {}).get('title', 'без названия')
            messages.success(request, f'Telegram вернул чат: {title}.')
    else:
        raise Http404
    return redirect('checklists:telegram_chats')


@telegram_settings_required
def telegram_templates(request):
    store = _selected_store(request)
    if store is None:
        raise Http404
    query = TelegramMessageTemplate.objects.filter(store=store)
    category = request.GET.get('category', '')
    event_code = request.GET.get('event', '')
    status = request.GET.get('status', '')
    destination = request.GET.get('destination', '')
    search = request.GET.get('q', '').strip()
    if category in TELEGRAM_CATEGORY_LABELS:
        category_codes = [
            event.code for event in TELEGRAM_EVENTS if event.category == category
        ]
        query = query.filter(event_code__in=category_codes)
    if event_code in TELEGRAM_EVENTS_BY_CODE:
        query = query.filter(event_code=event_code)
    if status == 'enabled':
        query = query.filter(is_enabled=True)
    elif status == 'disabled':
        query = query.filter(is_enabled=False)
    if destination == 'private':
        query = query.filter(send_to_private=True)
    elif destination == 'group':
        query = query.filter(send_to_group=True)
    elif destination == 'both':
        query = query.filter(send_to_private=True, send_to_group=True)
    elif destination == 'none':
        query = query.filter(send_to_private=False, send_to_group=False)
    if search:
        query = query.filter(
            models.Q(name__icontains=search)
            | models.Q(title__icontains=search)
            | models.Q(body__icontains=search)
            | models.Q(event_code__icontains=search)
        )
    grouped = defaultdict(list)
    for template in query:
        template.telegram_event = get_telegram_event(template.event_code)
        grouped[template.telegram_event.category].append(template)
    groups = [
        {
            'code': code,
            'title': label,
            'templates': grouped.get(code, []),
        }
        for code, label in TelegramEventCategory.CHOICES
        if grouped.get(code)
    ]
    used_codes = set(
        TelegramMessageTemplate.objects.filter(store=store).values_list(
            'event_code',
            flat=True,
        )
    )
    return render(
        request,
        'checklists/telegram/templates.html',
        {
            **_base_context(
                request,
                store,
                active_tab='templates',
                breadcrumb_tail=({'title': 'Шаблоны', 'url': None},),
            ),
            'template_groups': groups,
            'category_choices': TelegramEventCategory.CHOICES,
            'event_choices': (
                (event.code, event.title) for event in TELEGRAM_EVENTS
            ),
            'filters': {
                'category': category,
                'event': event_code,
                'status': status,
                'destination': destination,
                'q': search,
            },
            'can_create_template': len(used_codes) < len(TELEGRAM_EVENTS),
            'has_templates': bool(groups),
        },
    )


@telegram_settings_required
def telegram_template_create(request):
    store = _selected_store(request)
    used_codes = set(
        TelegramMessageTemplate.objects.filter(store=store).values_list(
            'event_code',
            flat=True,
        )
    )
    available_events = [
        event for event in TELEGRAM_EVENTS if event.code not in used_codes
    ]
    if not available_events:
        messages.info(request, 'Для всех событий уже созданы шаблоны магазина.')
        return redirect('checklists:telegram_templates')
    requested_event = request.POST.get('event_code') or request.GET.get('event')
    if requested_event not in {event.code for event in available_events}:
        requested_event = available_events[0].code
    initial = {'event_code': requested_event, **template_defaults(requested_event)}
    form = TelegramMessageTemplateCreateForm(
        request.POST or None,
        store=store,
        event_code=requested_event,
        initial=initial,
    )
    if request.method == 'POST' and form.is_valid():
        with transaction.atomic():
            template = form.save(commit=False)
            template.store = store
            template.created_by = request.user
            template.updated_by = request.user
            template.save()
            _audit_template(
                request,
                template,
                AuditLog.Action.TELEGRAM_TEMPLATE_CREATED,
                {'event_code': template.event_code, 'name': template.name},
            )
        messages.success(request, 'Шаблон Telegram создан.')
        return redirect(
            'checklists:telegram_template_edit',
            template_id=template.pk,
        )
    event = get_telegram_event(requested_event)
    return _render_template_editor(
        request,
        store,
        form,
        event,
        title='Создать шаблон',
        template=None,
    )


@telegram_settings_required
def telegram_template_edit(request, template_id):
    store = _selected_store(request)
    template = get_object_or_404(
        TelegramMessageTemplate,
        pk=template_id,
        store=store,
    )
    event = get_telegram_event(template.event_code)
    form = TelegramMessageTemplateForm(
        request.POST or None,
        instance=template,
        event_code=template.event_code,
    )
    if request.method == 'POST' and form.is_valid():
        old_enabled = template.is_enabled
        candidate = form.save(commit=False)
        with transaction.atomic():
            candidate.updated_by = request.user
            candidate.save()
            _audit_template(
                request,
                candidate,
                AuditLog.Action.TELEGRAM_TEMPLATE_UPDATED,
                {
                    'event_code': candidate.event_code,
                    'parse_mode': candidate.parse_mode,
                    'is_enabled': candidate.is_enabled,
                },
            )
            if old_enabled != candidate.is_enabled:
                _audit_template(
                    request,
                    candidate,
                    (
                        AuditLog.Action.TELEGRAM_TEMPLATE_ENABLED
                        if candidate.is_enabled
                        else AuditLog.Action.TELEGRAM_TEMPLATE_DISABLED
                    ),
                    {'event_code': candidate.event_code},
                )
        messages.success(request, 'Шаблон сохранён.')
        return redirect(
            'checklists:telegram_template_edit',
            template_id=template.pk,
        )
    return _render_template_editor(
        request,
        store,
        form,
        event,
        title=template.name,
        template=template,
    )


@telegram_settings_required
def telegram_template_reset(request, template_id):
    store = _selected_store(request)
    template = get_object_or_404(
        TelegramMessageTemplate,
        pk=template_id,
        store=store,
    )
    defaults = template_defaults(template.event_code)
    standard = default_template(store, template.event_code)
    if request.method == 'POST':
        with transaction.atomic():
            for field, value in defaults.items():
                setattr(template, field, value)
            template.updated_by = request.user
            template.save()
            _audit_template(
                request,
                template,
                AuditLog.Action.TELEGRAM_TEMPLATE_RESET,
                {'event_code': template.event_code},
            )
        messages.success(request, 'Стандартный шаблон восстановлен.')
        return redirect(
            'checklists:telegram_template_edit',
            template_id=template.pk,
        )
    return render(
        request,
        'checklists/telegram/template_confirm.html',
        {
            **_base_context(
                request,
                store,
                active_tab='templates',
                breadcrumb_tail=(
                    {
                        'title': 'Шаблоны',
                        'url': reverse('checklists:telegram_templates'),
                    },
                    {'title': 'Восстановление', 'url': None},
                ),
            ),
            'title': 'Восстановить стандартный шаблон?',
            'description': (
                'Текущий заголовок и текст будут заменены стандартными значениями.'
            ),
            'template_obj': template,
            'standard_preview': render_template(
                standard,
                example_context(template.event_code),
            ),
            'confirm_label': 'Восстановить',
            'confirm_class': 'btn-warning',
        },
    )


def _audit_template(request, template, action, value=None, *, object_id=None):
    template_id = object_id or str(template.pk)
    metadata = {
        'store_id': template.store_id,
        'template_id': template_id,
        'event_code': template.event_code,
        'title': template.title,
        'parse_mode': template.parse_mode,
        'channels': {
            'private': template.send_to_private,
            'group': template.send_to_group,
        },
        'actor_id': request.user.pk,
    }
    metadata.update(value or {})
    AuditLog.objects.create(
        actor=request.user,
        store=template.store,
        object_type=TelegramMessageTemplate._meta.label_lower,
        object_id=template_id,
        action=action,
        new_value=metadata,
    )


def _template_destinations(store, template):
    destinations = []
    if template.send_to_private:
        destinations.extend(
            {
                'chat_id': str(binding.telegram_chat_id),
                'thread_id': None,
                'label': f'Личный чат @{binding.username or binding.telegram_user_id}',
            }
            for binding in TelegramStoreBinding.objects.filter(
                store=store,
                is_active=True,
            )
        )
    if template.send_to_group:
        destinations.extend(
            {
                'chat_id': chat.chat_id,
                'thread_id': chat.message_thread_id,
                'label': (
                    f'{chat.title}'
                    + (
                        f' · Topic {chat.message_thread_id}'
                        if chat.message_thread_id is not None
                        else ''
                    )
                ),
            }
            for chat in TelegramStoreChat.objects.filter(
                store=store,
                is_active=True,
            )
        )
    return destinations


def _render_template_editor(request, store, form, event, *, title, template):
    candidate = template or default_template(store, event.code)
    destinations = _template_destinations(store, candidate)
    return render(
        request,
        'checklists/telegram/template_form.html',
        {
            **_base_context(
                request,
                store,
                active_tab='templates',
                breadcrumb_tail=(
                    {
                        'title': 'Шаблоны',
                        'url': reverse('checklists:telegram_templates'),
                    },
                    {'title': title, 'url': None},
                ),
            ),
            'title': title,
            'form': form,
            'telegram_event': event,
            'template_obj': template,
            'destinations': destinations,
            'standard_preview': render_template(
                default_template(store, event.code),
                example_context(event.code),
            ),
        },
    )


@telegram_settings_required
def telegram_template_delete(request, template_id):
    store = _selected_store(request)
    template = get_object_or_404(
        TelegramMessageTemplate,
        pk=template_id,
        store=store,
    )
    if request.method == 'POST':
        object_id = str(template.pk)
        event_code = template.event_code
        with transaction.atomic():
            template.delete()
            _audit_template(
                request,
                template,
                AuditLog.Action.TELEGRAM_TEMPLATE_DELETED,
                {'event_code': event_code, 'fallback_to_default': True},
                object_id=object_id,
            )
        messages.success(
            request,
            'Настройка удалена. Для события используется стандартный шаблон.',
        )
        return redirect('checklists:telegram_templates')
    return render(
        request,
        'checklists/telegram/template_confirm.html',
        {
            **_base_context(
                request,
                store,
                active_tab='templates',
                breadcrumb_tail=(
                    {
                        'title': 'Шаблоны',
                        'url': reverse('checklists:telegram_templates'),
                    },
                    {'title': 'Удаление', 'url': None},
                ),
            ),
            'title': 'Удалить настройку шаблона?',
            'description': (
                'Запись будет удалена безвозвратно. Отправка продолжит работать '
                'со стандартным шаблоном этого события.'
            ),
            'template_obj': template,
            'confirm_label': 'Удалить настройку',
            'confirm_class': 'btn-danger',
        },
    )


@telegram_settings_required
def telegram_template_toggle(request, template_id):
    if request.method != 'POST':
        return HttpResponseForbidden('Действие доступно только через POST.')
    store = _selected_store(request)
    with transaction.atomic():
        template = get_object_or_404(
            TelegramMessageTemplate.objects.select_for_update(),
            pk=template_id,
            store=store,
        )
        template.is_enabled = not template.is_enabled
        template.updated_by = request.user
        template.save(update_fields=('is_enabled', 'updated_by', 'updated_at'))
        _audit_template(
            request,
            template,
            (
                AuditLog.Action.TELEGRAM_TEMPLATE_ENABLED
                if template.is_enabled
                else AuditLog.Action.TELEGRAM_TEMPLATE_DISABLED
            ),
            {'event_code': template.event_code},
        )
    messages.success(
        request,
        'Шаблон включён.' if template.is_enabled else 'Шаблон выключен.',
    )
    return redirect('checklists:telegram_templates')


@telegram_settings_required
def telegram_template_preview(request, template_id=None):
    if request.method != 'POST':
        return JsonResponse({'error': 'Доступен только POST.'}, status=405)
    store = _selected_store(request)
    template = None
    if template_id is not None:
        template = get_object_or_404(
            TelegramMessageTemplate,
            pk=template_id,
            store=store,
        )
        event_code = template.event_code
        form = TelegramMessageTemplateForm(
            request.POST,
            instance=template,
            event_code=event_code,
        )
    else:
        event_code = request.POST.get('event_code')
        form = TelegramMessageTemplateCreateForm(
            request.POST,
            store=store,
            event_code=event_code,
        )
    if not form.is_valid():
        return JsonResponse(
            {'errors': form.errors.get_json_data()},
            status=400,
        )
    candidate = form.save(commit=False)
    candidate.store = store
    return JsonResponse(
        {
            'preview': render_template(
                candidate,
                example_context(candidate.event_code),
            )
        }
    )


@telegram_settings_required
def telegram_template_test(request, template_id):
    if request.method != 'POST':
        return HttpResponseForbidden('Действие доступно только через POST.')
    store = _selected_store(request)
    template = get_object_or_404(
        TelegramMessageTemplate,
        pk=template_id,
        store=store,
    )
    form = TelegramMessageTemplateForm(
        request.POST,
        instance=template,
        event_code=template.event_code,
    )
    if not form.is_valid():
        return _render_template_editor(
            request,
            store,
            form,
            get_telegram_event(template.event_code),
            title=template.name,
            template=template,
        )
    candidate = form.save(commit=False)
    preview = render_template(candidate, example_context(candidate.event_code))
    destinations = _template_destinations(store, candidate)
    if not destinations:
        form.add_error(None, 'Нет активных получателей для тестовой отправки.')
        return _render_template_editor(
            request,
            store,
            form,
            get_telegram_event(template.event_code),
            title=template.name,
            template=template,
        )
    queued = []
    for destination in destinations:
        queued.append(
            enqueue_telegram_message(
                store=store,
                chat_id=destination['chat_id'],
                message_thread_id=destination['thread_id'],
                message_type='template_test',
                idempotency_key=(
                    f'template-test:{template.pk}:{request.user.pk}:'
                    f'{timezone.now().isoformat()}:{destination["chat_id"]}:'
                    f'{destination["thread_id"] or 0}'
                ),
                payload={
                    'text': preview,
                    **(
                        {}
                        if candidate.parse_mode
                        == TelegramMessageTemplate.ParseMode.PLAIN
                        else {'parse_mode': candidate.parse_mode}
                    ),
                },
            )
        )
    _audit_template(
        request,
        template,
        AuditLog.Action.TELEGRAM_TEMPLATE_TEST_SENT,
        {
            'event_code': template.event_code,
            'queued_count': len(queued),
            'has_topic': any(item['thread_id'] for item in destinations),
        },
    )
    messages.success(
        request,
        f'Тестовых сообщений поставлено в очередь: {len(queued)}.',
    )
    return redirect(
        'checklists:telegram_template_edit',
        template_id=template.pk,
    )


@telegram_settings_required
def telegram_users(request):
    if not request.telegram_is_system_admin:
        return HttpResponseForbidden('Привязки доступны только администратору.')
    store = _selected_store(request, required=False)
    pending = TelegramPendingBinding.objects.filter(
        status=TelegramPendingBinding.Status.PENDING,
    )
    bindings = TelegramStoreBinding.objects.select_related('store', 'approved_by')
    profiles = TelegramUserProfile.objects.select_related('user').prefetch_related(
        'user__store_memberships__store'
    )
    if store:
        bindings = bindings.filter(store=store)
    return render(
        request,
        'checklists/telegram/users.html',
        {
            **_base_context(
                request,
                store,
                active_tab='users',
                breadcrumb_tail=({'title': 'Привязки', 'url': None},),
            ),
            'pending_bindings': pending,
            'bindings': bindings,
            'telegram_profiles': profiles,
            'approval_form': TelegramBindingApprovalForm(),
            'profile_user_form': TelegramProfileUserForm(),
        },
    )


@telegram_settings_required
def telegram_binding_action(request, binding_id, action):
    if not request.telegram_is_system_admin:
        return HttpResponseForbidden('Недостаточно прав.')
    if request.method != 'POST':
        return HttpResponseForbidden('Действие доступно только через POST.')
    if action == 'approve':
        pending = get_object_or_404(TelegramPendingBinding, pk=binding_id)
        form = TelegramBindingApprovalForm(request.POST)
        if form.is_valid():
            try:
                approve_pending_binding(
                    pending=pending,
                    store=form.cleaned_data['store'],
                    actor=request.user,
                    user=form.cleaned_data.get('user'),
                )
            except (ValidationError, ChecklistServiceError) as exc:
                messages.error(request, str(exc))
            else:
                messages.success(request, 'Привязка подтверждена.')
    elif action == 'disable':
        binding = get_object_or_404(TelegramStoreBinding, pk=binding_id)
        disable_telegram_binding(binding=binding, actor=request.user)
        messages.success(request, 'Привязка отключена.')
    elif action == 'transfer':
        binding = get_object_or_404(TelegramStoreBinding, pk=binding_id)
        form = TelegramBindingApprovalForm(request.POST)
        if form.is_valid():
            binding = transfer_telegram_binding(
                binding=binding,
                store=form.cleaned_data['store'],
                actor=request.user,
            )
            if form.cleaned_data.get('user'):
                link_telegram_user(
                    binding=binding,
                    user=form.cleaned_data['user'],
                )
            messages.success(request, 'Привязка перенесена.')
    elif action == 'reject':
        pending = get_object_or_404(
            TelegramPendingBinding,
            pk=binding_id,
            status=TelegramPendingBinding.Status.PENDING,
        )
        pending.status = TelegramPendingBinding.Status.REJECTED
        pending.save(update_fields=('status',))
        messages.success(request, 'Заявка отклонена.')
    else:
        raise Http404
    return redirect('checklists:telegram_users')


@telegram_settings_required
def telegram_profile_action(request, profile_id, action):
    if not request.telegram_is_system_admin:
        return HttpResponseForbidden('Недостаточно прав.')
    if request.method != 'POST':
        return HttpResponseForbidden('Действие доступно только через POST.')
    profile = get_object_or_404(TelegramUserProfile, pk=profile_id)
    try:
        if action == 'reassign':
            form = TelegramProfileUserForm(request.POST)
            if not form.is_valid():
                raise ValidationError('Выберите пользователя.')
            reassign_telegram_profile(
                profile=profile,
                user=form.cleaned_data['user'],
                actor=request.user,
            )
            messages.success(request, 'Пользователь Telegram изменён.')
        elif action == 'disconnect':
            disconnect_telegram_profile(
                profile=profile,
                actor=request.user,
            )
            messages.success(request, 'Telegram отвязан от пользователя.')
        else:
            raise Http404
    except (ValidationError, ChecklistServiceError) as exc:
        messages.error(request, str(exc))
    return redirect('checklists:telegram_users')


@telegram_settings_required
def telegram_queue(request):
    store = _selected_store(request, required=False)
    queue_type = request.GET.get('type', 'outbound')
    query = TelegramOutboundMessage.objects.select_related(
        'store',
        'deleted_by',
    )
    inbound = TelegramInboundJob.objects.select_related('store', 'update_log')
    updates = TelegramUpdateLog.objects.all()
    filtered_store = store if not request.telegram_is_system_admin else None
    selected_store_filter = request.GET.get('store', '').strip()
    if request.telegram_is_system_admin and selected_store_filter:
        filtered_store = get_object_or_404(Store, pk=selected_store_filter)
    if not request.telegram_is_system_admin:
        query = query.filter(store=store)
        inbound = inbound.filter(store=store)
        updates = updates.filter(
            models.Q(inbound_job__store=store)
            | models.Q(
                telegram_user_id__in=TelegramStoreBinding.objects.filter(
                    store=store,
                    is_active=True,
                ).values('telegram_user_id')
            )
        )
    elif filtered_store:
        query = query.filter(store=filtered_store)
        inbound = inbound.filter(store=filtered_store)
        updates = updates.filter(
            models.Q(inbound_job__store=filtered_store)
            | models.Q(
                telegram_user_id__in=TelegramStoreBinding.objects.filter(
                    store=filtered_store,
                    is_active=True,
                ).values('telegram_user_id')
            )
        )
    status = request.GET.get('status')
    if status in TelegramOutboundMessage.Status.values:
        query = query.filter(status=status)
    selected_user = request.GET.get('user', '').strip()
    if selected_user:
        profile = get_object_or_404(TelegramUserProfile, user_id=selected_user)
        query = query.filter(chat_id=str(profile.telegram_chat_id))
    for field_name, lookup in (
        ('date_from', 'created_at__date__gte'),
        ('date_to', 'created_at__date__lte'),
    ):
        value = request.GET.get(field_name, '').strip()
        try:
            parsed = date.fromisoformat(value) if value else None
        except ValueError:
            parsed = None
        if parsed:
            query = query.filter(**{lookup: parsed})
    user_profiles = TelegramUserProfile.objects.select_related('user')
    if not request.telegram_is_system_admin:
        user_profiles = user_profiles.filter(
            telegram_user_id__in=TelegramStoreBinding.objects.filter(
                store=store,
                is_active=True,
            ).values('telegram_user_id')
        )
    outbound_messages = list(query.order_by('-created_at')[:200])
    recipient_by_chat = {
        str(item.telegram_chat_id): item.user.get_username()
        for item in user_profiles
    }
    for message in outbound_messages:
        message.recipient_label = recipient_by_chat.get(
            str(message.chat_id),
            message.chat_id,
        )
    return render(
        request,
        'checklists/telegram/queue.html',
        {
            **_base_context(
                request,
                store,
                active_tab='queue',
                breadcrumb_tail=({'title': 'Очередь', 'url': None},),
            ),
            'outbound_messages': outbound_messages,
            'selected_status': status,
            'selected_user': selected_user,
            'selected_store_filter': selected_store_filter,
            'date_from': request.GET.get('date_from', ''),
            'date_to': request.GET.get('date_to', ''),
            'message_users': user_profiles.order_by('user__username'),
            'message_stores': (
                Store.objects.order_by('name')
                if request.telegram_is_system_admin
                else Store.objects.none()
            ),
            'status_choices': TelegramOutboundMessage.Status.choices,
            'inbound_jobs': inbound.order_by('-created_at')[:200],
            'telegram_updates': updates.distinct().order_by('-created_at')[:200],
            'inbound_status_choices': TelegramInboundJob.Status.choices,
            'queue_type': queue_type,
        },
    )


@telegram_settings_required
def telegram_queue_retry(request, message_id):
    if request.method != 'POST':
        return HttpResponseForbidden('Действие доступно только через POST.')
    store = _selected_store(request)
    query = TelegramOutboundMessage.objects.filter(
        pk=message_id,
        status=TelegramOutboundMessage.Status.FAILED,
    )
    if not request.telegram_is_system_admin:
        query = query.filter(store=store)
    message = get_object_or_404(query)
    message.status = TelegramOutboundMessage.Status.PENDING
    message.last_error = ''
    message.scheduled_at = timezone.now()
    message.save(
        update_fields=('status', 'last_error', 'scheduled_at', 'updated_at')
    )
    messages.success(request, 'Сообщение возвращено в очередь.')
    return redirect('checklists:telegram_queue')


@telegram_settings_required
def telegram_message_delete(request, message_id):
    if request.method != 'POST':
        return HttpResponseForbidden('Действие доступно только через POST.')
    store = _selected_store(request, required=not request.telegram_is_system_admin)
    query = TelegramOutboundMessage.objects.filter(pk=message_id)
    if not request.telegram_is_system_admin:
        query = query.filter(store=store)
    message = get_object_or_404(query)
    try:
        delete_telegram_message(message, actor=request.user)
    except TelegramAPIError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, 'Сообщение удалено в Telegram.')
    return redirect('checklists:telegram_queue')
