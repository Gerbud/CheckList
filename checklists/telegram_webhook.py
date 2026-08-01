import json
import secrets

from django.conf import settings
from django.http import HttpResponse, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from checklists.models import (
    TelegramOutboundMessage,
    TelegramSystemSettings,
    TelegramUpdateLog,
)
from checklists.telegram_client import TelegramAPIError, send_telegram_request
from checklists.telegram_inbound import enqueue_inbound_update
from checklists.telegram_queue import enqueue_telegram_message
from checklists.telegram_update_processor import (
    UpdateMode,
    classify_telegram_update,
    process_telegram_update,
)


def _deliver_synchronous_actions(result, config):
    delivered = 0
    queued = 0
    failed = 0
    errors = []
    for action in result.actions:
        message = enqueue_telegram_message(
            store=action.store,
            chat_id=action.chat_id,
            method=action.method,
            message_type=f'webhook_{action.message_type}'[:64],
            idempotency_key=f'webhook:{action.idempotency_key}',
            payload=action.payload,
        )
        try:
            response = send_telegram_request(
                action.method,
                action.payload,
                system_settings=config,
                quick=True,
            )
        except TelegramAPIError as exc:
            message.status = (
                TelegramOutboundMessage.Status.PENDING
                if exc.retryable
                else TelegramOutboundMessage.Status.FAILED
            )
            message.alternative_attempts_count += exc.alternative_attempts
            message.official_attempts_count += exc.official_attempts
            message.last_error = str(exc)
            message.save(
                update_fields=(
                    'status',
                    'alternative_attempts_count',
                    'official_attempts_count',
                    'last_error',
                    'updated_at',
                )
            )
            errors.append(str(exc))
            if exc.retryable:
                queued += 1
            else:
                failed += 1
        else:
            api_result = response.data.get('result')
            message.status = TelegramOutboundMessage.Status.SENT
            message.alternative_attempts_count += response.alternative_attempts
            message.official_attempts_count += response.official_attempts
            message.telegram_message_id = (
                api_result.get('message_id')
                if isinstance(api_result, dict)
                else None
            )
            message.sent_at = timezone.now()
            message.last_error = ''
            message.save(
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
            delivered += 1
    return {
        'delivered': delivered,
        'queued': queued,
        'failed': failed,
        'errors': errors,
    }


def _record_response(update_id, result, delivery=None):
    status = TelegramUpdateLog.ResponseStatus.IGNORED
    error = result.safe_error or ''
    if result.outcome == 'failed':
        status = TelegramUpdateLog.ResponseStatus.FAILED
    elif delivery:
        if delivery['failed']:
            status = TelegramUpdateLog.ResponseStatus.FAILED
        elif delivery['queued']:
            status = TelegramUpdateLog.ResponseStatus.QUEUED
        elif delivery['delivered']:
            status = TelegramUpdateLog.ResponseStatus.SENT
        error = '; '.join(delivery['errors'])[:2000]
    TelegramUpdateLog.objects.filter(update_id=update_id).update(
        response_status=status,
        response_error=error,
        responded_at=timezone.now(),
    )


@csrf_exempt
def telegram_webhook(request):
    if request.method != 'POST':
        return HttpResponse(status=405)
    config = TelegramSystemSettings.get_solo()
    supplied = request.headers.get('X-Telegram-Bot-Api-Secret-Token', '')
    expected = config.webhook_secret_token
    if not expected or not secrets.compare_digest(supplied, expected):
        return HttpResponse(status=403)
    if not request.content_type == 'application/json':
        return JsonResponse({'error': 'Content-Type must be application/json.'}, status=400)
    content_length = request.META.get('CONTENT_LENGTH')
    try:
        too_large = (
            bool(content_length)
            and int(content_length) > settings.TELEGRAM_WEBHOOK_MAX_BODY_BYTES
        )
    except (TypeError, ValueError):
        return JsonResponse({'error': 'Invalid Content-Length.'}, status=400)
    if too_large:
        return JsonResponse({'error': 'Request body is too large.'}, status=413)
    body = request.body
    if len(body) > settings.TELEGRAM_WEBHOOK_MAX_BODY_BYTES:
        return JsonResponse({'error': 'Request body is too large.'}, status=413)
    try:
        update = json.loads(body)
        if not isinstance(update, dict) or 'update_id' not in update:
            raise ValueError
        int(update['update_id'])
    except (TypeError, ValueError, json.JSONDecodeError):
        return JsonResponse({'error': 'Invalid Telegram update JSON.'}, status=400)
    mode = classify_telegram_update(update)
    if mode == UpdateMode.BACKGROUND:
        enqueue_inbound_update(update)
    else:
        result = process_telegram_update(update, collect_actions=True)
        if result.outcome != 'duplicate':
            delivery = _deliver_synchronous_actions(result, config)
            _record_response(int(update['update_id']), result, delivery)
    return JsonResponse({'ok': True})
