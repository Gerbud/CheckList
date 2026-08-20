from django.db import transaction
from django.utils import timezone

from checklists.models import TelegramSystemSettings
from checklists.telegram_client import TelegramAPIError, send_telegram_request
from warranty.models import WarrantyTelegramSettings, WarrantyTelegramThread


def _config():
    warranty = WarrantyTelegramSettings.get_solo()
    bot = TelegramSystemSettings.get_solo()
    if not warranty.is_enabled:
        raise TelegramAPIError('Telegram гарантийного отдела выключен.')
    if not bot.is_enabled or not bot.bot_token:
        raise TelegramAPIError('Рабочий Telegram-бот проекта не настроен.')
    return warranty, bot


def create_claim_topic(thread):
    warranty, bot = _config()
    response = send_telegram_request(
        'createForumTopic',
        {'chat_id': warranty.chat_id, 'name': thread.title[:128]},
        system_settings=bot,
    )
    result = response.data.get('result') or {}
    topic_id = result.get('message_thread_id')
    if not topic_id:
        raise TelegramAPIError('Telegram не вернул ID созданной темы.')
    with transaction.atomic():
        locked = WarrantyTelegramThread.objects.select_for_update().get(pk=thread.pk)
        locked.chat_id = warranty.chat_id
        locked.topic_id = str(topic_id)
        locked.state = WarrantyTelegramThread.State.ACTIVE
        locked.last_error = ''
        locked.save()
    send_telegram_request(
        'sendMessage',
        {
            'chat_id': warranty.chat_id,
            'message_thread_id': topic_id,
            'text': (
                f'Гарантийное обращение №{thread.claim.external_id}\n'
                f'Клиент: {thread.claim.customer_name or "—"}\n'
                f'Товар: {thread.claim.product_name or "—"}\n'
                f'Неисправность: {thread.claim.defect or "—"}'
            ),
        },
        system_settings=bot,
    )
    return locked


def close_claim_topic(thread):
    warranty, bot = _config()
    send_telegram_request('closeForumTopic', {'chat_id': warranty.chat_id, 'message_thread_id': int(thread.topic_id)}, system_settings=bot)
    thread.state = WarrantyTelegramThread.State.ARCHIVED
    thread.archived_at = timezone.now()
    thread.last_error = ''
    thread.save()
    return thread


def reopen_claim_topic(thread):
    warranty, bot = _config()
    send_telegram_request('reopenForumTopic', {'chat_id': warranty.chat_id, 'message_thread_id': int(thread.topic_id)}, system_settings=bot)
    thread.state = WarrantyTelegramThread.State.ACTIVE
    thread.archived_at = None
    thread.last_error = ''
    thread.save()
    return thread


def sync_warranty_topics(limit=50):
    results = {'created': 0, 'closed': 0, 'reopened': 0, 'failed': 0}
    query = WarrantyTelegramThread.objects.select_related('claim').filter(
        state__in=(WarrantyTelegramThread.State.PLANNED, WarrantyTelegramThread.State.CLOSE_PENDING, WarrantyTelegramThread.State.RESTORE_PENDING)
    ).order_by('id')[:limit]
    for thread in query:
        try:
            if thread.state == WarrantyTelegramThread.State.PLANNED:
                create_claim_topic(thread)
                results['created'] += 1
            elif thread.state == WarrantyTelegramThread.State.CLOSE_PENDING:
                close_claim_topic(thread)
                results['closed'] += 1
            else:
                reopen_claim_topic(thread)
                results['reopened'] += 1
        except (TelegramAPIError, ValueError) as exc:
            thread.state = WarrantyTelegramThread.State.ERROR
            thread.last_error = str(exc)
            thread.save(update_fields=('state', 'last_error', 'updated_at'))
            results['failed'] += 1
    return results
