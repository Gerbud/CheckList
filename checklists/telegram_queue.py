from datetime import timedelta

from django.db import connection, transaction
from django.db.models import Q
from django.utils import timezone

from checklists.models import (
    AuditLog,
    TelegramMessageTemplate,
    TelegramOutboundMessage,
    TelegramStoreBinding,
    TelegramStoreChat,
)
from checklists.telegram_client import TelegramAPIError, send_telegram_request
from checklists.telegram_templates import get_template_or_default, render_template


STALE_PROCESSING_AFTER = timedelta(minutes=5)
PROCESSING_TIMEOUT_ERROR = 'Message processing timeout, returned to queue'


def enqueue_telegram_message(
    *,
    chat_id,
    message_type,
    idempotency_key,
    payload,
    store=None,
    message_thread_id=None,
    method='sendMessage',
    scheduled_at=None,
):
    complete_payload = dict(payload)
    if (
        method != 'answerCallbackQuery'
        and chat_id
        and 'chat_id' not in complete_payload
    ):
        complete_payload['chat_id'] = str(chat_id)
    if message_thread_id is not None:
        complete_payload['message_thread_id'] = message_thread_id
    message, created = TelegramOutboundMessage.objects.get_or_create(
        idempotency_key=idempotency_key,
        defaults={
            'store': store,
            'chat_id': str(chat_id),
            'message_thread_id': message_thread_id,
            'method': method,
            'payload': complete_payload,
            'message_type': message_type,
            'scheduled_at': scheduled_at or timezone.now(),
        },
    )
    message.was_created = created
    return message


def _purpose_for_message_type(message_type):
    if message_type in {'task_created', 'task_completed'}:
        return TelegramStoreChat.Purpose.TASKS
    if message_type in {'task_failed', 'incomplete_tasks'}:
        return TelegramStoreChat.Purpose.FAILURES
    return TelegramStoreChat.Purpose.NOTIFICATIONS


def enqueue_template_message(
    store,
    event_code,
    context,
    *,
    idempotency_key,
    scheduled_at=None,
):
    template = get_template_or_default(store, event_code)
    if not template.is_enabled:
        return []
    text = render_template(template, context)
    base_payload = {'text': text, 'disable_web_page_preview': True}
    if template.parse_mode != TelegramMessageTemplate.ParseMode.PLAIN:
        base_payload['parse_mode'] = template.parse_mode
    destinations = []
    if template.send_to_private:
        destinations.extend(
            (str(binding.telegram_chat_id), None, f'private:{binding.pk}')
            for binding in TelegramStoreBinding.objects.filter(
                store=store,
                is_active=True,
            )
        )
    if template.send_to_group:
        purpose = _purpose_for_message_type(event_code)
        destinations.extend(
            (chat.chat_id, chat.message_thread_id, f'chat:{chat.pk}')
            for chat in TelegramStoreChat.objects.filter(
                store=store,
                is_active=True,
                purpose__in=(purpose, TelegramStoreChat.Purpose.ALL),
            )
        )
    return [
        enqueue_telegram_message(
            store=store,
            chat_id=chat_id,
            message_thread_id=thread_id,
            method='sendMessage',
            payload=base_payload,
            message_type=event_code,
            idempotency_key=f'{idempotency_key}:{destination_key}',
            scheduled_at=scheduled_at,
        )
        for chat_id, thread_id, destination_key in destinations
    ]


def recover_stale_processing_messages(*, at=None):
    now = at or timezone.now()
    with transaction.atomic():
        query = TelegramOutboundMessage.objects.filter(
            status=TelegramOutboundMessage.Status.PROCESSING,
            updated_at__lte=now - STALE_PROCESSING_AFTER,
        )
        if connection.features.has_select_for_update_skip_locked:
            query = query.select_for_update(skip_locked=True)
        else:
            query = query.select_for_update()
        messages = list(query.order_by('id'))
        for message in messages:
            message.status = TelegramOutboundMessage.Status.PENDING
            message.last_error = PROCESSING_TIMEOUT_ERROR
            message.save(update_fields=('status', 'last_error', 'updated_at'))
        return [message.pk for message in messages]


def _claim_messages(
    *,
    limit,
    retry_failed=False,
    store_code=None,
    exclude_ids=(),
):
    now = timezone.now()
    with transaction.atomic():
        eligible = Q(status=TelegramOutboundMessage.Status.PENDING)
        if retry_failed:
            eligible |= Q(status=TelegramOutboundMessage.Status.FAILED)
        query = TelegramOutboundMessage.objects.filter(
            scheduled_at__lte=now,
        ).filter(
            Q(store__isnull=True) | Q(store__is_active=True),
        ).filter(eligible)
        if store_code:
            query = query.filter(store__code=store_code)
        if exclude_ids:
            query = query.exclude(pk__in=exclude_ids)
        if connection.features.has_select_for_update_skip_locked:
            query = query.select_for_update(skip_locked=True)
        else:
            query = query.select_for_update()
        messages = list(query.order_by('scheduled_at', 'id')[:limit])
        for message in messages:
            message.status = TelegramOutboundMessage.Status.PROCESSING
            message.last_error = ''
            message.save(update_fields=('status', 'last_error', 'updated_at'))
        return [message.pk for message in messages]


def _audit_delivery_failure(message):
    AuditLog.objects.create(
        actor=None,
        store=message.store,
        object_type=message._meta.label_lower,
        object_id=str(message.pk),
        action=AuditLog.Action.TELEGRAM_DELIVERY_FAILED,
        new_value={
            'message_type': message.message_type,
            'idempotency_key': message.idempotency_key,
            'error': message.last_error,
        },
    )


def delete_telegram_message(message, *, actor):
    if (
        message.status != TelegramOutboundMessage.Status.SENT
        or not message.telegram_message_id
    ):
        raise TelegramAPIError(
            'Удалить можно только отправленное сообщение с Telegram message ID.',
            retryable=False,
        )
    response = send_telegram_request(
        'deleteMessage',
        {
            'chat_id': message.chat_id,
            'message_id': message.telegram_message_id,
        },
    )
    with transaction.atomic():
        locked = TelegramOutboundMessage.objects.select_for_update().get(
            pk=message.pk
        )
        locked.status = TelegramOutboundMessage.Status.DELETED
        locked.deleted_at = timezone.now()
        locked.deleted_by = actor
        locked.last_error = ''
        locked.alternative_attempts_count += response.alternative_attempts
        locked.official_attempts_count += response.official_attempts
        locked.save(
            update_fields=(
                'status',
                'deleted_at',
                'deleted_by',
                'last_error',
                'alternative_attempts_count',
                'official_attempts_count',
                'updated_at',
            )
        )
    return locked


def process_telegram_queue(*, limit=100, retry_failed=False, store_code=None):
    limit = max(1, min(int(limit), 1000))
    recovered_ids = recover_stale_processing_messages()
    message_ids = _claim_messages(
        limit=limit,
        retry_failed=retry_failed,
        store_code=store_code,
        exclude_ids=recovered_ids,
    )
    result = {
        'recovered': len(recovered_ids),
        'claimed': len(message_ids),
        'sent': 0,
        'failed': 0,
    }
    for message_id in message_ids:
        message = TelegramOutboundMessage.objects.get(pk=message_id)
        try:
            response = send_telegram_request(message.method, message.payload)
        except TelegramAPIError as exc:
            with transaction.atomic():
                locked = TelegramOutboundMessage.objects.select_for_update().get(
                    pk=message_id
                )
                locked.status = TelegramOutboundMessage.Status.FAILED
                locked.alternative_attempts_count += exc.alternative_attempts
                locked.official_attempts_count += exc.official_attempts
                locked.last_error = str(exc)
                locked.save(
                    update_fields=(
                        'status',
                        'alternative_attempts_count',
                        'official_attempts_count',
                        'last_error',
                        'updated_at',
                    )
                )
                _audit_delivery_failure(locked)
            result['failed'] += 1
            continue
        telegram_message_id = None
        api_result = response.data.get('result')
        if isinstance(api_result, dict):
            telegram_message_id = api_result.get('message_id')
        with transaction.atomic():
            locked = TelegramOutboundMessage.objects.select_for_update().get(
                pk=message_id
            )
            locked.status = TelegramOutboundMessage.Status.SENT
            locked.alternative_attempts_count += response.alternative_attempts
            locked.official_attempts_count += response.official_attempts
            locked.telegram_message_id = telegram_message_id
            locked.sent_at = timezone.now()
            locked.last_error = ''
            locked.save(
                update_fields=(
                    'status',
                    'alternative_attempts_count',
                    'official_attempts_count',
                    'telegram_message_id',
                    'sent_at',
                    'last_error',
                    'updated_at',
                )
            )
        result['sent'] += 1
    return result
