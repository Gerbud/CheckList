from datetime import timedelta

from django.db import connection, transaction
from django.db.models import Q
from django.utils import timezone

from checklists.models import (
    TelegramInboundJob,
    TelegramOutboundMessage,
    TelegramStoreBinding,
    TelegramSystemSettings,
    TelegramUpdateLog,
)
from checklists.telegram_bot import _safe_update
from checklists.telegram_client import TelegramAPIError, send_telegram_request
from checklists.telegram_queue import enqueue_telegram_message
from checklists.telegram_update_processor import process_logged_telegram_update


STALE_INBOUND_AFTER = timedelta(minutes=10)


def extract_update_metadata(update):
    safe = _safe_update(update)
    message = safe.get('message') or {}
    callback = safe.get('callback_query') or {}
    source = callback.get('from') or message.get('from') or {}
    chat = message.get('chat') or (callback.get('message') or {}).get('chat') or {}
    text = str(message.get('text', '')).strip()
    command = (
        text.split()[0].split('@')[0].lower()[:64]
        if text.startswith('/')
        else ''
    )
    if not command and callback.get('data'):
        command = f"callback:{callback['data']}"[:64]
    update_type = 'callback_query' if callback else ('message' if message else 'unknown')
    return safe, source, chat, update_type, command


@transaction.atomic
def enqueue_inbound_update(update):
    update_id = int(update['update_id'])
    existing = TelegramInboundJob.objects.filter(update_id=update_id).first()
    if existing:
        return existing, False
    safe, source, chat, update_type, command = extract_update_metadata(update)
    binding = TelegramStoreBinding.objects.filter(
        telegram_user_id=source.get('id'),
        is_active=True,
        store__is_active=True,
    ).first()
    log, created = TelegramUpdateLog.objects.get_or_create(
        update_id=update_id,
        defaults={
            'telegram_user_id': source.get('id'),
            'telegram_chat_id': chat.get('id'),
            'update_type': update_type,
            'command': command,
            'payload': safe,
            'response_status': TelegramUpdateLog.ResponseStatus.BACKGROUND,
        },
    )
    if not created and hasattr(log, 'inbound_job'):
        return log.inbound_job, False
    job = TelegramInboundJob.objects.create(
        update_id=update_id,
        update_log=log,
        store=binding.store if binding else None,
        telegram_user_id=source.get('id'),
        telegram_chat_id=chat.get('id'),
        update_type=update_type,
        command=command,
    )
    return job, True


def _queue_ack(job, method, payload, suffix):
    return enqueue_telegram_message(
        store=job.store,
        chat_id=job.telegram_chat_id or '',
        method=method,
        message_type='webhook_ack',
        idempotency_key=f'webhook:{job.update_id}:ack:{suffix}',
        payload=payload,
    )


def send_immediate_ack(job, config=None):
    config = config or TelegramSystemSettings.get_solo()
    safe = job.update_log.payload
    message = safe.get('message') or {}
    callback = safe.get('callback_query') or {}
    source = callback.get('from') or message.get('from') or {}
    if (
        not config.immediate_ack_enabled
        or source.get('is_bot')
        or not job.telegram_chat_id
        or job.update_type not in {'message', 'callback_query'}
    ):
        return {'attempted': 0, 'queued': 0}
    operations = []
    if callback.get('id'):
        operations.append(
            (
                'answerCallbackQuery',
                {'callback_query_id': callback['id']},
                'callback',
            )
        )
    operations.append(
        (
            'sendMessage',
            {
                'chat_id': str(job.telegram_chat_id),
                'text': config.immediate_ack_text,
            },
            'message',
        )
    )
    result = {'attempted': 0, 'queued': 0}
    for method, payload, suffix in operations:
        result['attempted'] += 1
        try:
            send_telegram_request(
                method,
                payload,
                system_settings=config,
                quick=True,
            )
        except TelegramAPIError:
            _queue_ack(job, method, payload, suffix)
            result['queued'] += 1
    return result


def _claim_inbound_jobs(*, limit, retry_failed, store_code, max_attempts):
    now = timezone.now()
    with transaction.atomic():
        eligible = Q(status=TelegramInboundJob.Status.PENDING) | Q(
            status=TelegramInboundJob.Status.PROCESSING,
            locked_at__lte=now - STALE_INBOUND_AFTER,
        )
        if retry_failed:
            eligible |= Q(status=TelegramInboundJob.Status.FAILED)
        query = TelegramInboundJob.objects.filter(
            available_at__lte=now,
            attempts_count__lt=max_attempts,
        ).filter(eligible)
        if store_code:
            query = query.filter(store__code=store_code)
        if connection.features.has_select_for_update_skip_locked:
            query = query.select_for_update(skip_locked=True)
        else:
            query = query.select_for_update()
        jobs = list(query.order_by('available_at', 'id')[:limit])
        for job in jobs:
            job.status = TelegramInboundJob.Status.PROCESSING
            job.locked_at = now
            job.attempts_count += 1
            job.last_error = ''
            job.save(
                update_fields=(
                    'status',
                    'locked_at',
                    'attempts_count',
                    'last_error',
                    'updated_at',
                )
            )
        return [job.pk for job in jobs]


def process_inbound_queue(
    *,
    limit=50,
    retry_failed=False,
    store_code=None,
    max_attempts=3,
):
    ids = _claim_inbound_jobs(
        limit=max(1, min(int(limit), 1000)),
        retry_failed=retry_failed,
        store_code=store_code,
        max_attempts=max(1, int(max_attempts)),
    )
    result = {'claimed': len(ids), 'completed': 0, 'failed': 0}
    for job_id in ids:
        job = TelegramInboundJob.objects.select_related('update_log').get(pk=job_id)
        outcome = process_logged_telegram_update(job.update_log)
        with transaction.atomic():
            locked = TelegramInboundJob.objects.select_for_update().get(pk=job_id)
            if outcome in {'processed', 'duplicate'}:
                locked.status = TelegramInboundJob.Status.COMPLETED
                locked.completed_at = timezone.now()
                locked.last_error = ''
                result['completed'] += 1
            else:
                locked.status = TelegramInboundJob.Status.FAILED
                locked.last_error = 'Update processing failed.'
                locked.available_at = timezone.now() + timedelta(minutes=1)
                result['failed'] += 1
            locked.save()
    return result
