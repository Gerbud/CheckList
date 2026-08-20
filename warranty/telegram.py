import html
import hashlib
import re
from functools import lru_cache
from pathlib import Path
from urllib import error, request

from django.conf import settings
from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone

from checklists.models import TelegramSystemSettings
from checklists.telegram_client import OFFICIAL_API_BASE_URL, TelegramAPIError, send_telegram_request
from warranty.models import WarrantyAttachment, WarrantyTelegramMessage, WarrantyTelegramSettings, WarrantyTelegramStatusButton, WarrantyTelegramThread


TOPIC_STATUS_EMOJI = {
    'new': '🆕',
    'service_decision': '❓',
    'in_progress': '🛠',
    'customer_wait': '👤',
    'diagnostics': '🔍',
    'parts_wait': '📦',
    'ready': '✅',
    'closed': '🔒',
}


@lru_cache(maxsize=1)
def _forum_topic_icons():
    _, bot = _config()
    response = send_telegram_request(
        'getForumTopicIconStickers', {}, system_settings=bot, quick=True,
    )
    stickers = response.data.get('result') or []
    return tuple(
        (str(item.get('emoji') or ''), str(item.get('custom_emoji_id') or ''))
        for item in stickers
        if isinstance(item, dict) and item.get('custom_emoji_id')
    )


def _topic_icon_id(status):
    icons = _forum_topic_icons()
    if not icons:
        raise TelegramAPIError('Telegram не вернул доступные иконки тем.')
    desired = TOPIC_STATUS_EMOJI.get(status, TOPIC_STATUS_EMOJI['new'])
    exact = next((icon_id for emoji, icon_id in icons if emoji == desired), '')
    if exact:
        return exact
    statuses = tuple(TOPIC_STATUS_EMOJI)
    return icons[statuses.index(status) % len(icons)][1]


def update_claim_topic_icon(thread, *, bot=None):
    if not thread.topic_id:
        return thread
    warranty, configured_bot = _config()
    bot = bot or configured_bot
    try:
        send_telegram_request(
            'editForumTopic',
            {
                'chat_id': warranty.chat_id,
                'message_thread_id': int(thread.topic_id),
                'icon_custom_emoji_id': _topic_icon_id(thread.claim.status),
            },
            system_settings=bot,
            quick=True,
            retry_on_failure=False,
        )
    except TelegramAPIError as exc:
        if exc.status_code != 400 or 'TOPIC_NOT_MODIFIED' not in str(exc):
            raise
    return thread


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


def _claim_url(claim):
    template = getattr(
        settings,
        'WARRANTY_CLAIM_URL_TEMPLATE',
        'https://pinel.ru/personal/warranty-claims-list/?search_str={claim_id}',
    )
    return template.format(claim_id=claim.external_id) if template else ''


def _phone_href(phone):
    value = re.sub(r'[^\d+]', '', phone or '')
    return f'tel:{value}' if value else ''


def _claim_message(claim):
    claim_url = _claim_url(claim)
    claim_link = (
        f'\n<a href="{html.escape(claim_url, quote=True)}">Открыть обращение на сайте</a>'
        if claim_url else ''
    )
    product_name = html.escape(claim.product_name or '—')
    product_url = _product_url(claim)
    product = (
        f'<a href="{html.escape(product_url, quote=True)}">{product_name}</a>'
        if product_url else product_name
    )
    phone_text = html.escape(claim.phone or '—')
    phone_href = _phone_href(claim.phone)
    phone = (
        f'<a href="{html.escape(phone_href, quote=True)}">{phone_text}</a>'
        if phone_href else phone_text
    )
    purchased_from_us = 'Да' if claim.purchased_from_us else 'Нет'
    product_location = (
        'у клиента'
        if claim.product_remains_with_customer
        else 'в сервисном центре'
    )
    return (
        f'<b>Гарантийное обращение №{claim.external_id}</b>{claim_link}\n\n'
        f'<b>Статус:</b> {html.escape(claim.get_status_display())}\n'
        f'<b>Куплено у нас:</b> {purchased_from_us}\n'
        f'<b>Товар находится:</b> {product_location}\n\n'
        f'<b>Товар</b>\n{product}\n\n'
        f'<b>Клиент:</b> {html.escape(claim.customer_name or "—")}\n'
        f'<b>Телефон:</b> {phone}\n\n'
        f'<b>Неисправность</b>\n{html.escape(claim.defect or "—")}'
    )


def _status_keyboard(claim):
    buttons = WarrantyTelegramStatusButton.objects.filter(
        source_status=claim.status,
        is_enabled=True,
    ).order_by('position', 'id')
    return {
        'inline_keyboard': [[{
            'text': button.label,
            'callback_data': f'warranty:{claim.pk}:{button.pk}',
        }] for button in buttons]
    }


def create_claim_topic(thread):
    if thread.topic_id:
        return thread
    warranty, bot = _config()
    response = send_telegram_request(
        'createForumTopic',
        {
            'chat_id': warranty.chat_id,
            'name': thread.title[:128],
            'icon_custom_emoji_id': _topic_icon_id(thread.claim.status),
        },
        system_settings=bot,
        incoming=True,
        retry_on_failure=False,
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
    payload = {
        'chat_id': warranty.chat_id,
        'message_thread_id': topic_id,
        'text': _claim_message(thread.claim),
        'parse_mode': 'HTML',
        'disable_web_page_preview': True,
    }
    keyboard = _status_keyboard(thread.claim)
    if keyboard['inline_keyboard']:
        payload['reply_markup'] = keyboard
    message_response = send_telegram_request(
        'sendMessage',
        payload,
        system_settings=bot,
        incoming=True,
        retry_on_failure=False,
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


def _telegram_file_entries(message):
    entries = []
    document_types = (
        ('document', 'document.bin'),
        ('video', 'video.mp4'),
        ('audio', 'audio.mp3'),
        ('voice', 'voice.ogg'),
        ('animation', 'animation.mp4'),
        ('video_note', 'video-note.mp4'),
    )
    for key, fallback_name in document_types:
        item = message.get(key)
        if isinstance(item, dict) and item.get('file_id'):
            entries.append({
                'file_id': str(item['file_id']),
                'file_unique_id': str(item.get('file_unique_id') or item['file_id']),
                'file_name': str(item.get('file_name') or fallback_name),
                'content_type': str(item.get('mime_type') or ''),
                'file_size': int(item.get('file_size') or 0),
            })
    photos = message.get('photo') or []
    if isinstance(photos, list):
        candidates = [item for item in photos if isinstance(item, dict) and item.get('file_id')]
        if candidates:
            item = max(candidates, key=lambda value: int(value.get('file_size') or 0))
            entries.append({
                'file_id': str(item['file_id']),
                'file_unique_id': str(item.get('file_unique_id') or item['file_id']),
                'file_name': f"photo-{message.get('message_id') or 'telegram'}.jpg",
                'content_type': 'image/jpeg',
                'file_size': int(item.get('file_size') or 0),
            })
    return entries


def _download_telegram_file(file_id, bot):
    response = send_telegram_request(
        'getFile',
        {'file_id': file_id},
        system_settings=bot,
        incoming=True,
    )
    result = response.data.get('result') or {}
    file_path = str(result.get('file_path') or '').lstrip('/')
    if not file_path or '..' in Path(file_path).parts:
        raise TelegramAPIError('Telegram не вернул безопасный путь файла.')
    url = f'{OFFICIAL_API_BASE_URL}/file/bot{bot.bot_token}/{file_path}'
    max_bytes = settings.WARRANTY_TELEGRAM_FILE_MAX_BYTES
    try:
        with request.urlopen(url, timeout=bot.request_timeout_seconds) as response_handle:
            content = response_handle.read(max_bytes + 1)
    except (TimeoutError, error.URLError) as exc:
        raise TelegramAPIError('Не удалось скачать файл гарантийного обращения из Telegram.') from exc
    if len(content) > max_bytes:
        raise TelegramAPIError('Файл гарантийного обращения превышает допустимый размер.', retryable=False)
    return content, file_path


def _save_message_attachments(thread, message):
    entries = _telegram_file_entries(message)
    if not entries:
        return []
    _, bot = _config()
    saved = []
    for entry in entries:
        digest = hashlib.sha256(entry['file_unique_id'].encode('utf-8')).hexdigest()
        external_id = f'telegram:{digest[:55]}'
        attachment, created = WarrantyAttachment.objects.get_or_create(
            claim=thread.claim,
            external_file_id=external_id,
            defaults={
                'original_name': Path(entry['file_name']).name[:500],
                'content_type': entry['content_type'][:255],
                'size': entry['file_size'],
                'source_path': f"telegram:{entry['file_id']}",
            },
        )
        if created or not attachment.file:
            content, telegram_path = _download_telegram_file(entry['file_id'], bot)
            file_name = Path(entry['file_name'] or telegram_path).name or 'telegram-file'
            attachment.size = len(content)
            attachment.source_path = f"telegram:{entry['file_id']}"
            attachment.file.save(file_name[:255], ContentFile(content), save=True)
        saved.append(attachment)
    return saved


def record_warranty_update(update):
    callback = update.get('callback_query') or {}
    callback_data = str(callback.get('data') or '')
    if callback_data.startswith('warranty:'):
        return _handle_status_callback(callback, callback_data)
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
                'attachments': [
                    {
                        'file_id': item['file_id'],
                        'file_unique_id': item['file_unique_id'],
                        'file_name': item['file_name'],
                    }
                    for item in _telegram_file_entries(message)
                ],
            },
        },
    )
    _save_message_attachments(thread, message)
    return True


def _handle_status_callback(callback, callback_data):
    try:
        prefix, claim_id, button_id = callback_data.split(':', 2)
        claim_id, button_id = int(claim_id), int(button_id)
    except (TypeError, ValueError):
        return False
    message = callback.get('message') or {}
    chat_id = str((message.get('chat') or {}).get('id') or '')
    topic_id = str(message.get('message_thread_id') or '')
    thread = WarrantyTelegramThread.objects.select_related('claim').filter(
        claim_id=claim_id, chat_id=chat_id, topic_id=topic_id,
    ).first()
    button = WarrantyTelegramStatusButton.objects.filter(pk=button_id).first()
    if not thread or not button:
        return False
    sender = callback.get('from') or {}
    actor_name = ' '.join(filter(None, (
        str(sender.get('first_name') or '').strip(),
        str(sender.get('last_name') or '').strip(),
    ))) or str(sender.get('username') or sender.get('id') or 'Telegram')
    from warranty.services import apply_telegram_status_button
    claim, changed = apply_telegram_status_button(
        claim_id=claim_id, button=button, actor_name=actor_name[:255],
    )
    warranty, bot = _config()
    callback_payload = {'callback_query_id': callback.get('id')}
    if not changed:
        callback_payload.update({'text': 'Статус обращения уже изменён.', 'show_alert': True})
    else:
        callback_payload['text'] = f'Статус: {claim.get_status_display()}'
    send_telegram_request('answerCallbackQuery', callback_payload, system_settings=bot, quick=True)
    message_id = message.get('message_id')
    if message_id:
        send_telegram_request(
            'editMessageText',
            {
                'chat_id': warranty.chat_id,
                'message_id': message_id,
                'text': _claim_message(claim),
                'parse_mode': 'HTML',
                'disable_web_page_preview': True,
                'reply_markup': _status_keyboard(claim),
            },
            system_settings=bot,
            quick=True,
        )
    if changed:
        response = send_telegram_request(
            'sendMessage',
            {
                'chat_id': warranty.chat_id,
                'message_thread_id': int(thread.topic_id),
                'text': f'✅ {button.label}\nНовый статус: {claim.get_status_display()}\nИзменил: {actor_name}',
            },
            system_settings=bot,
            quick=True,
            retry_on_failure=False,
        )
        result = response.data.get('result') or {}
        WarrantyTelegramMessage.objects.create(
            thread=thread,
            telegram_message_id=str(result.get('message_id') or ''),
            direction='outbound',
            sender_name='Telegram bot',
            text=f'{button.label}: {claim.get_status_display()}',
            payload=result if isinstance(result, dict) else {},
        )
    return True


def close_claim_topic(thread):
    warranty, bot = _config()
    update_claim_topic_icon(thread, bot=bot)
    send_telegram_request('closeForumTopic', {'chat_id': warranty.chat_id, 'message_thread_id': int(thread.topic_id)}, system_settings=bot, quick=True)
    thread.state = WarrantyTelegramThread.State.ARCHIVED
    thread.archived_at = timezone.now()
    thread.last_error = ''
    thread.save()
    return thread


def reopen_claim_topic(thread):
    warranty, bot = _config()
    send_telegram_request('reopenForumTopic', {'chat_id': warranty.chat_id, 'message_thread_id': int(thread.topic_id)}, system_settings=bot, quick=True)
    update_claim_topic_icon(thread, bot=bot)
    thread.state = WarrantyTelegramThread.State.ACTIVE
    thread.archived_at = None
    thread.last_error = ''
    thread.save()
    return thread


def sync_warranty_topics(limit=50):
    results = {'created': 0, 'closed': 0, 'reopened': 0, 'updated': 0, 'failed': 0, 'rate_limited': 0}
    query = WarrantyTelegramThread.objects.select_related('claim').filter(
        state__in=(WarrantyTelegramThread.State.PLANNED, WarrantyTelegramThread.State.CLOSE_PENDING, WarrantyTelegramThread.State.RESTORE_PENDING, WarrantyTelegramThread.State.STATUS_UPDATE_PENDING)
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
            elif thread.state == WarrantyTelegramThread.State.RESTORE_PENDING:
                reopen_claim_topic(thread)
                results['reopened'] += 1
            else:
                update_claim_topic_icon(thread)
                thread.state = WarrantyTelegramThread.State.ACTIVE
                thread.last_error = ''
                thread.save(update_fields=('state', 'last_error', 'updated_at'))
                results['updated'] += 1
        except TelegramAPIError as exc:
            if exc.status_code == 429:
                thread.last_error = str(exc)
                thread.save(update_fields=('last_error', 'updated_at'))
                results['rate_limited'] += 1
                break
            if exc.retryable:
                thread.state = original_state
                thread.last_error = str(exc)
                thread.save(update_fields=('state', 'last_error', 'updated_at'))
                results['failed'] += 1
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
