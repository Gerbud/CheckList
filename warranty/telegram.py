import html
import re

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from checklists.models import TelegramSystemSettings
from checklists.telegram_client import TelegramAPIError, send_telegram_request
from warranty.models import WarrantyTelegramMessage, WarrantyTelegramSettings, WarrantyTelegramThread


def _config():
    warranty = WarrantyTelegramSettings.get_solo()
    bot = TelegramSystemSettings.get_solo()
    if not warranty.is_enabled:
        raise TelegramAPIError('Telegram гарантийного отдела выключен.')
    if not bot.is_enabled or not bot.bot_token:
        raise TelegramAPIError('Рабочий Telegram-бот проекта не настроен.')
    return warranty, bot


def _product_url(claim):
    raw = claim.raw_source_data or {}
    for key in ('UF_PRODUCT_URL', 'PRODUCT_URL', 'DETAIL_PAGE_URL'):
        value = str(raw.get(key) or '').strip()
        if value.startswith(('https://', 'http://')):
            return value
    product_id = str(claim.external_product_id or '').strip()
    template = getattr(settings, 'WARRANTY_PRODUCT_URL_TEMPLATE', '')
    if not product_id or not template:
        return ''
    return template.format(product_id=product_id)


def _phone_href(phone):
    value = re.sub(r'[^\d+]', '', phone or '')
    return f'tel:{value}' if value else ''


def _claim_message(claim):
    product_name = html.escape(claim.product_name or '—')
    product_url = _product_url(claim)
    product_link = (
        f'\n<a href="{html.escape(product_url, quote=True)}">Открыть товар</a>'
        if product_url else ''
    )
    phone_text = html.escape(claim.phone or '—')
    phone_href = _phone_href(claim.phone)
    phone = (
        f'<a href="{html.escape(phone_href, quote=True)}">{phone_text}</a>'
        if phone_href else phone_text
    )
    return (
        f'<b>Гарантийное обращение №{claim.external_id}</b>\n\n'
        f'<b>Товар</b>\n{product_name}{product_link}\n\n'
        f'<b>Клиент:</b> {html.escape(claim.customer_name or "—")}\n'
        f'<b>Телефон:</b> {phone}\n\n'
        f'<b>Неисправность</b>\n{html.escape(claim.defect or "—")}'
    )


def create_claim_topic(thread):
    if thread.topic_id:
        return thread
    warranty, bot = _config()
    response = send_telegram_request(
        'createForumTopic',
        {'chat_id': warranty.chat_id, 'name': thread.title[:128]},
        system_settings=bot,
        quick=True,
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
    message_response = send_telegram_request(
        'sendMessage',
        {
            'chat_id': warranty.chat_id,
            'message_thread_id': topic_id,
            'text': _claim_message(thread.claim),
            'parse_mode': 'HTML',
            'disable_web_page_preview': True,
        },
        system_settings=bot,
        quick=True,
    )
    message_result = message_response.data.get('result') or {}
    message_id = message_result.get('message_id')
    WarrantyTelegramMessage.objects.create(
        thread=locked,
        telegram_message_id=str(message_id or ''),
        direction='outbound',
        sender_name='Telegram bot',
        text=_claim_message(thread.claim),
        payload=message_result if isinstance(message_result, dict) else {},
    )
    return locked


def record_warranty_update(update):
    message = update.get('message') or update.get('edited_message') or {}
    chat_id = str((message.get('chat') or {}).get('id') or '')
    topic_id = str(message.get('message_thread_id') or '')
    message_id = str(message.get('message_id') or '')
    if not chat_id or not topic_id or not message_id:
        return False
    thread = WarrantyTelegramThread.objects.filter(
        chat_id=chat_id,
        topic_id=topic_id,
    ).first()
    if not thread:
        return False
    sender = message.get('from') or {}
    sender_name = ' '.join(filter(None, (
        str(sender.get('first_name') or '').strip(),
        str(sender.get('last_name') or '').strip(),
    ))) or str(sender.get('username') or '')
    WarrantyTelegramMessage.objects.update_or_create(
        thread=thread,
        telegram_message_id=message_id,
        defaults={
            'direction': 'inbound',
            'sender_external_id': str(sender.get('id') or ''),
            'sender_name': sender_name[:255],
            'text': str(message.get('text') or message.get('caption') or ''),
            'payload': {
                'message_id': message.get('message_id'),
                'message_thread_id': message.get('message_thread_id'),
                'date': message.get('date'),
            },
        },
    )
    return True


def close_claim_topic(thread):
    warranty, bot = _config()
    send_telegram_request('closeForumTopic', {'chat_id': warranty.chat_id, 'message_thread_id': int(thread.topic_id)}, system_settings=bot, quick=True)
    thread.state = WarrantyTelegramThread.State.ARCHIVED
    thread.archived_at = timezone.now()
    thread.last_error = ''
    thread.save()
    return thread


def reopen_claim_topic(thread):
    warranty, bot = _config()
    send_telegram_request('reopenForumTopic', {'chat_id': warranty.chat_id, 'message_thread_id': int(thread.topic_id)}, system_settings=bot, quick=True)
    thread.state = WarrantyTelegramThread.State.ACTIVE
    thread.archived_at = None
    thread.last_error = ''
    thread.save()
    return thread


def sync_warranty_topics(limit=50):
    results = {'created': 0, 'closed': 0, 'reopened': 0, 'failed': 0, 'rate_limited': 0}
    query = WarrantyTelegramThread.objects.select_related('claim').filter(
        state__in=(WarrantyTelegramThread.State.PLANNED, WarrantyTelegramThread.State.CLOSE_PENDING, WarrantyTelegramThread.State.RESTORE_PENDING)
    ).order_by('id')[:limit]
    for thread in query:
        original_state = thread.state
        if original_state == WarrantyTelegramThread.State.PLANNED:
            claimed = WarrantyTelegramThread.objects.filter(
                pk=thread.pk,
                state=WarrantyTelegramThread.State.PLANNED,
                topic_id='',
            ).update(state=WarrantyTelegramThread.State.CREATING)
            if not claimed:
                continue
            thread.state = WarrantyTelegramThread.State.CREATING
        try:
            if original_state == WarrantyTelegramThread.State.PLANNED:
                create_claim_topic(thread)
                results['created'] += 1
            elif thread.state == WarrantyTelegramThread.State.CLOSE_PENDING:
                close_claim_topic(thread)
                results['closed'] += 1
            else:
                reopen_claim_topic(thread)
                results['reopened'] += 1
        except TelegramAPIError as exc:
            if exc.status_code == 429:
                thread.last_error = str(exc)
                thread.save(update_fields=('last_error', 'updated_at'))
                results['rate_limited'] += 1
                break
            thread.state = WarrantyTelegramThread.State.ERROR
            thread.last_error = str(exc)
            thread.save(update_fields=('state', 'last_error', 'updated_at'))
            results['failed'] += 1
        except ValueError as exc:
            thread.state = WarrantyTelegramThread.State.ERROR
            thread.last_error = str(exc)
            thread.save(update_fields=('state', 'last_error', 'updated_at'))
            results['failed'] += 1
    return results
