import html
import json
import socket
from datetime import timedelta
from urllib import error, parse, request
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from checklists.models import (
    AuditLog,
    ChecklistAnswer,
    ChecklistDayStatus,
    ChecklistItem,
    ChecklistNotification,
    DailyChecklistStage,
    StoreChecklistSchedule,
    StoreNotificationSettings,
)


STALE_SENDING_AFTER = timedelta(minutes=10)
FINAL_STAGE_STATUSES = {
    DailyChecklistStage.Status.COMPLETED,
    DailyChecklistStage.Status.COMPLETED_LATE,
}


class TelegramDeliveryError(Exception):
    """Безопасная ошибка доставки без URL, токена и ответа Telegram."""


def _aware_now(at=None):
    value = at or timezone.now()
    if not timezone.is_aware(value):
        raise ValueError('Время обработки уведомлений должно быть aware.')
    return value


def _delivery_settings(stage, notification_type):
    if not settings.TELEGRAM_NOTIFICATIONS_ENABLED:
        return None
    if stage.daily_checklist.day_status == ChecklistDayStatus.DAY_OFF:
        return None
    store = stage.daily_checklist.store
    try:
        schedule = store.checklist_schedule
        notification_settings = store.notification_settings
    except (
        StoreChecklistSchedule.DoesNotExist,
        StoreNotificationSettings.DoesNotExist,
    ):
        return None
    if not (
        schedule.is_active
        and schedule.notifications_enabled
        and notification_settings.is_active
        and notification_settings.telegram_chat_id
    ):
        return None
    enabled_field = {
        ChecklistNotification.NotificationType.DEADLINE_WARNING: 'warning_enabled',
        ChecklistNotification.NotificationType.OVERDUE: 'overdue_enabled',
        ChecklistNotification.NotificationType.COMPLETED_LATE: (
            'completed_late_enabled'
        ),
    }[notification_type]
    if not getattr(notification_settings, enabled_field):
        return None
    return schedule, notification_settings


def _scheduled_for(stage, notification_type, schedule):
    if notification_type == ChecklistNotification.NotificationType.DEADLINE_WARNING:
        return stage.deadline_at - timedelta(
            minutes=schedule.warning_minutes_before
        )
    if notification_type == ChecklistNotification.NotificationType.OVERDUE:
        return stage.deadline_at
    return stage.completed_at


def _create_notification(stage, notification_type, scheduled_for):
    _, created = ChecklistNotification.objects.get_or_create(
        stage=stage,
        notification_type=notification_type,
        defaults={'scheduled_for': scheduled_for},
    )
    return created


def schedule_stage_notifications(stage):
    stage = (
        DailyChecklistStage.objects.select_related(
            'daily_checklist__store__checklist_schedule',
            'daily_checklist__store__notification_settings',
        )
        .get(pk=stage.pk)
    )
    if stage.status in FINAL_STAGE_STATUSES:
        return 0
    created = 0
    for notification_type in (
        ChecklistNotification.NotificationType.DEADLINE_WARNING,
        ChecklistNotification.NotificationType.OVERDUE,
    ):
        delivery = _delivery_settings(stage, notification_type)
        if delivery is None:
            continue
        schedule, _ = delivery
        created += int(
            _create_notification(
                stage,
                notification_type,
                _scheduled_for(stage, notification_type, schedule),
            )
        )
    return created


def schedule_due_notifications(at=None):
    at = _aware_now(at)
    created = 0
    stages = DailyChecklistStage.objects.select_related(
        'daily_checklist__store__checklist_schedule',
        'daily_checklist__store__notification_settings',
    ).exclude(status__in=FINAL_STAGE_STATUSES)
    for stage in stages.iterator():
        for notification_type in (
            ChecklistNotification.NotificationType.DEADLINE_WARNING,
            ChecklistNotification.NotificationType.OVERDUE,
        ):
            delivery = _delivery_settings(stage, notification_type)
            if delivery is None:
                continue
            schedule, _ = delivery
            scheduled_for = _scheduled_for(stage, notification_type, schedule)
            if scheduled_for <= at:
                created += int(
                    _create_notification(stage, notification_type, scheduled_for)
                )
    return created


def create_completed_late_notification(stage):
    stage = DailyChecklistStage.objects.select_related(
        'daily_checklist__store__checklist_schedule',
        'daily_checklist__store__notification_settings',
    ).get(pk=stage.pk)
    notification_type = ChecklistNotification.NotificationType.COMPLETED_LATE
    if (
        stage.status != DailyChecklistStage.Status.COMPLETED_LATE
        or stage.completed_at is None
        or _delivery_settings(stage, notification_type) is None
    ):
        return None
    notification, _ = ChecklistNotification.objects.get_or_create(
        stage=stage,
        notification_type=notification_type,
        defaults={'scheduled_for': stage.completed_at},
    )
    return notification


@transaction.atomic
def claim_notification(notification_id, at=None):
    at = _aware_now(at)
    notification = (
        ChecklistNotification.objects.select_for_update()
        .select_related('stage')
        .get(pk=notification_id)
    )
    if notification.scheduled_for > at:
        return None
    can_claim = notification.status in {
        ChecklistNotification.Status.PENDING,
        ChecklistNotification.Status.FAILED,
    }
    if notification.status == ChecklistNotification.Status.SENDING:
        can_claim = (
            notification.sending_started_at is None
            or notification.sending_started_at <= at - STALE_SENDING_AFTER
        )
    if not can_claim:
        return None
    notification.status = ChecklistNotification.Status.SENDING
    notification.attempts += 1
    notification.sending_started_at = at
    notification.save(
        update_fields=(
            'status',
            'attempts',
            'sending_started_at',
            'updated_at',
        )
    )
    return notification


def _store_timezone(stage):
    try:
        return ZoneInfo(stage.daily_checklist.store.timezone)
    except ZoneInfoNotFoundError:
        return ZoneInfo('UTC')


def _format_datetime(value, stage):
    return value.astimezone(_store_timezone(stage)).strftime('%d.%m.%Y %H:%M')


def _format_duration(value):
    seconds = max(0, int(value.total_seconds()))
    hours, remainder = divmod(seconds, 3600)
    minutes, _ = divmod(remainder, 60)
    return f'{hours:02d}:{minutes:02d}'


def _stage_counts(stage):
    answers = list(
        ChecklistAnswer.objects.filter(
            daily_item__daily_checklist=stage.daily_checklist,
            daily_item__section_code=stage.section_code,
        ).select_related('daily_item')
    )
    pending_count = sum(
        (
            answer.daily_item.answer_type_snapshot
            == ChecklistItem.AnswerType.INTEGER
            and answer.integer_value is None
        )
        or (
            answer.daily_item.answer_type_snapshot
            == ChecklistItem.AnswerType.STATUS
            and answer.status == ChecklistAnswer.Status.PENDING
        )
        for answer in answers
    )
    return len(answers) - pending_count, pending_count, len(answers)


def build_notification_text(notification):
    stage = notification.stage
    daily = stage.daily_checklist
    store_name = html.escape(daily.store.name)
    if daily.employee_id:
        account_user = daily.employee.user
        account_name = account_user.get_full_name() or account_user.get_username()
    else:
        account_name = 'Терминал магазина'
    employee_name = html.escape(account_name)
    stage_name = html.escape(stage.get_section_code_display())
    deadline = _format_datetime(stage.deadline_at, stage)
    completed_count, pending_count, total_count = _stage_counts(stage)

    if notification.notification_type == ChecklistNotification.NotificationType.DEADLINE_WARNING:
        remaining = _format_duration(stage.deadline_at - notification.scheduled_for)
        return (
            '⚠️ <b>Чек-лист скоро просрочится</b>\n\n'
            f'Магазин: {store_name}\n'
            f'Сотрудник: {employee_name}\n'
            f'Этап: {stage_name}\n'
            f'Дедлайн: {deadline}\n'
            f'Осталось: {remaining}\n'
            f'Заполнено: {completed_count} из {total_count}'
        )
    if notification.notification_type == ChecklistNotification.NotificationType.OVERDUE:
        return (
            '🔴 <b>Чек-лист просрочен</b>\n\n'
            f'Магазин: {store_name}\n'
            f'Сотрудник: {employee_name}\n'
            f'Этап: {stage_name}\n'
            f'Дедлайн: {deadline}\n'
            f'Не заполнено: {pending_count} пунктов'
        )
    completed_at = stage.completed_at or notification.scheduled_for
    return (
        '🟠 <b>Этап заполнен с опозданием</b>\n\n'
        f'Магазин: {store_name}\n'
        f'Сотрудник: {employee_name}\n'
        f'Этап: {stage_name}\n'
        f'Дедлайн: {deadline}\n'
        f'Завершено: {_format_datetime(completed_at, stage)}\n'
        f'Опоздание: {_format_duration(completed_at - stage.deadline_at)}'
    )


def send_telegram_message(chat_id, text):
    token = settings.TELEGRAM_BOT_TOKEN
    if not token:
        raise TelegramDeliveryError('Telegram-клиент не настроен.')
    endpoint = f'https://api.telegram.org/bot{token}/sendMessage'
    payload = parse.urlencode(
        {
            'chat_id': chat_id,
            'text': text,
            'parse_mode': 'HTML',
            'disable_web_page_preview': 'true',
        }
    ).encode()
    telegram_request = request.Request(endpoint, data=payload, method='POST')
    try:
        with request.urlopen(
            telegram_request,
            timeout=settings.TELEGRAM_REQUEST_TIMEOUT,
        ) as response:
            raw_response = response.read()
    except error.HTTPError as exc:
        raise TelegramDeliveryError(
            f'Telegram HTTP error: {exc.code}.'
        ) from None
    except (TimeoutError, socket.timeout):
        raise TelegramDeliveryError('Telegram request timed out.') from None
    except error.URLError:
        raise TelegramDeliveryError('Telegram network error.') from None
    except Exception:
        raise TelegramDeliveryError('Telegram transport error.') from None
    try:
        response_data = json.loads(raw_response)
    except (TypeError, ValueError, json.JSONDecodeError):
        raise TelegramDeliveryError('Telegram returned invalid JSON.') from None
    if not isinstance(response_data, dict) or response_data.get('ok') is not True:
        raise TelegramDeliveryError('Telegram API returned ok=false.')
    try:
        return int(response_data['result']['message_id'])
    except (KeyError, TypeError, ValueError):
        raise TelegramDeliveryError(
            'Telegram response has no message ID.'
        ) from None


def mask_chat_id(chat_id):
    value = str(chat_id)
    return f'***{value[-4:]}' if len(value) > 4 else '***'


def _write_notification_audit(notification, action, *, error_message=None):
    stage = notification.stage
    try:
        chat_id = stage.daily_checklist.store.notification_settings.telegram_chat_id
    except StoreNotificationSettings.DoesNotExist:
        chat_id = ''
    value = {
        'notification_type': notification.notification_type,
        'chat_id': mask_chat_id(chat_id),
        'attempts': notification.attempts,
    }
    if error_message:
        value['error'] = error_message
    AuditLog.objects.create(
        actor=None,
        store=stage.daily_checklist.store,
        object_type=notification._meta.label_lower,
        object_id=str(notification.pk),
        action=action,
        new_value=value,
    )


def _reset_after_skip(notification):
    with transaction.atomic():
        locked = ChecklistNotification.objects.select_for_update().get(
            pk=notification.pk
        )
        locked.status = ChecklistNotification.Status.PENDING
        locked.sending_started_at = None
        locked.save(
            update_fields=('status', 'sending_started_at', 'updated_at')
        )


def send_notification(notification):
    notification = ChecklistNotification.objects.select_related(
        'stage__daily_checklist__store__checklist_schedule',
        'stage__daily_checklist__store__notification_settings',
        'stage__daily_checklist__employee__user',
        'stage__daily_checklist__terminal_account__user',
    ).get(pk=notification.pk)
    stage = notification.stage
    if (
        notification.notification_type
        in {
            ChecklistNotification.NotificationType.DEADLINE_WARNING,
            ChecklistNotification.NotificationType.OVERDUE,
        }
        and stage.status in FINAL_STAGE_STATUSES
    ):
        notification.delete()
        return 'skipped'
    delivery = _delivery_settings(stage, notification.notification_type)
    if delivery is None:
        _reset_after_skip(notification)
        return 'skipped'
    _, notification_settings = delivery
    try:
        message_id = send_telegram_message(
            notification_settings.telegram_chat_id,
            build_notification_text(notification),
        )
    except TelegramDeliveryError as exc:
        safe_error = str(exc)
        with transaction.atomic():
            failed = ChecklistNotification.objects.select_for_update().select_related(
                'stage__daily_checklist__store'
            ).get(pk=notification.pk)
            failed.status = ChecklistNotification.Status.FAILED
            failed.last_error = safe_error
            failed.sending_started_at = None
            failed.save(
                update_fields=(
                    'status',
                    'last_error',
                    'sending_started_at',
                    'updated_at',
                )
            )
            _write_notification_audit(
                failed,
                AuditLog.Action.TELEGRAM_NOTIFICATION_FAILED,
                error_message=safe_error,
            )
        return 'failed'

    sent_at = timezone.now()
    with transaction.atomic():
        sent = ChecklistNotification.objects.select_for_update().select_related(
            'stage__daily_checklist__store'
        ).get(pk=notification.pk)
        sent.status = ChecklistNotification.Status.SENT
        sent.sent_at = sent_at
        sent.telegram_message_id = message_id
        sent.last_error = None
        sent.sending_started_at = None
        sent.save(
            update_fields=(
                'status',
                'sent_at',
                'telegram_message_id',
                'last_error',
                'sending_started_at',
                'updated_at',
            )
        )
        _write_notification_audit(
            sent,
            AuditLog.Action.TELEGRAM_NOTIFICATION_SENT,
        )
    return 'sent'


def process_due_notifications(at=None, limit=None):
    at = _aware_now(at)
    result = {
        'created': schedule_due_notifications(at),
        'sent': 0,
        'skipped': 0,
        'failed': 0,
    }
    stale_before = at - STALE_SENDING_AFTER
    candidates = ChecklistNotification.objects.filter(
        Q(
            status__in=(
                ChecklistNotification.Status.PENDING,
                ChecklistNotification.Status.FAILED,
            ),
            scheduled_for__lte=at,
        )
        | Q(
            status=ChecklistNotification.Status.SENDING,
            scheduled_for__lte=at,
        )
        & (
            Q(sending_started_at__lte=stale_before)
            | Q(sending_started_at__isnull=True)
        )
    ).order_by('scheduled_for', 'id')
    candidate_ids = list(candidates.values_list('pk', flat=True))
    if limit is not None:
        candidate_ids = candidate_ids[:limit]
    for notification_id in candidate_ids:
        candidate = ChecklistNotification.objects.select_related(
            'stage__daily_checklist__store__checklist_schedule',
            'stage__daily_checklist__store__notification_settings',
        ).get(pk=notification_id)
        if _delivery_settings(
            candidate.stage,
            candidate.notification_type,
        ) is None:
            result['skipped'] += 1
            continue
        claimed = claim_notification(notification_id, at)
        if claimed is None:
            result['skipped'] += 1
            continue
        outcome = send_notification(claimed)
        result[outcome] += 1
    return result


def preview_due_notifications(at=None, limit=None):
    at = _aware_now(at)
    previews = []
    stages = DailyChecklistStage.objects.select_related(
        'daily_checklist__store__checklist_schedule',
        'daily_checklist__store__notification_settings',
    ).exclude(status__in=FINAL_STAGE_STATUSES)
    for stage in stages:
        for notification_type in (
            ChecklistNotification.NotificationType.DEADLINE_WARNING,
            ChecklistNotification.NotificationType.OVERDUE,
        ):
            delivery = _delivery_settings(stage, notification_type)
            if delivery is None:
                continue
            schedule, notification_settings = delivery
            scheduled_for = _scheduled_for(stage, notification_type, schedule)
            if scheduled_for > at:
                continue
            existing = ChecklistNotification.objects.filter(
                stage=stage,
                notification_type=notification_type,
            ).first()
            if existing and existing.status == ChecklistNotification.Status.SENT:
                continue
            previews.append(
                {
                    'stage_id': stage.pk,
                    'notification_type': notification_type,
                    'scheduled_for': scheduled_for,
                    'would_create': existing is None,
                    'chat_id': mask_chat_id(notification_settings.telegram_chat_id),
                }
            )
    preview_keys = {
        (item['stage_id'], item['notification_type']) for item in previews
    }
    existing_due = ChecklistNotification.objects.select_related(
        'stage__daily_checklist__store__checklist_schedule',
        'stage__daily_checklist__store__notification_settings',
    ).filter(
        scheduled_for__lte=at,
        status__in=(
            ChecklistNotification.Status.PENDING,
            ChecklistNotification.Status.FAILED,
            ChecklistNotification.Status.SENDING,
        ),
    )
    for notification in existing_due:
        key = (notification.stage_id, notification.notification_type)
        delivery = _delivery_settings(
            notification.stage,
            notification.notification_type,
        )
        if key in preview_keys or delivery is None:
            continue
        _, notification_settings = delivery
        previews.append(
            {
                'stage_id': notification.stage_id,
                'notification_type': notification.notification_type,
                'scheduled_for': notification.scheduled_for,
                'would_create': False,
                'chat_id': mask_chat_id(notification_settings.telegram_chat_id),
            }
        )
    previews.sort(key=lambda item: (item['scheduled_for'], item['stage_id']))
    return previews[:limit] if limit is not None else previews
