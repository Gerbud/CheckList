import json
import socket
from datetime import date, datetime, timedelta
from io import StringIO
from urllib import error
from zoneinfo import ZoneInfo

import pytest
from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.exceptions import ValidationError
from django.db import connection

from checklists.models import (
    AuditLog,
    ChecklistAnswer,
    ChecklistItem,
    ChecklistNotification,
    ChecklistSection,
    ChecklistTemplate,
    ChecklistTemplateVersion,
    DailyChecklistStage,
    EmployeeProfile,
    Store,
    StoreChecklistSchedule,
    StoreNotificationSettings,
)
from checklists.notifications import (
    build_notification_text,
    claim_notification,
    create_completed_late_notification,
    process_due_notifications,
    schedule_due_notifications,
    schedule_stage_notifications,
    send_notification,
)
from checklists.services import (
    complete_checklist_stage,
    create_daily_checklist,
    publish_template_version,
    update_answer,
)


pytestmark = pytest.mark.django_db
MOSCOW = ZoneInfo('Europe/Moscow')
CHECKLIST_DATE = date(2026, 7, 16)


def at(hour, minute=0):
    return datetime(2026, 7, 16, hour, minute, tzinfo=MOSCOW)


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return self.payload


@pytest.fixture
def notification_setup(settings):
    settings.TELEGRAM_NOTIFICATIONS_ENABLED = True
    settings.TELEGRAM_BOT_TOKEN = 'test-only-secret-token'
    settings.TELEGRAM_REQUEST_TIMEOUT = 3
    store = Store.objects.create(
        name='5 <Планет & тест>',
        code='telegram-store',
        timezone='Europe/Moscow',
    )
    StoreChecklistSchedule.objects.create(
        store=store,
        warning_minutes_before=30,
    )
    notification_settings = StoreNotificationSettings.objects.create(
        store=store,
        telegram_chat_id='-1001234567890',
    )
    manager_user = User.objects.create_user(username='telegram-manager')
    EmployeeProfile.objects.create(
        user=manager_user,
        store=store,
        role=EmployeeProfile.Role.MANAGER,
    )
    employee_user = User.objects.create_user(
        username='telegram-employee<&>',
        first_name='Иван <Тест>',
    )
    employee = EmployeeProfile.objects.create(user=employee_user, store=store)
    template = ChecklistTemplate.objects.create(store=store, name='Telegram')
    version = ChecklistTemplateVersion.objects.create(
        template=template,
        version_number=1,
        created_by=manager_user,
    )
    for order, code in enumerate(('opening', 'during_day', 'closing'), start=1):
        section = ChecklistSection.objects.create(
            version=version,
            name=code,
            code=code,
            sort_order=order,
        )
        ChecklistItem.objects.create(
            section=section,
            text=f'{code} item',
            sort_order=1,
        )
    publish_template_version(version, manager_user)
    daily = create_daily_checklist(employee, CHECKLIST_DATE)
    return {
        'store': store,
        'schedule': store.checklist_schedule,
        'notification_settings': notification_settings,
        'employee': employee,
        'daily': daily,
    }


def stage_and_notification(setup, notification_type, section='opening'):
    stage = setup['daily'].stages.get(section_code=section)
    notification = stage.notifications.get(notification_type=notification_type)
    return stage, notification


def complete_opening(setup, completed_at):
    stage = setup['daily'].stages.get(section_code='opening')
    answer = setup['daily'].items.get(section_code='opening').answer
    update_answer(
        answer,
        ChecklistAnswer.Status.COMPLETED,
        '',
        setup['employee'].user,
        at=min(completed_at, at(10, 59)),
    )
    return complete_checklist_stage(
        stage,
        setup['employee'].user,
        at=completed_at,
    )


def successful_urlopen(*args, **kwargs):
    return FakeResponse(b'{"ok": true, "result": {"message_id": 98765}}')


def test_warning_is_scheduled_from_store_setting(notification_setup):
    stage, notification = stage_and_notification(
        notification_setup,
        ChecklistNotification.NotificationType.DEADLINE_WARNING,
    )

    assert notification.scheduled_for == stage.deadline_at - timedelta(minutes=30)


def test_changed_warning_setting_is_used_for_new_schedule(notification_setup):
    stage, _ = stage_and_notification(
        notification_setup,
        ChecklistNotification.NotificationType.DEADLINE_WARNING,
    )
    stage.notifications.filter(
        notification_type=ChecklistNotification.NotificationType.DEADLINE_WARNING
    ).delete()
    schedule = notification_setup['schedule']
    schedule.warning_minutes_before = 45
    schedule.save()

    schedule_stage_notifications(stage)
    notification = stage.notifications.get(
        notification_type=ChecklistNotification.NotificationType.DEADLINE_WARNING
    )

    assert notification.scheduled_for == stage.deadline_at - timedelta(minutes=45)


def test_overdue_is_created_by_cron_at_deadline(notification_setup):
    stage, _ = stage_and_notification(
        notification_setup,
        ChecklistNotification.NotificationType.OVERDUE,
    )
    stage.notifications.filter(
        notification_type=ChecklistNotification.NotificationType.OVERDUE
    ).delete()

    assert schedule_due_notifications(at(10, 59)) == 0
    assert not stage.notifications.filter(
        notification_type=ChecklistNotification.NotificationType.OVERDUE
    ).exists()
    assert schedule_due_notifications(at(11)) == 1


def test_warning_is_not_sent_for_completed_stage(
    notification_setup,
    monkeypatch,
):
    complete_opening(notification_setup, at(10))
    monkeypatch.setattr(
        'checklists.notifications.request.urlopen',
        lambda *args, **kwargs: pytest.fail('HTTP must not be called'),
    )

    result = process_due_notifications(at(10, 30))

    assert result['sent'] == 0
    assert result['skipped'] == 1


def test_overdue_is_sent_at_deadline_and_message_id_is_saved(
    notification_setup,
    monkeypatch,
):
    stage, notification = stage_and_notification(
        notification_setup,
        ChecklistNotification.NotificationType.OVERDUE,
    )
    stage.notifications.filter(
        notification_type=ChecklistNotification.NotificationType.DEADLINE_WARNING
    ).update(status=ChecklistNotification.Status.SENT, sent_at=at(10, 30))
    monkeypatch.setattr(
        'checklists.notifications.request.urlopen',
        successful_urlopen,
    )

    result = process_due_notifications(at(11))
    notification.refresh_from_db()

    assert result['sent'] == 1
    assert notification.status == ChecklistNotification.Status.SENT
    assert notification.telegram_message_id == 98765
    audit = AuditLog.objects.get(
        action=AuditLog.Action.TELEGRAM_NOTIFICATION_SENT
    )
    assert audit.new_value['chat_id'].startswith('***')
    assert '-1001234567890' not in json.dumps(audit.new_value)


def test_overdue_is_not_sent_for_completed_stage(notification_setup, monkeypatch):
    complete_opening(notification_setup, at(10))
    notification_setup['daily'].stages.get(section_code='opening').notifications.filter(
        notification_type=ChecklistNotification.NotificationType.DEADLINE_WARNING
    ).delete()
    monkeypatch.setattr(
        'checklists.notifications.request.urlopen',
        lambda *args, **kwargs: pytest.fail('HTTP must not be called'),
    )

    result = process_due_notifications(at(11))

    assert result['sent'] == 0
    assert result['skipped'] == 1


def test_completed_late_notification_is_created_only_once(notification_setup):
    stage = complete_opening(notification_setup, at(12))

    first = create_completed_late_notification(stage)
    second = create_completed_late_notification(stage)

    assert first.pk == second.pk
    assert stage.notifications.filter(
        notification_type=ChecklistNotification.NotificationType.COMPLETED_LATE
    ).count() == 1


def test_repeated_scheduler_does_not_create_duplicates(notification_setup):
    stage = notification_setup['daily'].stages.get(section_code='opening')

    assert schedule_stage_notifications(stage) == 0
    assert schedule_due_notifications(at(11)) == 0
    assert stage.notifications.count() == 2


def test_notification_can_be_claimed_only_once(notification_setup):
    _, notification = stage_and_notification(
        notification_setup,
        ChecklistNotification.NotificationType.DEADLINE_WARNING,
    )

    first = claim_notification(notification.pk, at(10, 30))
    second = claim_notification(notification.pk, at(10, 30))

    assert first is not None
    assert first.attempts == 1
    assert second is None


@pytest.mark.django_db(transaction=True)
def test_http_is_executed_outside_database_transaction(
    notification_setup,
    monkeypatch,
):
    _, notification = stage_and_notification(
        notification_setup,
        ChecklistNotification.NotificationType.DEADLINE_WARNING,
    )
    claimed = claim_notification(notification.pk, at(10, 30))

    def assert_no_transaction(*args, **kwargs):
        assert not connection.in_atomic_block
        return successful_urlopen()

    monkeypatch.setattr(
        'checklists.notifications.request.urlopen',
        assert_no_transaction,
    )

    assert send_notification(claimed) == 'sent'


def test_stale_sending_notification_can_be_claimed_again(notification_setup):
    _, notification = stage_and_notification(
        notification_setup,
        ChecklistNotification.NotificationType.DEADLINE_WARNING,
    )
    notification.status = ChecklistNotification.Status.SENDING
    notification.attempts = 1
    notification.sending_started_at = at(10, 10)
    notification.save()

    claimed = claim_notification(notification.pk, at(10, 30))

    assert claimed is not None
    assert claimed.attempts == 2


@pytest.mark.parametrize(
    ('transport', 'expected_error'),
    (
        (lambda *args, **kwargs: (_ for _ in ()).throw(socket.timeout()), 'timed out'),
        (
            lambda *args, **kwargs: (_ for _ in ()).throw(
                error.HTTPError('https://redacted.invalid', 500, 'error', {}, None)
            ),
            'HTTP error: 500',
        ),
        (
            lambda *args, **kwargs: FakeResponse(
                b'{"ok": false, "description": "rejected"}'
            ),
            'ok=false',
        ),
        (lambda *args, **kwargs: FakeResponse(b'not-json'), 'invalid JSON'),
    ),
)
def test_delivery_errors_mark_notification_failed(
    notification_setup,
    monkeypatch,
    transport,
    expected_error,
):
    _, notification = stage_and_notification(
        notification_setup,
        ChecklistNotification.NotificationType.DEADLINE_WARNING,
    )
    monkeypatch.setattr('checklists.notifications.request.urlopen', transport)
    claimed = claim_notification(notification.pk, at(10, 30))

    outcome = send_notification(claimed)
    notification.refresh_from_db()

    assert outcome == 'failed'
    assert notification.status == ChecklistNotification.Status.FAILED
    assert expected_error in notification.last_error


def test_dynamic_values_are_html_escaped_and_store_timezone_is_used(
    notification_setup,
):
    _, notification = stage_and_notification(
        notification_setup,
        ChecklistNotification.NotificationType.DEADLINE_WARNING,
    )

    text = build_notification_text(notification)

    assert '5 &lt;Планет &amp; тест&gt;' in text
    assert 'Иван &lt;Тест&gt;' in text
    assert '16.07.2026 11:00' in text
    assert '5 <Планет' not in text


def test_global_disable_prevents_http_and_state_change(
    notification_setup,
    settings,
    monkeypatch,
):
    settings.TELEGRAM_NOTIFICATIONS_ENABLED = False
    _, notification = stage_and_notification(
        notification_setup,
        ChecklistNotification.NotificationType.DEADLINE_WARNING,
    )
    monkeypatch.setattr(
        'checklists.notifications.request.urlopen',
        lambda *args, **kwargs: pytest.fail('HTTP must not be called'),
    )

    result = process_due_notifications(at(10, 30), limit=1)
    notification.refresh_from_db()

    assert result['skipped'] == 1
    assert notification.status == ChecklistNotification.Status.PENDING
    assert notification.attempts == 0


def test_store_settings_disable_individual_notification_type(notification_setup):
    notification_settings = notification_setup['notification_settings']
    notification_settings.warning_enabled = False
    notification_settings.save()
    stage = notification_setup['daily'].stages.get(section_code='opening')
    stage.notifications.filter(
        notification_type=ChecklistNotification.NotificationType.DEADLINE_WARNING
    ).delete()

    schedule_stage_notifications(stage)

    assert not stage.notifications.filter(
        notification_type=ChecklistNotification.NotificationType.DEADLINE_WARNING
    ).exists()
    assert stage.notifications.filter(
        notification_type=ChecklistNotification.NotificationType.OVERDUE
    ).exists()


def test_completed_late_type_can_be_disabled(notification_setup):
    notification_settings = notification_setup['notification_settings']
    notification_settings.completed_late_enabled = False
    notification_settings.save()

    stage = complete_opening(notification_setup, at(12))

    assert not stage.notifications.filter(
        notification_type=ChecklistNotification.NotificationType.COMPLETED_LATE
    ).exists()


def test_active_store_notification_settings_require_chat_id(notification_setup):
    notification_settings = notification_setup['notification_settings']
    notification_settings.telegram_chat_id = ''

    with pytest.raises(ValidationError, match='chat ID'):
        notification_settings.full_clean()


def test_token_and_full_chat_id_do_not_reach_audit_or_logs(
    notification_setup,
    monkeypatch,
    caplog,
):
    _, notification = stage_and_notification(
        notification_setup,
        ChecklistNotification.NotificationType.DEADLINE_WARNING,
    )
    monkeypatch.setattr(
        'checklists.notifications.request.urlopen',
        lambda *args, **kwargs: (_ for _ in ()).throw(socket.timeout()),
    )
    claimed = claim_notification(notification.pk, at(10, 30))

    send_notification(claimed)
    audit = AuditLog.objects.get(
        action=AuditLog.Action.TELEGRAM_NOTIFICATION_FAILED
    )
    serialized_audit = json.dumps(audit.new_value)

    assert 'test-only-secret-token' not in caplog.text
    assert 'test-only-secret-token' not in serialized_audit
    assert '-1001234567890' not in serialized_audit
    assert audit.new_value['chat_id'].startswith('***')


def test_dry_run_does_not_send_or_change_database(
    notification_setup,
    monkeypatch,
):
    before = list(
        ChecklistNotification.objects.order_by('pk').values_list(
            'pk',
            'status',
            'attempts',
        )
    )
    monkeypatch.setattr(
        'checklists.notifications.request.urlopen',
        lambda *args, **kwargs: pytest.fail('HTTP must not be called'),
    )
    output = StringIO()

    call_command(
        'send_checklist_notifications',
        '--dry-run',
        '--at',
        at(10, 30).isoformat(),
        stdout=output,
    )

    assert before == list(
        ChecklistNotification.objects.order_by('pk').values_list(
            'pk',
            'status',
            'attempts',
        )
    )
    assert 'WOULD SEND' in output.getvalue()
    assert 'sent=0' in output.getvalue()
