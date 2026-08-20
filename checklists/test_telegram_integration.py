import json
from datetime import date, datetime, time, timedelta
from io import BytesIO, StringIO
from urllib import error as urllib_error
from zoneinfo import ZoneInfo

import pytest
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from checklists.ad_hoc_tasks import (
    create_ad_hoc_task,
    is_ad_hoc_stage_closed,
)
from checklists.exceptions import ChecklistLockedError, OperationNotAllowedError
from checklists.models import (
    AuditLog,
    ChecklistItem,
    ChecklistSection,
    ChecklistTemplate,
    ChecklistTemplateVersion,
    DailyChecklist,
    DailyChecklistStage,
    EmployeeProfile,
    Store,
    StoreAdHocTask,
    StoreChecklistSchedule,
    StoreEmployee,
    StoreTerminalAccount,
    TelegramConversationState,
    TelegramMessageTemplate,
    TelegramInboundJob,
    TelegramOutboundMessage,
    TelegramPendingBinding,
    TelegramStoreBinding,
    TelegramStoreChat,
    TelegramSystemSettings,
    TelegramUpdateLog,
)
from checklists.telegram_events import TELEGRAM_EVENTS, get_telegram_event
from checklists.services import create_daily_checklist, publish_template_version
from checklists.services import update_answer
from checklists.exceptions import AnswerValidationError
from checklists.telegram_bot import process_telegram_update
from checklists.telegram_client import (
    TelegramAPIError,
    TelegramResponse,
    send_telegram_request,
)
from checklists.telegram_queue import (
    enqueue_telegram_message,
    enqueue_template_message,
    process_telegram_queue,
)
from checklists.telegram_inbound import process_inbound_queue
from checklists.telegram_reminders import schedule_telegram_notifications
from checklists.telegram_services import approve_pending_binding
from checklists.telegram_templates import (
    default_template,
    render_template,
    template_defaults,
    validate_template_source,
)


pytestmark = pytest.mark.django_db


def configure_bot(**overrides):
    values = {
        'bot_token': '123456:super-secret-token',
        'is_enabled': True,
        'retry_delay_seconds': 0,
        'alternative_attempts': 5,
        'official_attempts': 5,
    }
    values.update(overrides)
    return TelegramSystemSettings.objects.create(**values)


def make_store(code='tg-store'):
    store = Store.objects.create(name=f'Магазин {code}', code=code)
    StoreChecklistSchedule.objects.create(store=store)
    return store


def make_admin(username='tg-admin'):
    return User.objects.create_superuser(username, f'{username}@example.com', 'Strong-934!')


def make_director(store, username='tg-director'):
    user = User.objects.create_user(username, password='Strong-934!')
    EmployeeProfile.objects.create(
        user=user,
        store=store,
        role=EmployeeProfile.Role.STORE_DIRECTOR,
        is_active=True,
    )
    return user


def make_message_template(store, event_code='test_message', **overrides):
    values = template_defaults(event_code)
    values.update(overrides)
    return TelegramMessageTemplate.objects.create(
        store=store,
        event_code=event_code,
        **values,
    )


def template_form_data(template, **overrides):
    values = {
        'name': template.name,
        'title': template.title,
        'body': template.body,
        'parse_mode': template.parse_mode,
        'is_enabled': 'on' if template.is_enabled else '',
        'send_to_private': 'on' if template.send_to_private else '',
        'send_to_group': 'on' if template.send_to_group else '',
    }
    values.update(overrides)
    return values


def make_terminal_daily(store, work_date):
    user = User.objects.create_user(f'terminal-{store.code}', password='Strong-934!')
    profile = EmployeeProfile.objects.create(
        user=user,
        store=store,
        role=EmployeeProfile.Role.STORE_ACCOUNT,
        is_active=True,
    )
    terminal = StoreTerminalAccount.objects.create(store=store, user=user)
    template = ChecklistTemplate.objects.create(store=store, name='Telegram')
    version = ChecklistTemplateVersion.objects.create(
        template=template,
        version_number=1,
        created_by=user,
    )
    for position, (code, label) in enumerate(
        DailyChecklistStage.SectionCode.choices
    ):
        section = ChecklistSection.objects.create(
            version=version,
            name=label,
            code=code,
            sort_order=position,
        )
        ChecklistItem.objects.create(
            section=section,
            text=f'{label}: основной пункт',
            sort_order=1,
        )
    publisher = User.objects.create_superuser(
        f'publisher-{store.code}',
        f'publisher-{store.code}@example.com',
        'Strong-934!',
    )
    publish_template_version(version, publisher)
    return create_daily_checklist(terminal, work_date), terminal


def message_update(update_id, user_id, chat_id, text):
    return {
        'update_id': update_id,
        'message': {
            'message_id': update_id,
            'from': {
                'id': user_id,
                'username': f'user{user_id}',
                'first_name': 'Иван',
            },
            'chat': {'id': chat_id, 'type': 'private'},
            'text': text,
        },
    }


def callback_update(update_id, user_id, chat_id, data):
    return {
        'update_id': update_id,
        'callback_query': {
            'id': f'callback-{update_id}',
            'from': {'id': user_id, 'username': f'user{user_id}'},
            'message': {'chat': {'id': chat_id, 'type': 'private'}},
            'data': data,
        },
    }


class TelegramHTTPResponse:
    status = 200

    def __init__(self, payload=None):
        self.payload = payload or {'ok': True, 'result': {'message_id': 1}}

    def read(self):
        return json.dumps(self.payload).encode()

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False


def assert_telegram_headers(api_request):
    assert (
        api_request.get_header('User-agent')
        == 'Mozilla/5.0 TelegramGateway/1.0'
    )
    assert api_request.get_header('Accept') == 'application/json'
    assert api_request.get_header('Content-type') == 'application/json'


def test_system_settings_mask_token():
    config = configure_bot()
    assert config.masked_token == '1234…oken'
    assert config.bot_token not in config.masked_token


def test_alternative_gateway_request_has_user_agent(monkeypatch):
    config = configure_bot()
    requests = []

    def transport(api_request, timeout):
        requests.append(api_request)
        return TelegramHTTPResponse({'ok': True, 'result': {'id': 1}})

    monkeypatch.setattr('checklists.telegram_client.request.urlopen', transport)
    send_telegram_request('getMe', {}, system_settings=config)
    assert requests[0].full_url.startswith('https://tauto.gerbud.ru/')
    assert_telegram_headers(requests[0])


def test_official_api_request_has_user_agent(monkeypatch):
    config = configure_bot(use_alternative_gateway=False)
    requests = []

    def transport(api_request, timeout):
        requests.append(api_request)
        return TelegramHTTPResponse({'ok': True, 'result': {'id': 1}})

    monkeypatch.setattr('checklists.telegram_client.request.urlopen', transport)
    send_telegram_request('getMe', {}, system_settings=config)
    assert requests[0].full_url.startswith('https://api.telegram.org/')
    assert_telegram_headers(requests[0])


def test_get_me_uses_get_with_common_headers(monkeypatch):
    config = configure_bot()
    requests = []

    def transport(api_request, timeout):
        requests.append(api_request)
        return TelegramHTTPResponse({'ok': True, 'result': {'id': 1}})

    monkeypatch.setattr('checklists.telegram_client.request.urlopen', transport)
    send_telegram_request('getMe', {}, system_settings=config)
    assert requests[0].get_method() == 'GET'
    assert requests[0].data is None
    assert_telegram_headers(requests[0])


def test_send_message_uses_post_with_common_headers(monkeypatch):
    config = configure_bot()
    requests = []

    def transport(api_request, timeout):
        requests.append(api_request)
        return TelegramHTTPResponse()

    monkeypatch.setattr('checklists.telegram_client.request.urlopen', transport)
    send_telegram_request(
        'sendMessage',
        {'chat_id': '1', 'text': 'test'},
        system_settings=config,
    )
    assert requests[0].get_method() == 'POST'
    assert json.loads(requests[0].data) == {'chat_id': '1', 'text': 'test'}
    assert_telegram_headers(requests[0])


def test_token_is_redacted_from_http_error_and_log(monkeypatch, caplog):
    config = configure_bot()

    def transport(api_request, timeout):
        body = json.dumps(
            {
                'ok': False,
                'error_code': 1010,
                'description': f'Cloudflare denied token {config.bot_token}',
            }
        ).encode()
        raise urllib_error.HTTPError(
            api_request.full_url,
            403,
            'Forbidden',
            {},
            BytesIO(body),
        )

    monkeypatch.setattr('checklists.telegram_client.request.urlopen', transport)
    caplog.set_level('WARNING', logger='checklists.telegram_client')
    with pytest.raises(TelegramAPIError) as caught:
        send_telegram_request(
            'getMe',
            {},
            system_settings=config,
            sleeper=lambda delay: None,
        )
    error_message = str(caught.value)
    assert 'http_status=403' in error_message
    assert 'error_code=1010' in error_message
    assert 'description=Cloudflare denied token [REDACTED]' in error_message
    assert config.bot_token not in error_message
    assert config.bot_token not in caplog.text
    assert '/bot123456' not in error_message
    assert '/bot123456' not in caplog.text


def test_alternative_gateway_retries_five_then_official(monkeypatch):
    config = configure_bot()
    calls = []

    def attempt(base_url, token, method, payload, timeout):
        calls.append(base_url)
        if base_url == 'https://api.telegram.org':
            return {'ok': True, 'result': {'message_id': 7}}, None
        return None, 'network error'

    monkeypatch.setattr('checklists.telegram_client._safe_attempt', attempt)
    response = send_telegram_request(
        'sendMessage',
        {'chat_id': '1', 'text': 'test'},
        system_settings=config,
        sleeper=lambda delay: None,
    )
    assert response.alternative_attempts == 5
    assert response.official_attempts == 1
    assert calls[:5] == ['https://tauto.gerbud.ru'] * 5
    assert calls[5] == 'https://api.telegram.org'


def test_successful_alternative_does_not_use_fallback(monkeypatch):
    config = configure_bot()
    calls = []
    monkeypatch.setattr(
        'checklists.telegram_client._safe_attempt',
        lambda base, *args: (
            calls.append(base) or {'ok': True, 'result': {'message_id': 1}},
            None,
        ),
    )
    response = send_telegram_request(
        'sendMessage',
        {'chat_id': '1', 'text': 'ok'},
        system_settings=config,
        sleeper=lambda delay: None,
    )
    assert response.alternative_attempts == 1
    assert response.official_attempts == 0
    assert calls == ['https://tauto.gerbud.ru']


def test_quick_request_falls_back_to_official_api_once(monkeypatch):
    config = configure_bot()
    calls = []
    delays = []

    def attempt(base_url, token, method, payload, timeout):
        calls.append((base_url, timeout))
        if base_url == 'https://api.telegram.org':
            return {'ok': True, 'result': {'message_id': 8}}, None
        return None, 'description=gateway unavailable'

    monkeypatch.setattr('checklists.telegram_client._safe_attempt', attempt)
    response = send_telegram_request(
        'sendMessage',
        {'chat_id': '1', 'text': 'быстрый ответ'},
        system_settings=config,
        quick=True,
        sleeper=delays.append,
    )
    assert response.alternative_attempts == 1
    assert response.official_attempts == 1
    assert [base_url for base_url, _ in calls] == [
        'https://tauto.gerbud.ru',
        'https://api.telegram.org',
    ]
    assert all(timeout <= 1.5 for _, timeout in calls)
    assert delays == []


def test_non_idempotent_request_does_not_retry_after_ambiguous_failure(monkeypatch):
    config = configure_bot()
    calls = []

    def attempt(base_url, token, method, payload, timeout):
        calls.append(base_url)
        return None, 'description=request timeout'

    monkeypatch.setattr('checklists.telegram_client._safe_attempt', attempt)
    with pytest.raises(TelegramAPIError) as caught:
        send_telegram_request(
            'sendMessage',
            {'chat_id': '1', 'text': 'не дублировать'},
            system_settings=config,
            quick=True,
            retry_on_failure=False,
        )

    assert caught.value.alternative_attempts == 1
    assert caught.value.official_attempts == 0
    assert calls == ['https://tauto.gerbud.ru']


def test_failed_after_ten_attempts_never_exposes_token(monkeypatch):
    config = configure_bot()
    monkeypatch.setattr(
        'checklists.telegram_client._safe_attempt',
        lambda *args: (None, 'ok=false'),
    )
    with pytest.raises(TelegramAPIError) as caught:
        send_telegram_request(
            'sendMessage',
            {'chat_id': '1', 'text': 'x'},
            system_settings=config,
            sleeper=lambda delay: None,
        )
    assert caught.value.alternative_attempts == 5
    assert caught.value.official_attempts == 5
    assert config.bot_token not in str(caught.value)


def test_queue_is_idempotent_and_message_thread_is_in_payload():
    store = make_store()
    first = enqueue_telegram_message(
        store=store,
        chat_id='-1001',
        message_thread_id=42,
        message_type='test_message',
        idempotency_key='same-key',
        payload={'text': 'test'},
    )
    second = enqueue_telegram_message(
        store=store,
        chat_id='-1001',
        message_thread_id=42,
        message_type='test_message',
        idempotency_key='same-key',
        payload={'text': 'duplicate'},
    )
    assert first.pk == second.pk
    assert TelegramOutboundMessage.objects.count() == 1
    assert first.payload['message_thread_id'] == 42


def test_queue_processes_once(monkeypatch):
    store = make_store()
    message = enqueue_telegram_message(
        store=store,
        chat_id='1',
        message_type='test_message',
        idempotency_key='process-once',
        payload={'text': 'test'},
    )
    calls = []

    def sender(method, payload):
        calls.append((method, payload))
        return TelegramResponse(
            {'ok': True, 'result': {'message_id': 99}},
            alternative_attempts=1,
            official_attempts=0,
        )

    monkeypatch.setattr('checklists.telegram_queue.send_telegram_request', sender)
    assert process_telegram_queue()['sent'] == 1
    assert process_telegram_queue()['claimed'] == 0
    message.refresh_from_db()
    assert message.status == TelegramOutboundMessage.Status.SENT
    assert message.telegram_message_id == 99
    assert len(calls) == 1


def test_queue_failed_error_is_safe(monkeypatch):
    store = make_store()
    message = enqueue_telegram_message(
        store=store,
        chat_id='1',
        message_type='test_message',
        idempotency_key='failed-safe',
        payload={'text': 'test'},
    )
    monkeypatch.setattr(
        'checklists.telegram_queue.send_telegram_request',
        lambda *args: (_ for _ in ()).throw(
            TelegramAPIError(
                'Telegram delivery failed: network error.',
                alternative_attempts=5,
                official_attempts=5,
            )
        ),
    )
    assert process_telegram_queue()['failed'] == 1
    message.refresh_from_db()
    assert message.status == TelegramOutboundMessage.Status.FAILED
    assert 'super-secret-token' not in message.last_error


def test_stale_processing_message_is_returned_to_queue_before_retry(monkeypatch):
    message = enqueue_telegram_message(
        chat_id='1',
        message_type='test_message',
        idempotency_key='stale-processing',
        payload={'text': 'test'},
    )
    TelegramOutboundMessage.objects.filter(pk=message.pk).update(
        status=TelegramOutboundMessage.Status.PROCESSING,
        updated_at=timezone.now() - timedelta(minutes=6),
    )
    calls = []
    monkeypatch.setattr(
        'checklists.telegram_queue.send_telegram_request',
        lambda *args: calls.append(args),
    )

    result = process_telegram_queue()

    message.refresh_from_db()
    assert result['recovered'] == 1
    assert result['claimed'] == 0
    assert message.status == TelegramOutboundMessage.Status.PENDING
    assert message.last_error == 'Message processing timeout, returned to queue'
    assert calls == []


def test_start_creates_pending_binding_and_update_is_once():
    update = message_update(100, 700, 700, '/start')
    assert process_telegram_update(update) == 'processed'
    assert process_telegram_update(update) == 'duplicate'
    pending = TelegramPendingBinding.objects.get()
    assert pending.telegram_user_id == 700
    assert pending.status == TelegramPendingBinding.Status.PENDING
    assert len(pending.one_time_code) == 6
    assert TelegramUpdateLog.objects.count() == 1


def test_only_system_admin_can_approve_binding():
    store = make_store()
    director = make_director(store)
    pending = TelegramPendingBinding.objects.create(
        telegram_user_id=701,
        telegram_chat_id=701,
        one_time_code='123456',
        update_id=101,
        expires_at=timezone.now() + timedelta(minutes=10),
    )
    with pytest.raises(OperationNotAllowedError):
        approve_pending_binding(pending=pending, store=store, actor=director)
    binding = approve_pending_binding(
        pending=pending,
        store=store,
        actor=make_admin(),
    )
    assert binding.is_active
    assert binding.store == store


def test_inactive_binding_cannot_use_bot():
    store = make_store()
    TelegramStoreBinding.objects.create(
        store=store,
        telegram_user_id=702,
        telegram_chat_id=702,
        is_active=False,
    )
    process_telegram_update(message_update(102, 702, 702, '/task'))
    assert not TelegramConversationState.objects.exists()


def test_stepwise_task_dialog_creates_single_task():
    store = make_store()
    linked_user = User.objects.create_user(username='telegram-task-author')
    binding = TelegramStoreBinding.objects.create(
        store=store,
        telegram_user_id=703,
        telegram_chat_id=703,
        user=linked_user,
    )
    tomorrow = (
        timezone.now().astimezone(ZoneInfo(store.timezone)).date()
        + timedelta(days=1)
    )
    assert process_telegram_update(message_update(110, 703, 703, '/task')) == 'processed'
    process_telegram_update(callback_update(111, 703, 703, 'task:date:tomorrow'))
    process_telegram_update(callback_update(112, 703, 703, 'task:section:morning'))
    process_telegram_update(message_update(113, 703, 703, 'Проверить витрину'))
    process_telegram_update(
        callback_update(114, 703, 703, 'task:skip-description')
    )
    confirm = callback_update(115, 703, 703, 'task:confirm')
    assert process_telegram_update(confirm) == 'processed'
    assert process_telegram_update(confirm) == 'duplicate'
    task = StoreAdHocTask.objects.get()
    assert task.store == store
    assert task.date == tomorrow
    assert task.source == StoreAdHocTask.Source.TELEGRAM
    assert task.created_by_telegram_binding == binding
    assert task.created_by == linked_user


def test_closed_stage_rejects_spoofed_callback():
    store = make_store()
    daily, _ = make_terminal_daily(store, timezone.localdate() + timedelta(days=1))
    stage = daily.stages.get(
        section_code=DailyChecklistStage.SectionCode.OPENING
    )
    DailyChecklistStage.objects.filter(pk=stage.pk).update(
        status=DailyChecklistStage.Status.COMPLETED,
        completed_at=timezone.now(),
    )
    binding = TelegramStoreBinding.objects.create(
        store=store,
        telegram_user_id=704,
        telegram_chat_id=704,
    )
    _ = binding
    process_telegram_update(message_update(120, 704, 704, '/task'))
    custom_date = callback_update(121, 704, 704, 'task:date:custom')
    process_telegram_update(custom_date)
    process_telegram_update(
        message_update(122, 704, 704, daily.checklist_date.isoformat())
    )
    process_telegram_update(
        callback_update(123, 704, 704, 'task:section:morning')
    )
    assert not StoreAdHocTask.objects.exists()


@pytest.mark.parametrize(
    ('section_code', 'daily_code'),
    (
        ('morning', DailyChecklistStage.SectionCode.OPENING),
        ('day', DailyChecklistStage.SectionCode.DURING_DAY),
        ('evening', DailyChecklistStage.SectionCode.CLOSING),
    ),
)
def test_all_three_closed_stages_are_detected(section_code, daily_code):
    store = make_store()
    daily, _ = make_terminal_daily(store, timezone.localdate() + timedelta(days=1))
    DailyChecklistStage.objects.filter(
        daily_checklist=daily,
        section_code=daily_code,
    ).update(
        status=DailyChecklistStage.Status.COMPLETED,
        completed_at=timezone.now(),
    )
    assert is_ad_hoc_stage_closed(store, daily.checklist_date, section_code)


def test_task_is_inserted_into_existing_daily_without_template_change():
    store = make_store()
    daily, _ = make_terminal_daily(store, timezone.localdate() + timedelta(days=1))
    template_items = ChecklistItem.objects.count()
    task = create_ad_hoc_task(
        store=store,
        date=daily.checklist_date,
        section_code=StoreAdHocTask.SectionCode.MORNING,
        text='Разовая проверка',
    )
    assert task.daily_checklist == daily
    assert task.daily_item.source_item is None
    assert task.daily_item.answer.status == 'pending'
    assert ChecklistItem.objects.count() == template_items


def test_task_is_included_when_daily_is_created_later():
    store = make_store()
    tomorrow = timezone.localdate() + timedelta(days=1)
    task = create_ad_hoc_task(
        store=store,
        date=tomorrow,
        section_code='morning',
        text='Будущая разовая задача',
    )
    assert task.daily_item_id is None
    daily, _ = make_terminal_daily(store, tomorrow)
    task.refresh_from_db()
    assert task.daily_checklist == daily
    assert task.daily_item.answer.status == 'pending'


def test_task_completion_records_employee_and_queues_message():
    store = make_store()
    daily, terminal = make_terminal_daily(
        store,
        timezone.localdate() + timedelta(days=1),
    )
    employee = StoreEmployee.objects.create(
        store=store,
        first_name='Анна',
        display_name='Анна',
    )
    TelegramStoreChat.objects.create(
        store=store,
        title='Задачи',
        chat_id='-100903',
        chat_type='supergroup',
        purpose='tasks',
    )
    task = create_ad_hoc_task(
        store=store,
        date=daily.checklist_date,
        section_code='morning',
        text='Завершить выкладку',
    )
    answer = task.daily_item.answer
    stage = task.daily_stage
    update_answer(
        answer,
        'completed',
        'Готово',
        terminal.user,
        employee=employee,
        at=stage.opens_at + timedelta(minutes=1),
    )
    task.refresh_from_db()
    assert task.status == StoreAdHocTask.Status.COMPLETED
    assert task.completed_by_employee == employee
    assert TelegramOutboundMessage.objects.filter(
        message_type='task_completed',
    ).exists()


def test_failed_task_requires_comment_and_queues_failure():
    store = make_store()
    daily, terminal = make_terminal_daily(
        store,
        timezone.localdate() + timedelta(days=1),
    )
    employee = StoreEmployee.objects.create(
        store=store,
        first_name='Пётр',
        display_name='Пётр',
    )
    TelegramStoreChat.objects.create(
        store=store,
        title='Ошибки',
        chat_id='-100904',
        chat_type='supergroup',
        purpose='failures',
    )
    task = create_ad_hoc_task(
        store=store,
        date=daily.checklist_date,
        section_code='day',
        text='Проверить поставку',
    )
    answer = task.daily_item.answer
    with pytest.raises(AnswerValidationError):
        update_answer(
            answer,
            'failed',
            '',
            terminal.user,
            employee=employee,
            at=task.daily_stage.opens_at + timedelta(minutes=1),
        )
    update_answer(
        answer,
        'failed',
        'Поставка не приехала',
        terminal.user,
        employee=employee,
        at=task.daily_stage.opens_at + timedelta(minutes=1),
    )
    task.refresh_from_db()
    assert task.status == StoreAdHocTask.Status.FAILED
    assert task.completion_comment == 'Поставка не приехала'
    assert TelegramOutboundMessage.objects.filter(
        message_type='task_failed',
    ).exists()


def test_unknown_template_variable_is_rejected():
    with pytest.raises(ValidationError):
        validate_template_source('Ошибка {secret_token}')


def test_template_values_are_escaped_for_html_and_markdown():
    store = make_store()
    template = default_template(store, 'task_completed')
    template.title = '{task_text}'
    template.body = '{comment}'
    template.parse_mode = TelegramMessageTemplate.ParseMode.HTML
    assert '&lt;b&gt;' in render_template(
        template,
        {'task_text': '<b>', 'comment': '&'},
    )
    template.parse_mode = TelegramMessageTemplate.ParseMode.MARKDOWN_V2
    rendered = render_template(
        template,
        {'task_text': '[важно]', 'comment': 'готово!'},
    )
    assert r'\[важно\]' in rendered
    assert r'готово\!' in rendered


def test_new_store_uses_defaults_without_materialized_templates():
    first = make_store('first-tg')
    second = make_store('second-tg')
    assert first.telegram_message_templates.count() == 0
    assert second.telegram_message_templates.count() == 0
    assert default_template(first, 'test_message').store == first
    assert not first.telegram_message_templates.filter(store=second).exists()


def test_template_routing_supports_topic():
    store = make_store()
    chat = TelegramStoreChat.objects.create(
        store=store,
        title='Задачи',
        chat_id='-100900',
        chat_type=TelegramStoreChat.ChatType.SUPERGROUP,
        message_thread_id=77,
        purpose=TelegramStoreChat.Purpose.TASKS,
    )
    messages = enqueue_template_message(
        store,
        'task_created',
        {
            'store_name': store.name,
            'date': '01.01.2027',
            'stage_name': 'Утро',
            'task_text': 'Тест',
        },
        idempotency_key='topic-template',
    )
    assert len(messages) == 1
    assert messages[0].message_thread_id == chat.message_thread_id
    assert messages[0].payload['message_thread_id'] == 77


def test_reminders_30_and_10_minutes_are_idempotent():
    store = make_store()
    daily, _ = make_terminal_daily(store, timezone.localdate() + timedelta(days=1))
    chat = TelegramStoreChat.objects.create(
        store=store,
        title='Уведомления',
        chat_id='-100901',
        chat_type=TelegramStoreChat.ChatType.SUPERGROUP,
        purpose=TelegramStoreChat.Purpose.NOTIFICATIONS,
    )
    _ = chat
    stage = daily.stages.get(
        section_code=DailyChecklistStage.SectionCode.OPENING
    )
    at_30 = stage.deadline_at - timedelta(minutes=29)
    assert schedule_telegram_notifications(at=at_30) == 1
    assert schedule_telegram_notifications(at=at_30) == 0
    at_10 = stage.deadline_at - timedelta(minutes=9)
    assert schedule_telegram_notifications(at=at_10) == 1
    assert schedule_telegram_notifications(at=at_10) == 0
    assert TelegramOutboundMessage.objects.filter(
        message_type__in=('stage_reminder_30', 'stage_reminder_10')
    ).count() == 2


def test_store_account_cannot_open_telegram_settings():
    store = make_store()
    user = User.objects.create_user('store-web', password='Strong-934!')
    EmployeeProfile.objects.create(
        user=user,
        store=store,
        role=EmployeeProfile.Role.STORE_ACCOUNT,
        is_active=True,
    )
    StoreTerminalAccount.objects.create(store=store, user=user)
    client = Client()
    client.force_login(user)
    assert client.get(reverse('checklists:telegram_settings')).status_code == 403


def test_director_cannot_edit_another_store_template():
    own = make_store('own-tg')
    other = make_store('other-tg')
    director = make_director(own)
    foreign = make_message_template(other)
    client = Client()
    client.force_login(director)
    assert client.get(
        reverse('checklists:telegram_template_edit', args=[foreign.pk])
    ).status_code == 404
    assert client.get(reverse('checklists:telegram_users')).status_code == 403


def test_chat_mutation_requires_csrf():
    store = make_store()
    director = make_director(store)
    client = Client(enforce_csrf_checks=True)
    client.force_login(director)
    response = client.post(
        reverse('checklists:telegram_chats'),
        {
            'title': 'Без CSRF',
            'chat_id': '-1001',
            'chat_type': 'group',
            'purpose': 'all',
            'is_active': 'on',
        },
    )
    assert response.status_code == 403
    assert not TelegramStoreChat.objects.exists()


def test_settings_pages_render_and_never_show_full_token():
    store = make_store()
    director = make_director(store)
    config = configure_bot()
    client = Client()
    client.force_login(director)
    for name in (
        'telegram_settings',
        'telegram_templates',
        'telegram_chats',
        'telegram_queue',
    ):
        response = client.get(reverse(f'checklists:{name}'))
        assert response.status_code == 200
        assert config.bot_token not in response.content.decode()
    assert client.get(reverse('checklists:telegram_users')).status_code == 403

    client.force_login(make_admin())
    assert client.post(
        reverse('checklists:system_select_managed_store'),
        {'store': store.pk},
    ).status_code == 302
    for name in (
        'telegram_settings',
        'telegram_templates',
        'telegram_chats',
        'telegram_queue',
        'telegram_users',
    ):
        response = client.get(
            reverse(f'checklists:{name}'),
            {},
        )
        assert response.status_code == 200
        assert config.bot_token not in response.content.decode()


def test_empty_new_token_keeps_existing_and_clear_is_explicit():
    store = make_store()
    admin = make_admin()
    config = configure_bot()
    client = Client()
    client.force_login(admin)
    base_data = {
        'action': 'system_settings',
        'alternative_api_base_url': 'https://tauto.gerbud.ru',
        'use_alternative_gateway': 'on',
        'fallback_to_official_api': 'on',
        'alternative_attempts': 5,
        'official_attempts': 5,
        'request_timeout_seconds': 10,
        'retry_delay_seconds': 1,
        'is_enabled': 'on',
    }
    assert client.post(reverse('checklists:telegram_settings'), base_data).status_code == 302
    config.refresh_from_db()
    assert config.bot_token == '123456:super-secret-token'
    assert client.post(
        reverse('checklists:telegram_settings'),
        {**base_data, 'clear_token': 'on'},
    ).status_code == 302
    config.refresh_from_db()
    assert config.bot_token == ''


def test_event_catalog_has_categories_and_event_specific_variables():
    assert len(TELEGRAM_EVENTS) == 12
    assert {event.category for event in TELEGRAM_EVENTS} == {
        'stages',
        'tasks',
        'binding',
        'system',
        'test',
    }
    assert 'task_text' in get_telegram_event('task_created').variable_codes
    with pytest.raises(ValidationError):
        validate_template_source(
            'Недоступно: {task_text}',
            'test_message',
        )


def test_template_pages_show_breadcrumbs_store_block_and_active_tab():
    store = make_store()
    director = make_director(store)
    make_message_template(store)
    client = Client()
    client.force_login(director)
    response = client.get(reverse('checklists:telegram_templates'))
    content = response.content.decode()
    assert response.status_code == 200
    assert '>Главная</a>' in content
    assert 'Telegram для вашего магазина' in content
    assert store.name in content
    assert 'aria-current="page"' in content
    assert 'Шаблоны сообщений' in content
    assert 'id="telegram-store"' not in content


def test_template_edit_breadcrumbs_and_event_variables():
    store = make_store()
    director = make_director(store)
    template = make_message_template(store, 'task_created')
    client = Client()
    client.force_login(director)
    response = client.get(
        reverse('checklists:telegram_template_edit', args=[template.pk])
    )
    content = response.content.decode()
    assert response.status_code == 200
    assert '>Главная</a>' in content
    assert template.name in content
    assert '{task_text}' in content
    assert '{employee_name}' not in content
    assert 'telegram_template_editor.js' in content


def test_system_admin_can_switch_store_on_template_pages():
    first = make_store('switch-one')
    second = make_store('switch-two')
    make_message_template(first)
    make_message_template(second)
    client = Client()
    client.force_login(make_admin('switch-admin'))
    assert client.post(
        reverse('checklists:system_select_managed_store'),
        {'store': second.pk},
    ).status_code == 302
    response = client.get(
        reverse('checklists:telegram_templates'),
    )
    content = response.content.decode()
    assert response.status_code == 200
    assert 'Сменить магазин' in content
    assert f'value="{second.pk}" selected' in content


def test_director_creates_template_with_audit_and_attribution():
    store = make_store()
    director = make_director(store)
    client = Client()
    client.force_login(director)
    values = template_defaults('test_message')
    response = client.post(
        reverse('checklists:telegram_template_create'),
        {
            'event_code': 'test_message',
            **values,
            'is_enabled': 'on',
            'send_to_group': 'on',
        },
    )
    assert response.status_code == 302
    template = TelegramMessageTemplate.objects.get(
        store=store,
        event_code='test_message',
    )
    assert template.created_by == director
    assert template.updated_by == director
    assert AuditLog.objects.filter(
        store=store,
        object_id=str(template.pk),
        action=AuditLog.Action.TELEGRAM_TEMPLATE_CREATED,
    ).exists()


def test_duplicate_store_event_is_blocked():
    store = make_store()
    director = make_director(store)
    existing = make_message_template(store)
    client = Client()
    client.force_login(director)
    values = template_defaults(existing.event_code)
    response = client.post(
        reverse('checklists:telegram_template_create'),
        {
            'event_code': existing.event_code,
            **values,
            'is_enabled': 'on',
            'send_to_group': 'on',
        },
    )
    assert response.status_code == 200
    assert store.telegram_message_templates.count() == 1
    assert 'уже создан шаблон' in response.content.decode()


def test_live_preview_uses_unsaved_values_and_is_safe():
    store = make_store()
    director = make_director(store)
    template = make_message_template(store)
    client = Client()
    client.force_login(director)
    response = client.post(
        reverse(
            'checklists:telegram_template_preview',
            args=[template.pk],
        ),
        template_form_data(
            template,
            title='Несохранённый {store_name}',
            body='<script>alert(1)</script>',
        ),
    )
    assert response.status_code == 200
    preview = response.json()['preview']
    assert 'Несохранённый Магазин' in preview
    assert '<script>alert(1)</script>' in preview
    editor_script = (
        'checklists/static/checklists/telegram_template_editor.js'
    )
    with open(editor_script, encoding='utf-8') as script_file:
        assert 'preview.textContent = result.data.preview' in script_file.read()
    template.refresh_from_db()
    assert template.title != 'Несохранённый {store_name}'


def test_preview_rejects_variable_not_available_for_event():
    store = make_store()
    director = make_director(store)
    template = make_message_template(store, 'test_message')
    client = Client()
    client.force_login(director)
    response = client.post(
        reverse(
            'checklists:telegram_template_preview',
            args=[template.pk],
        ),
        template_form_data(template, body='{task_text}'),
    )
    assert response.status_code == 400
    assert 'body' in response.json()['errors']


def test_template_test_uses_unsaved_values_and_topic():
    store = make_store()
    director = make_director(store)
    template = make_message_template(store, send_to_group=True)
    chat = TelegramStoreChat.objects.create(
        store=store,
        title='Основная группа',
        chat_id='-100771',
        chat_type=TelegramStoreChat.ChatType.SUPERGROUP,
        message_thread_id=77,
        purpose=TelegramStoreChat.Purpose.ALL,
    )
    client = Client()
    client.force_login(director)
    response = client.post(
        reverse('checklists:telegram_template_test', args=[template.pk]),
        template_form_data(
            template,
            title='UNSAVED {store_name}',
            body='Тест текущей формы',
        ),
    )
    assert response.status_code == 302
    outbound = TelegramOutboundMessage.objects.get(message_type='template_test')
    assert 'UNSAVED' in outbound.payload['text']
    assert outbound.message_thread_id == chat.message_thread_id
    template.refresh_from_db()
    assert 'UNSAVED' not in template.title
    assert AuditLog.objects.filter(
        action=AuditLog.Action.TELEGRAM_TEMPLATE_TEST_SENT,
        object_id=str(template.pk),
    ).exists()


def test_toggle_reset_and_delete_are_audited_and_delete_enables_fallback():
    store = make_store()
    director = make_director(store)
    template = make_message_template(
        store,
        title='Изменённый заголовок',
        body='Изменённый текст',
    )
    client = Client()
    client.force_login(director)

    response = client.post(
        reverse('checklists:telegram_template_toggle', args=[template.pk]),
        {'selected_store': store.pk},
    )
    assert response.status_code == 302
    template.refresh_from_db()
    assert not template.is_enabled
    assert AuditLog.objects.filter(
        action=AuditLog.Action.TELEGRAM_TEMPLATE_DISABLED,
        object_id=str(template.pk),
    ).exists()

    reset_url = reverse(
        'checklists:telegram_template_reset',
        args=[template.pk],
    )
    response = client.get(reset_url)
    assert response.status_code == 200
    template.refresh_from_db()
    assert template.title == 'Изменённый заголовок'
    assert client.post(
        reset_url,
        {'selected_store': store.pk},
    ).status_code == 302
    template.refresh_from_db()
    assert template.title == template_defaults('test_message')['title']
    assert template.is_enabled
    assert AuditLog.objects.filter(
        action=AuditLog.Action.TELEGRAM_TEMPLATE_RESET,
        object_id=str(template.pk),
    ).exists()

    delete_url = reverse(
        'checklists:telegram_template_delete',
        args=[template.pk],
    )
    assert client.get(delete_url).status_code == 200
    template_id = template.pk
    assert client.post(
        delete_url,
        {'selected_store': store.pk},
    ).status_code == 302
    assert not TelegramMessageTemplate.objects.filter(pk=template_id).exists()
    assert AuditLog.objects.filter(
        action=AuditLog.Action.TELEGRAM_TEMPLATE_DELETED,
        object_id=str(template_id),
    ).exists()

    TelegramStoreChat.objects.create(
        store=store,
        title='Уведомления',
        chat_id='-100779',
        chat_type=TelegramStoreChat.ChatType.SUPERGROUP,
        purpose=TelegramStoreChat.Purpose.NOTIFICATIONS,
    )
    queued = enqueue_template_message(
        store,
        'test_message',
        {'store_name': store.name, 'date': '18.07.2026'},
        idempotency_key='fallback-after-delete',
    )
    assert len(queued) == 1
    assert 'Связь с магазином' in queued[0].payload['text']


def test_template_mutations_are_scoped_to_director_store():
    own = make_store('scope-own')
    other = make_store('scope-other')
    director = make_director(own)
    foreign = make_message_template(other)
    client = Client()
    client.force_login(director)
    for name in (
        'telegram_template_edit',
        'telegram_template_delete',
        'telegram_template_reset',
    ):
        assert client.get(
            reverse(f'checklists:{name}', args=[foreign.pk])
        ).status_code == 404
    assert client.post(
        reverse('checklists:telegram_template_toggle', args=[foreign.pk]),
    ).status_code == 404


def test_store_account_cannot_access_template_list_or_mutations():
    store = make_store()
    template = make_message_template(store)
    user = User.objects.create_user('scoped-store-account', password='Strong-934!')
    EmployeeProfile.objects.create(
        user=user,
        store=store,
        role=EmployeeProfile.Role.STORE_ACCOUNT,
        is_active=True,
    )
    StoreTerminalAccount.objects.create(store=store, user=user)
    client = Client()
    client.force_login(user)
    assert client.get(reverse('checklists:telegram_templates')).status_code == 403
    assert client.post(
        reverse('checklists:telegram_template_toggle', args=[template.pk])
    ).status_code == 403


def test_template_delete_requires_csrf():
    store = make_store()
    director = make_director(store)
    template = make_message_template(store)
    client = Client(enforce_csrf_checks=True)
    client.force_login(director)
    response = client.post(
        reverse('checklists:telegram_template_delete', args=[template.pk]),
        {'selected_store': store.pk},
    )
    assert response.status_code == 403
    assert TelegramMessageTemplate.objects.filter(pk=template.pk).exists()


def test_system_admin_template_id_is_scoped_to_selected_store():
    own = make_store('admin-scope-own')
    other = make_store('admin-scope-other')
    foreign = make_message_template(other)
    client = Client()
    client.force_login(make_admin('scoped-system-admin'))
    assert client.get(
        reverse('checklists:telegram_template_edit', args=[foreign.pk]),
        {'store': own.pk},
    ).status_code == 404


def test_template_list_filters_search_status_category_and_destination():
    store = make_store()
    director = make_director(store)
    make_message_template(
        store,
        'task_created',
        name='Особая задача',
        is_enabled=False,
        send_to_group=False,
        send_to_private=True,
    )
    make_message_template(store, 'test_message')
    client = Client()
    client.force_login(director)
    response = client.get(
        reverse('checklists:telegram_templates'),
        {
            'q': 'Особая',
            'category': 'tasks',
            'status': 'disabled',
            'destination': 'private',
        },
    )
    content = response.content.decode()
    assert response.status_code == 200
    assert 'Особая задача' in content
    shown = [
        item.event_code
        for group in response.context['template_groups']
        for item in group['templates']
    ]
    assert shown == ['task_created']


def test_template_update_audit_contains_safe_metadata():
    store = make_store()
    director = make_director(store)
    template = make_message_template(store)
    client = Client()
    client.force_login(director)
    response = client.post(
        reverse('checklists:telegram_template_edit', args=[template.pk]),
        template_form_data(template, name='Новое имя'),
    )
    assert response.status_code == 302
    audit = AuditLog.objects.get(
        action=AuditLog.Action.TELEGRAM_TEMPLATE_UPDATED,
        object_id=str(template.pk),
    )
    assert audit.new_value['store_id'] == store.pk
    assert audit.new_value['template_id'] == str(template.pk)
    assert audit.new_value['event_code'] == template.event_code
    assert audit.new_value['actor_id'] == director.pk
    assert 'token' not in json.dumps(audit.new_value).lower()


@pytest.mark.django_db(transaction=True)
def test_template_migration_preserves_existing_rows():
    executor = MigrationExecutor(connection)
    executor.migrate([('checklists', '0013_expand_default_telegram_task_templates')])
    old_apps = executor.loader.project_state(
        [('checklists', '0013_expand_default_telegram_task_templates')]
    ).apps
    OldStore = old_apps.get_model('checklists', 'Store')
    OldTemplate = old_apps.get_model('checklists', 'TelegramMessageTemplate')
    store = OldStore.objects.create(
        name='Миграционный магазин',
        code='migration-store',
        timezone='Europe/Moscow',
        is_active=True,
    )
    old = OldTemplate.objects.create(
        store=store,
        template_type='stage_closed',
        title='Сохранённый заголовок',
        body='Сохранённый текст {store_name}',
        parse_mode='HTML',
        is_enabled=True,
        send_to_private=False,
        send_to_group=True,
    )

    executor = MigrationExecutor(connection)
    executor.migrate([('checklists', '0015_finalize_telegram_template_redesign')])
    new_apps = executor.loader.project_state(
        [('checklists', '0015_finalize_telegram_template_redesign')]
    ).apps
    NewTemplate = new_apps.get_model('checklists', 'TelegramMessageTemplate')
    migrated = NewTemplate.objects.get(pk=old.pk)
    assert migrated.event_code == 'stage_closed'
    assert migrated.name == 'Этап закрыт'
    assert migrated.title == 'Сохранённый заголовок'
    assert migrated.body == 'Сохранённый текст {store_name}'


def webhook_headers(secret='webhook-test-secret'):
    return {
        'HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN': secret,
        'content_type': 'application/json',
    }


def test_webhook_processes_synchronous_update_and_deduplicates(monkeypatch):
    config = configure_bot(
        webhook_secret_token='webhook-test-secret',
        webhook_is_enabled=True,
    )
    requests = []

    def transport(api_request, timeout):
        requests.append((api_request, timeout))
        return TelegramHTTPResponse()

    monkeypatch.setattr('checklists.telegram_client.request.urlopen', transport)
    client = Client()
    update = message_update(81001, 7001, 7001, '/start')
    response = client.post(
        reverse('checklists:telegram_webhook'),
        data=json.dumps(update),
        **webhook_headers(),
    )
    assert response.status_code == 200
    log = TelegramUpdateLog.objects.get(update_id=81001)
    assert log.processed
    assert log.payload['message']['text'] == '/start'
    assert not TelegramInboundJob.objects.filter(update_id=81001).exists()
    assert len(requests) == 1
    assert requests[0][1] <= 2
    assert requests[0][0].full_url.startswith('https://tauto.gerbud.ru/')
    assert 'Одноразовый код' in json.loads(requests[0][0].data)['text']
    assert 'Принято' not in json.loads(requests[0][0].data)['text']
    assert config.webhook_secret_token not in response.content.decode()

    duplicate = client.post(
        reverse('checklists:telegram_webhook'),
        data=json.dumps(update),
        **webhook_headers(),
    )
    assert duplicate.status_code == 200
    assert TelegramInboundJob.objects.filter(update_id=81001).count() == 0
    assert len(requests) == 1


def test_webhook_rejects_get_secret_json_and_large_body(settings):
    configure_bot(webhook_secret_token='webhook-test-secret')
    client = Client()
    url = reverse('checklists:telegram_webhook')
    assert client.get(url).status_code == 405
    assert client.post(
        url,
        data='{}',
        content_type='application/json',
    ).status_code == 403
    assert client.post(
        url,
        data='{}',
        content_type='application/json',
        HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN='wrong',
    ).status_code == 403
    assert client.post(
        url,
        data='{broken',
        **webhook_headers(),
    ).status_code == 400
    settings.TELEGRAM_WEBHOOK_MAX_BODY_BYTES = 20
    assert client.post(
        url,
        data=json.dumps(message_update(1, 1, 1, '/start')),
        **webhook_headers(),
    ).status_code == 413


def test_failed_immediate_real_response_is_queued(monkeypatch):
    config = configure_bot(webhook_secret_token='webhook-test-secret')

    def transport(api_request, timeout):
        raise urllib_error.URLError('offline')

    monkeypatch.setattr('checklists.telegram_client.request.urlopen', transport)
    response = Client().post(
        reverse('checklists:telegram_webhook'),
        data=json.dumps(message_update(81002, 7002, 7002, '/help')),
        **webhook_headers(),
    )
    assert response.status_code == 200
    fallback = TelegramOutboundMessage.objects.get()
    assert fallback.idempotency_key.startswith('webhook:update:81002:')
    assert fallback.payload['text'] == 'Доступ не подтверждён. Отправьте /start.'
    assert fallback.payload['text'] != 'Принято'
    log = TelegramUpdateLog.objects.get(update_id=81002)
    assert log.command == '/help'
    assert log.response_status == TelegramUpdateLog.ResponseStatus.QUEUED
    assert log.responded_at is not None
    assert config.bot_token not in log.response_error


def test_callback_webhook_answers_callback_and_acknowledges(monkeypatch):
    configure_bot(webhook_secret_token='webhook-test-secret')
    methods = []

    def transport(api_request, timeout):
        methods.append(api_request.full_url)
        return TelegramHTTPResponse()

    monkeypatch.setattr('checklists.telegram_client.request.urlopen', transport)
    response = Client().post(
        reverse('checklists:telegram_webhook'),
        data=json.dumps(callback_update(81003, 7003, 7003, 'task:cancel')),
        **webhook_headers(),
    )
    assert response.status_code == 200
    assert any('/answerCallbackQuery' in url for url in methods)
    assert any('/sendMessage' in url for url in methods)


def test_synchronous_start_is_not_processed_by_inbound_worker(monkeypatch):
    configure_bot(webhook_secret_token='webhook-test-secret')
    monkeypatch.setattr(
        'checklists.telegram_client.request.urlopen',
        lambda api_request, timeout: TelegramHTTPResponse(),
    )
    client = Client()
    update = message_update(81004, 7004, 7004, '/start')
    assert client.post(
        reverse('checklists:telegram_webhook'),
        data=json.dumps(update),
        **webhook_headers(),
    ).status_code == 200
    assert process_inbound_queue(limit=10)['completed'] == 0
    assert TelegramPendingBinding.objects.filter(update_id=81004).count() == 1
    assert process_inbound_queue(limit=10)['claimed'] == 0
    assert TelegramPendingBinding.objects.filter(update_id=81004).count() == 1


def test_synchronous_task_starts_dialog_without_inbound_worker(monkeypatch):
    store = make_store('inbound-task')
    config = configure_bot(webhook_secret_token='webhook-test-secret')
    _ = config
    TelegramStoreBinding.objects.create(
        store=store,
        telegram_user_id=7010,
        telegram_chat_id=7010,
        username='manager',
        user=User.objects.create_user(username='inbound-task-author'),
    )
    monkeypatch.setattr(
        'checklists.telegram_client.request.urlopen',
        lambda api_request, timeout: TelegramHTTPResponse(),
    )
    response = Client().post(
        reverse('checklists:telegram_webhook'),
        data=json.dumps(message_update(81010, 7010, 7010, '/task')),
        **webhook_headers(),
    )
    assert response.status_code == 200
    assert process_inbound_queue(limit=10)['completed'] == 0
    assert TelegramConversationState.objects.get(
        telegram_binding__telegram_user_id=7010
    ).state == 'choose_date'


def test_polling_is_disabled_for_active_webhook_and_force_works(monkeypatch):
    configure_bot(
        incoming_mode=TelegramSystemSettings.IncomingMode.WEBHOOK,
        webhook_is_enabled=True,
    )
    output = StringIO()
    call_command('poll_telegram_updates', stdout=output)
    assert 'Polling отключён' in output.getvalue()

    monkeypatch.setattr(
        'checklists.telegram_client.request.urlopen',
        lambda api_request, timeout: TelegramHTTPResponse(
            {'ok': True, 'result': []}
        ),
    )
    forced = StringIO()
    call_command(
        'poll_telegram_updates',
        '--force',
        stdout=forced,
    )
    assert 'Получено: 0' in forced.getvalue()


def test_system_admin_can_register_check_and_delete_webhook(monkeypatch):
    configure_bot(webhook_secret_token='webhook-test-secret')
    calls = []

    def transport(api_request, timeout):
        calls.append(api_request.full_url)
        if '/getWebhookInfo' in api_request.full_url:
            return TelegramHTTPResponse(
                {'ok': True, 'result': {'url': 'https://checklist.es-helper.ru/telegram/webhook/'}}
            )
        return TelegramHTTPResponse()

    monkeypatch.setattr('checklists.telegram_client.request.urlopen', transport)
    client = Client()
    client.force_login(make_admin('webhook-admin'))
    url = reverse('checklists:telegram_settings')
    for action in ('webhook_register', 'webhook_check', 'webhook_delete'):
        assert client.post(url, {'action': action}).status_code == 302
    assert any('/setWebhook' in url_value for url_value in calls)
    assert any('/getWebhookInfo' in url_value for url_value in calls)
    assert any('/deleteWebhook' in url_value for url_value in calls)


@pytest.mark.parametrize(
    ('command', 'expected'),
    (
        ('/start', 'Выберите действие'),
        ('/menu', 'Выберите действие'),
        ('/help', 'Команды бота'),
        ('/task', 'На какую дату создать задачу?'),
        ('/newtask', 'На какую дату создать задачу?'),
        ('/tasks', 'Задачи магазина'),
        ('/status', 'Статус магазина'),
        ('/myid', 'Telegram ID: 8200'),
        ('/whoami', 'Telegram ID: 8200'),
    ),
)
def test_bound_webhook_commands_answer_immediately_without_ack(
    monkeypatch,
    command,
    expected,
):
    store = make_store(f'sync-{command[1:]}')
    linked_user = User.objects.create_user(
        username=f"sync-author-{command[1:]}"
    )
    TelegramStoreBinding.objects.create(
        store=store,
        telegram_user_id=8200,
        telegram_chat_id=8200,
        user=linked_user,
    )
    configure_bot(webhook_secret_token='webhook-test-secret')
    payloads = []

    def transport(api_request, timeout):
        if api_request.data:
            payloads.append(json.loads(api_request.data))
        return TelegramHTTPResponse()

    monkeypatch.setattr('checklists.telegram_client.request.urlopen', transport)
    response = Client().post(
        reverse('checklists:telegram_webhook'),
        data=json.dumps(message_update(8201, 8200, 8200, command)),
        **webhook_headers(),
    )
    assert response.status_code == 200
    texts = [payload['text'] for payload in payloads if 'text' in payload]
    assert any(expected in text for text in texts)
    assert all(text != 'Принято' for text in texts)
    audit = TelegramOutboundMessage.objects.get()
    assert audit.status == TelegramOutboundMessage.Status.SENT
    assert audit.sent_at is not None
    log = TelegramUpdateLog.objects.get(update_id=8201)
    assert log.command == command
    assert log.response_status == TelegramUpdateLog.ResponseStatus.SENT
    assert log.responded_at is not None


def test_task_callback_returns_next_screen_immediately(monkeypatch):
    store = make_store('sync-callback')
    TelegramStoreBinding.objects.create(
        store=store,
        telegram_user_id=8210,
        telegram_chat_id=8210,
        user=User.objects.create_user(username='sync-callback-author'),
    )
    configure_bot(webhook_secret_token='webhook-test-secret')
    payloads = []

    def transport(api_request, timeout):
        payloads.append(
            json.loads(api_request.data) if api_request.data else {}
        )
        return TelegramHTTPResponse()

    monkeypatch.setattr('checklists.telegram_client.request.urlopen', transport)
    client = Client()
    client.post(
        reverse('checklists:telegram_webhook'),
        data=json.dumps(message_update(8211, 8210, 8210, '/task')),
        **webhook_headers(),
    )
    payloads.clear()
    client.post(
        reverse('checklists:telegram_webhook'),
        data=json.dumps(
            callback_update(8212, 8210, 8210, 'task:date:tomorrow')
        ),
        **webhook_headers(),
    )
    assert any('callback_query_id' in payload for payload in payloads)
    assert any(
        'Выберите этап' in payload.get('text', '') for payload in payloads
    )


def test_duplicate_confirm_callback_creates_task_and_response_once(monkeypatch):
    store = make_store('sync-confirm')
    linked_user = User.objects.create_user(username='sync-confirm-author')
    binding = TelegramStoreBinding.objects.create(
        store=store,
        telegram_user_id=8220,
        telegram_chat_id=8220,
        user=linked_user,
    )
    configure_bot(webhook_secret_token='webhook-test-secret')
    requests = []

    def transport(api_request, timeout):
        requests.append(api_request)
        return TelegramHTTPResponse()

    monkeypatch.setattr('checklists.telegram_client.request.urlopen', transport)
    client = Client()
    updates = (
        message_update(8221, 8220, 8220, '/task'),
        callback_update(8222, 8220, 8220, 'task:date:tomorrow'),
        callback_update(8223, 8220, 8220, 'task:section:morning'),
        message_update(8224, 8220, 8220, 'Проверить витрину'),
        callback_update(8225, 8220, 8220, 'task:skip-description'),
    )
    for update in updates:
        client.post(
            reverse('checklists:telegram_webhook'),
            data=json.dumps(update),
            **webhook_headers(),
        )
    confirm = callback_update(8226, 8220, 8220, 'task:confirm')
    client.post(
        reverse('checklists:telegram_webhook'),
        data=json.dumps(confirm),
        **webhook_headers(),
    )
    sent_after_first = len(requests)
    client.post(
        reverse('checklists:telegram_webhook'),
        data=json.dumps(confirm),
        **webhook_headers(),
    )
    assert StoreAdHocTask.objects.filter(
        created_by_telegram_binding=binding
    ).count() == 1
    assert StoreAdHocTask.objects.get(
        created_by_telegram_binding=binding
    ).created_by == linked_user
    assert len(requests) == sent_after_first


def test_background_webhook_creates_inbound_job(monkeypatch):
    configure_bot(webhook_secret_token='webhook-test-secret')
    monkeypatch.setattr(
        'checklists.telegram_client.request.urlopen',
        lambda api_request, timeout: TelegramHTTPResponse(),
    )
    response = Client().post(
        reverse('checklists:telegram_webhook'),
        data=json.dumps(message_update(8231, 8230, 8230, '/report')),
        **webhook_headers(),
    )
    assert response.status_code == 200
    job = TelegramInboundJob.objects.get(update_id=8231)
    assert job.status == TelegramInboundJob.Status.PENDING
    assert not job.update_log.processed


def test_task_list_is_readable_and_has_filters(monkeypatch):
    store = make_store('readable-tasks')
    TelegramStoreBinding.objects.create(
        store=store,
        telegram_user_id=8240,
        telegram_chat_id=8240,
    )
    StoreAdHocTask.objects.create(
        store=store,
        date=timezone.now().astimezone(ZoneInfo(store.timezone)).date(),
        section_code=StoreAdHocTask.SectionCode.DAY,
        text='Проверить поставку',
        status=StoreAdHocTask.Status.ACTIVE,
    )
    configure_bot(webhook_secret_token='webhook-test-secret')
    payloads = []

    def transport(api_request, timeout):
        payloads.append(json.loads(api_request.data))
        return TelegramHTTPResponse()

    monkeypatch.setattr('checklists.telegram_client.request.urlopen', transport)
    Client().post(
        reverse('checklists:telegram_webhook'),
        data=json.dumps(message_update(8241, 8240, 8240, '/tasks')),
        **webhook_headers(),
    )
    payload = next(item for item in payloads if 'text' in item)
    assert 'Сегодня:' in payload['text']
    assert 'День · Проверить поставку' in payload['text']
    keyboard_text = json.dumps(payload['reply_markup'], ensure_ascii=False)
    assert 'Проблемные' in keyboard_text
    assert 'Главное меню' in keyboard_text
    assert '#1 ' not in payload['text']


def test_set_and_get_my_commands(monkeypatch):
    config = configure_bot()
    methods = []

    def transport(api_request, timeout):
        methods.append(api_request.full_url)
        if '/getMyCommands' in api_request.full_url:
            return TelegramHTTPResponse(
                {'ok': True, 'result': [{'command': 'start'}]}
            )
        return TelegramHTTPResponse({'ok': True, 'result': True})

    monkeypatch.setattr('checklists.telegram_client.request.urlopen', transport)
    call_command('register_telegram_commands')
    config.refresh_from_db()
    assert config.bot_commands_registered_at
    from checklists.telegram_commands import BOT_COMMANDS, get_bot_commands

    assert get_bot_commands(config) == [{'command': 'start'}]
    assert [item['command'] for item in BOT_COMMANDS] == [
        'start',
        'help',
        'menu',
        'tasks',
        'newtask',
        'status',
        'myid',
    ]
    assert any('/setMyCommands' in url for url in methods)
    assert any('/getMyCommands' in url for url in methods)
