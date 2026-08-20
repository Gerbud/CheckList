import base64
import json
import re
import secrets
import subprocess
import tempfile
from datetime import datetime
from io import BytesIO
from pathlib import Path
from urllib import error, parse, request

from django.core.files.base import ContentFile
from django.db import transaction
from django.http import HttpResponse, JsonResponse
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views.decorators.csrf import csrf_exempt
from PIL import Image, ImageOps

from checklists.price_tags import ProductImportError, find_pinel_product
from warranty.bitrix_sync import BitrixSyncError, BitrixWarrantyClient
from warranty.models import WarrantyCustomerBotSettings, WarrantyCustomerDocument, WarrantyCustomerProfile, WarrantyCustomerSession, WarrantyCustomerSupportMessage, WarrantyCustomerSupportThread, WarrantyCustomerUpdate, WarrantyProductRegistration


class OpenAIModelUnavailable(RuntimeError):
    pass


def _telegram(config, method, payload):
    url = f'https://api.telegram.org/bot{config.bot_token}/{method}'
    body = json.dumps(payload, ensure_ascii=False).encode()
    try:
        with request.urlopen(request.Request(url, data=body, headers={'Content-Type': 'application/json'}), timeout=15) as response:
            data = json.loads(response.read())
    except (error.URLError, TimeoutError, ValueError) as exc:
        raise RuntimeError('Telegram временно недоступен.') from exc
    if not data.get('ok'):
        description = str(data.get('description') or 'неизвестная ошибка')
        raise RuntimeError(f'Telegram не принял запрос: {description}')
    return data.get('result')


def _send(config, session, text, **extra):
    result = _telegram(config, 'sendMessage', {'chat_id': session.chat_id, 'text': text, **extra})
    if isinstance(result, dict) and result.get('message_id'):
        _remember(session, result['message_id'])
        session.save(update_fields=('telegram_message_ids', 'updated_at'))
    return result


def _answer_callback(config, callback, text=''):
    payload = {'callback_query_id': callback['id']}
    if text:
        payload['text'] = text
    try:
        return _telegram(config, 'answerCallbackQuery', payload)
    except RuntimeError:
        # Telegram accepts callback answers only for a short time. The webhook
        # may be retried after that window, but the customer flow must continue.
        return None


def _remember(session, message_id):
    value = str(message_id or '')
    if value and value not in session.telegram_message_ids:
        session.telegram_message_ids = [*session.telegram_message_ids, value]


def _download_photo(config, file_id):
    info = _telegram(config, 'getFile', {'file_id': file_id})
    path = info.get('file_path', '')
    if not path:
        raise RuntimeError('Telegram не вернул файл.')
    with request.urlopen(f'https://api.telegram.org/file/bot{config.bot_token}/{path}', timeout=20) as response:
        content = response.read(20 * 1024 * 1024 + 1)
        content_type = response.headers.get_content_type()
    if len(content) > 20 * 1024 * 1024:
        raise ValueError('Файл больше 20 МБ.')
    return content, content_type, path.rsplit('.', 1)[-1]


def _prepare_ocr_image(content):
    try:
        image = ImageOps.exif_transpose(Image.open(BytesIO(content))).convert('RGB')
        image.thumbnail((1800, 1800))
        output = BytesIO()
        image.save(output, 'JPEG', quality=85, optimize=True)
        return output.getvalue(), 'image/jpeg'
    except (OSError, ValueError):
        return content, 'image/jpeg'


def _ocr_space(config, content, content_type):
    if not config.ocr_space_api_key:
        return ''
    body = parse.urlencode({
        'apikey': config.ocr_space_api_key,
        'language': 'rus',
        'OCREngine': '2',
        'scale': 'true',
        'isOverlayRequired': 'false',
        'base64Image': f'data:{content_type};base64,{base64.b64encode(content).decode()}',
    }).encode()
    api_request = request.Request('https://api.ocr.space/parse/image', data=body)
    with request.urlopen(api_request, timeout=45) as response:
        data = json.loads(response.read())
    if data.get('IsErroredOnProcessing'):
        raise RuntimeError('OCR.space не смог распознать изображение.')
    return '\n'.join(str(item.get('ParsedText') or '') for item in data.get('ParsedResults', []))


def _openai_ocr_with_model(config, content, content_type, kind, model):
    wanted = 'article и serial_number' if kind == 'label' else 'purchase_date'
    payload = {
        'model': model,
        'messages': [{'role': 'user', 'content': [
            {'type': 'text', 'text': f'Распознай {wanted}. Верни только JSON с ключами article, serial_number, purchase_date (YYYY-MM-DD). Не угадывай: неизвестное значение — пустая строка.'},
            {'type': 'image_url', 'image_url': {'url': f'data:{content_type};base64,{base64.b64encode(content).decode()}'}},
        ]}],
        'response_format': {'type': 'json_object'},
        'temperature': 0,
    }
    api_request = request.Request(
        'https://api.openai.com/v1/chat/completions', data=json.dumps(payload).encode(),
        headers={'Authorization': f'Bearer {config.ocr_api_key}', 'Content-Type': 'application/json'},
    )
    try:
        with request.urlopen(api_request, timeout=45) as response:
            data = json.loads(response.read())
    except error.HTTPError as exc:
        try:
            details = json.loads(exc.read())
        except (ValueError, json.JSONDecodeError):
            details = {}
        api_error = details.get('error') if isinstance(details, dict) else {}
        code = str((api_error or {}).get('code') or '').lower()
        message = str((api_error or {}).get('message') or '').lower()
        if exc.code in (400, 403, 404, 410) and (
            code in ('model_not_found', 'model_not_supported', 'unsupported_model')
            or any(word in message for word in ('model', 'deprecated', 'decommissioned'))
        ):
            raise OpenAIModelUnavailable('Выбранная модель OpenAI недоступна.') from exc
        raise
    result = json.loads(data['choices'][0]['message']['content'])
    if isinstance(result, dict):
        result['model'] = model
        return result
    return {}


def _openai_ocr(config, content, content_type, kind):
    if not config.ocr_api_key:
        return {}
    selected = config.ocr_model
    try:
        return _openai_ocr_with_model(config, content, content_type, kind, selected)
    except OpenAIModelUnavailable:
        fallback = config.OPENAI_CHEAPEST_MODEL
        if selected == fallback:
            raise
        result = _openai_ocr_with_model(config, content, content_type, kind, fallback)
        type(config).objects.filter(pk=config.pk).update(ocr_model=fallback)
        config.ocr_model = fallback
        result['fallback_from'] = selected
        return result


def _tesseract(config, content):
    command = (config.tesseract_command or 'tesseract').strip()
    if not command or Path(command).name != command and not Path(command).is_absolute():
        return ''
    try:
        with tempfile.NamedTemporaryFile(suffix='.jpg') as source:
            source.write(content)
            source.flush()
            result = subprocess.run(
                [command, source.name, 'stdout', '-l', 'rus+eng', '--psm', '6'],
                capture_output=True, text=True, timeout=45, check=False,
            )
        return result.stdout if result.returncode == 0 else ''
    except (FileNotFoundError, PermissionError, subprocess.TimeoutExpired, OSError):
        return ''


def _extract_ocr_fields(text, kind):
    normalized = ' '.join(str(text or '').replace('\x0c', ' ').split())
    result = {'raw_text': normalized[:10000]}
    if kind == 'label':
        article = re.search(r'(?:артикул|арт\.?|article|item|model)\s*[:№#-]?\s*([A-ZА-Я0-9][A-ZА-Я0-9._/-]{2,})', normalized, re.I)
        serial = re.search(r'(?:серийный(?:\s+номер)?|serial(?:\s+number)?|s[/\\]?n)\s*[:№#-]?\s*([A-ZА-Я0-9][A-ZА-Я0-9._/-]{3,})', normalized, re.I)
        result.update(article=article.group(1) if article else '', serial_number=serial.group(1) if serial else '')
    elif kind == 'receipt':
        match = re.search(r'(?<!\d)([0-3]?\d)[.\-/]([01]?\d)[.\-/](20\d{2}|\d{2})(?!\d)', normalized)
        if match:
            year = match.group(3) if len(match.group(3)) == 4 else '20' + match.group(3)
            try:
                result['purchase_date'] = datetime(int(year), int(match.group(2)), int(match.group(1))).date().isoformat()
            except ValueError:
                result['purchase_date'] = ''
    return result


def _recognize(config, content, content_type, kind):
    prepared, prepared_type = _prepare_ocr_image(content)
    try:
        result = _openai_ocr(config, prepared, prepared_type, kind)
        if any(result.get(field) for field in ('article', 'serial_number', 'purchase_date')):
            result['provider'] = 'openai'
            return result
    except (error.URLError, TimeoutError, ValueError, KeyError, IndexError, RuntimeError, json.JSONDecodeError):
        pass
    text = ''
    try:
        text = _ocr_space(config, prepared, prepared_type)
    except (error.URLError, TimeoutError, ValueError, RuntimeError, json.JSONDecodeError):
        pass
    if not text:
        text = _tesseract(config, prepared)
        provider = 'tesseract'
    else:
        provider = 'ocr_space'
    result = _extract_ocr_fields(text, kind)
    result['provider'] = provider if text else 'none'
    return result


def _phone(value):
    digits = re.sub(r'\D', '', value or '')
    if len(digits) == 11 and digits[0] in '78':
        return '+7' + digits[1:]
    return '+' + digits if 10 <= len(digits) <= 15 else ''


def _next_missing(session):
    if not session.article:
        session.step = session.Step.ARTICLE
        return 'Не удалось распознать артикул. Напишите его вручную.'
    if not session.serial_number:
        session.step = session.Step.SERIAL
        return 'Не удалось распознать серийный номер. Напишите его вручную.'
    if not session.purchase_date:
        session.step = session.Step.PURCHASE_DATE
        return 'Не удалось распознать дату покупки. Укажите её в формате ДД.ММ.ГГГГ.'
    session.step = session.Step.READY
    return (f'Проверьте данные:\nФИО: {session.full_name}\nТелефон: {session.phone}\n'
            f'Артикул: {session.article}\nСерийный номер: {session.serial_number}\n'
            f'Дата покупки: {session.purchase_date:%d.%m.%Y}')


def _show_next(config, session):
    text = _next_missing(session)
    session.save()
    extra = {}
    if session.step == session.Step.READY:
        if session.mode == session.Mode.REGISTRATION:
            extra['reply_markup'] = {'inline_keyboard': [[{'text': 'Активировать гарантию', 'callback_data': 'warranty:activate'}]]}
        else:
            extra['reply_markup'] = {'inline_keyboard': [[{'text': 'Оформить рекламацию', 'callback_data': 'warranty:create'}]]}
    _send(config, session, text, **extra)


def _menu_keyboard():
    return {'inline_keyboard': [
        [{'text': '✅ Активировать электронную гарантию', 'callback_data': 'flow:registration'}],
        [{'text': '🛠 Оформить рекламацию', 'callback_data': 'flow:claim'}],
    ]}


def _registered_document_ids(session):
    registrations = WarrantyProductRegistration.objects.filter(profile__telegram_user_id=session.telegram_user_id)
    return set(registrations.values_list('label_document_id', flat=True)) | set(registrations.values_list('receipt_document_id', flat=True))


def _active_documents(session):
    return session.documents.exclude(pk__in={value for value in _registered_document_ids(session) if value})


def _clear_active_documents(session):
    _active_documents(session).delete()


def _support_thread(config, session):
    thread = WarrantyCustomerSupportThread.objects.filter(telegram_user_id=session.telegram_user_id).first()
    if thread:
        return thread
    if not config.support_api_chat_id:
        return None
    profile = WarrantyCustomerProfile.objects.filter(telegram_user_id=session.telegram_user_id).first()
    customer_name = (profile.full_name if profile else '') or session.username or f'ID {session.telegram_user_id}'
    title = f'Клиент · {customer_name}'[:128]
    topic = _telegram(config, 'createForumTopic', {'chat_id': config.support_api_chat_id, 'name': title})
    thread = WarrantyCustomerSupportThread.objects.create(
        telegram_user_id=session.telegram_user_id, customer_chat_id=session.chat_id,
        support_chat_id=config.support_api_chat_id, message_thread_id=str(topic['message_thread_id']),
        username=session.username, customer_name=customer_name,
    )
    intro = _telegram(config, 'sendMessage', {
        'chat_id': thread.support_chat_id, 'message_thread_id': int(thread.message_thread_id),
        'text': f'Новый диалог с клиентом\nTelegram ID: {session.telegram_user_id}\nПользователь: @{session.username}' if session.username else f'Новый диалог с клиентом\nTelegram ID: {session.telegram_user_id}',
    })
    if isinstance(intro, dict) and intro.get('message_id'):
        thread.telegram_message_ids = [str(intro['message_id'])]
        thread.save(update_fields=('telegram_message_ids', 'updated_at'))
    return thread


def _route_to_support(config, session, message):
    thread = _support_thread(config, session)
    if not thread:
        _send(config, session, 'Я пока не понял сообщение. Выберите действие кнопкой или отправьте /start.')
        return
    source_message_id = str(message.get('message_id') or '')
    if WarrantyCustomerSupportMessage.objects.filter(thread=thread, direction='customer', source_message_id=source_message_id).exists():
        return
    copied = _telegram(config, 'copyMessage', {
        'chat_id': thread.support_chat_id, 'from_chat_id': session.chat_id,
        'message_id': int(source_message_id), 'message_thread_id': int(thread.message_thread_id),
    })
    copied_id = str((copied or {}).get('message_id') or '')
    WarrantyCustomerSupportMessage.objects.create(
        thread=thread, direction='customer', source_message_id=source_message_id,
        copied_message_id=copied_id, sender_external_id=session.telegram_user_id, payload=message,
    )
    for value in (source_message_id, copied_id):
        if value and value not in thread.telegram_message_ids:
            thread.telegram_message_ids.append(value)
    thread.customer_chat_id = session.chat_id
    thread.save(update_fields=('customer_chat_id', 'telegram_message_ids', 'updated_at'))
    _send(config, session, 'Передал сообщение специалисту 💬 Ответ придёт сюда. А пока можно продолжить оформление кнопками ниже.', reply_markup=_menu_keyboard())


def _handle_support_reply(config, message):
    chat_id = str((message.get('chat') or {}).get('id') or '')
    topic_id = str(message.get('message_thread_id') or '')
    if not config.support_api_chat_id or chat_id != config.support_api_chat_id:
        return False
    if not topic_id or any(key in message for key in ('forum_topic_created', 'forum_topic_closed', 'forum_topic_reopened', 'forum_topic_edited')):
        return True
    thread = WarrantyCustomerSupportThread.objects.filter(support_chat_id=chat_id, message_thread_id=topic_id).first()
    if not thread:
        return True
    source_message_id = str(message.get('message_id') or '')
    if WarrantyCustomerSupportMessage.objects.filter(thread=thread, direction='support', source_message_id=source_message_id).exists():
        return True
    copied = _telegram(config, 'copyMessage', {
        'chat_id': thread.customer_chat_id, 'from_chat_id': chat_id, 'message_id': int(source_message_id),
    })
    copied_id = str((copied or {}).get('message_id') or '')
    sender = message.get('from') or {}
    WarrantyCustomerSupportMessage.objects.create(
        thread=thread, direction='support', source_message_id=source_message_id,
        copied_message_id=copied_id, sender_external_id=str(sender.get('id') or ''), payload=message,
    )
    for value in (source_message_id, copied_id):
        if value and value not in thread.telegram_message_ids:
            thread.telegram_message_ids.append(value)
    thread.save(update_fields=('telegram_message_ids', 'updated_at'))
    return True


def _consent_text(config):
    external_ocr = [name for enabled, name in (
        (config.ocr_api_key, 'OpenAI'), (config.ocr_space_api_key, 'OCR.space'),
    ) if enabled]
    recognition_notice = (
        f'Для распознавания фотографии могут передаваться сервисам {", ".join(external_ocr)}.\n'
        if external_ocr else 'Распознавание выполняется локально без передачи фотографий внешним OCR-сервисам.\n'
    )
    text = config.consent_text_template
    replacements = {
        '{operator}': config.personal_data_operator,
        '{operator_address}': config.personal_data_operator_address,
        '{recognition_notice}': recognition_notice.strip(),
        '{withdrawal_contact}': config.consent_withdrawal_contact,
        '{privacy_policy_url}': config.privacy_policy_url,
    }
    for placeholder, value in replacements.items():
        text = text.replace(placeholder, str(value))
    return text.strip()


def _ask_for_phone(config, session):
    session.step = session.Step.PHONE
    session.save(update_fields=('step', 'updated_at'))
    _send(
        config, session,
        'Спасибо! Теперь поделитесь номером телефона кнопкой ниже — так быстрее и без ошибок.',
        reply_markup={
            'keyboard': [[{'text': 'Поделиться номером телефона 📱', 'request_contact': True}]],
            'resize_keyboard': True, 'one_time_keyboard': True,
        },
    )


def _request_contacts(config, session):
    profile = WarrantyCustomerProfile.objects.filter(
        telegram_user_id=session.telegram_user_id,
        consent_version=config.consent_version,
        consent_revoked_at__isnull=True,
    ).first()
    if profile:
        session.full_name = profile.full_name
        session.phone = profile.phone
        if session.full_name and session.phone:
            _show_next(config, session)
        elif session.phone:
            session.step = session.Step.FULL_NAME
            session.save()
            _send(config, session, 'Осталось указать ФИО полностью.', reply_markup={'remove_keyboard': True})
        else:
            _ask_for_phone(config, session)
        return
    session.step = session.Step.CONSENT
    session.save(update_fields=('step', 'updated_at'))
    _send(config, session, _consent_text(config), reply_markup={'inline_keyboard': [[
        {'text': 'Согласен ✅', 'callback_data': 'consent:accept'},
        {'text': 'Не согласен', 'callback_data': 'consent:decline'},
    ]]})


def _start_collection(config, session):
    profile = WarrantyCustomerProfile.objects.filter(telegram_user_id=session.telegram_user_id, consent_version=config.consent_version, consent_revoked_at__isnull=True).first()
    if profile:
        session.full_name = profile.full_name
        session.phone = profile.phone
    if session.mode == session.Mode.CLAIM and profile and profile.products.exists():
        session.step = session.Step.PRODUCT
        session.save()
        rows = [[{'text': f'{item.article} · {item.serial_number}', 'callback_data': f'claim:product:{item.pk}'}] for item in profile.products.order_by('-activated_at')[:8]]
        rows.append([{'text': 'Другой товар', 'callback_data': 'claim:product:new'}])
        _send(config, session, 'Выберите зарегистрированный товар — этикетку и чек повторно присылать не придётся.', reply_markup={'inline_keyboard': rows})
        return
    session.step = session.Step.LABEL
    session.save()
    action = 'регистрации покупки' if session.mode == session.Mode.REGISTRATION else 'оформления рекламации'
    _send(config, session, f'Начнём с {action}. Пришлите чёткое фото этикетки на товаре 📷')


def _start_flow(config, callback, mode):
    sender = callback.get('from') or {}
    message = callback.get('message') or {}
    session, _ = WarrantyCustomerSession.objects.get_or_create(
        telegram_user_id=str(sender.get('id')), defaults={'chat_id': str((message.get('chat') or {}).get('id') or sender.get('id'))},
    )
    session.chat_id = str((message.get('chat') or {}).get('id') or session.chat_id)
    session.username = str(sender.get('username') or '')[:255]
    session.mode = mode
    _clear_active_documents(session)
    session.article = session.serial_number = ''
    session.purchase_date = session.external_claim_id = None
    session.selected_registration = None
    session.raw_ocr_data = {}
    session.full_name = session.phone = ''
    session.save()
    _start_collection(config, session)
    _answer_callback(config, callback)


def _accept_consent(config, callback):
    sender = callback.get('from') or {}
    session = WarrantyCustomerSession.objects.get(telegram_user_id=str(sender.get('id')))
    if session.step != session.Step.CONSENT:
        _answer_callback(config, callback)
        return
    text = _consent_text(config)
    message_id = str(((callback.get('message') or {}).get('message_id')) or '')
    WarrantyCustomerProfile.objects.update_or_create(
        telegram_user_id=session.telegram_user_id,
        defaults={
            'chat_id': session.chat_id, 'username': session.username,
            'consent_version': config.consent_version, 'consent_text': text,
            'consent_message_id': message_id, 'consent_accepted_at': timezone.now(),
            'consent_revoked_at': None,
        },
    )
    _answer_callback(config, callback, 'Спасибо! Согласие сохранено.')
    _ask_for_phone(config, session)


def _decline_consent(config, callback):
    sender = callback.get('from') or {}
    session = WarrantyCustomerSession.objects.get(telegram_user_id=str(sender.get('id')))
    session.step = session.Step.MENU
    session.save(update_fields=('step', 'updated_at'))
    _clear_active_documents(session)
    _answer_callback(config, callback, 'Хорошо, данные не сохраняем.')
    _send(config, session, 'Без согласия мы не будем собирать данные. Вы можете вернуться в любой момент.', reply_markup=_menu_keyboard())


def _select_claim_product(config, callback, registration_id):
    sender = callback.get('from') or {}
    session = WarrantyCustomerSession.objects.get(telegram_user_id=str(sender.get('id')))
    if registration_id == 'new':
        session.selected_registration = None
        session.step = session.Step.LABEL
        session.save()
        _answer_callback(config, callback)
        _send(config, session, 'Пришлите чёткое фото этикетки на товаре 📷')
        return
    registration = WarrantyProductRegistration.objects.get(
        pk=int(registration_id), profile__telegram_user_id=session.telegram_user_id,
        profile__consent_revoked_at__isnull=True,
    )
    session.selected_registration = registration
    session.article = registration.article
    session.serial_number = registration.serial_number
    session.purchase_date = registration.purchase_date
    session.step = session.Step.WARRANTY_CARD
    session.save()
    _answer_callback(config, callback, 'Товар выбран.')
    _send(config, session, 'Нашёл этикетку и чек 👍 Осталось прислать фото гарантийного талона.')


@transaction.atomic
def _activate_registration(config, callback):
    sender = callback.get('from') or {}
    session = WarrantyCustomerSession.objects.select_for_update().get(telegram_user_id=str(sender.get('id')))
    if session.mode != session.Mode.REGISTRATION or session.step != session.Step.READY:
        _answer_callback(config, callback, 'Сначала заполните данные.')
        return
    profile = WarrantyCustomerProfile.objects.get(telegram_user_id=session.telegram_user_id, consent_version=config.consent_version, consent_revoked_at__isnull=True)
    profile.chat_id, profile.username = session.chat_id, session.username
    profile.full_name, profile.phone = session.full_name, session.phone
    profile.save()
    active_documents = _active_documents(session)
    receipt = active_documents.filter(kind='receipt').order_by('-id').first()
    products = session.raw_ocr_data.get('products') or [{
        'article': session.article,
        'serial_number': session.serial_number,
        'document_id': getattr(active_documents.filter(kind='label').order_by('-id').first(), 'pk', None),
    }]
    registrations = []
    created_count = 0
    for item in products:
        article = str(item.get('article') or '').strip()
        serial_number = str(item.get('serial_number') or '').strip()
        if not article or not serial_number:
            continue
        registration, created = WarrantyProductRegistration.objects.get_or_create(
            profile=profile, serial_number=serial_number,
            defaults={
                'article': article, 'purchase_date': session.purchase_date,
                'label_document_id': item.get('document_id'),
                'receipt_document': receipt,
                'raw_ocr_data': item,
            },
        )
        registrations.append(registration)
        created_count += int(created)
    if not registrations:
        _answer_callback(config, callback, 'Не удалось сохранить товары: проверьте артикулы и серийные номера.')
        return
    session.step = session.Step.SUBMITTED
    session.save(update_fields=('step', 'updated_at'))
    _answer_callback(config, callback, 'Гарантия активирована!')
    already_count = len(registrations) - created_count
    summary = f'Активировано товаров: {created_count}.'
    if already_count:
        summary += f' Уже были зарегистрированы: {already_count}.'
    _send(config, session, f'Готово 🎉 {summary} Общий чек сохранён — если понадобится помощь, приезжать для подачи обращения не нужно.', reply_markup=_menu_keyboard())


def _save_photo(config, session, message, kind):
    photos = message.get('photo') or []
    if not photos:
        if message.get('text'):
            _route_to_support(config, session, message)
        else:
            _send(config, session, 'Пожалуйста, пришлите именно фотографию.')
        return False
    file_id = photos[-1]['file_id']
    content, content_type, extension = _download_photo(config, file_id)
    document = WarrantyCustomerDocument.objects.create(
        session=session, kind=kind, telegram_file_id=file_id,
        telegram_message_id=str(message['message_id']), content_type=content_type,
        file=ContentFile(content, name=f'{kind}-{message["message_id"]}.{extension}'),
    )
    recognized = _recognize(config, content, content_type, kind)
    if kind == 'label':
        session.article = str(recognized.get('article') or '').strip()[:255]
        session.serial_number = str(recognized.get('serial_number') or '').strip()[:255]
        if session.article:
            try:
                recognized['product'] = find_pinel_product(session.article) or {}
            except ProductImportError:
                recognized['product'] = {}
        recognized['document_id'] = document.pk
        raw_data = {**session.raw_ocr_data, kind: recognized}
        if session.mode == session.Mode.REGISTRATION:
            raw_data['products'] = [*(session.raw_ocr_data.get('products') or []), dict(recognized)]
        session.raw_ocr_data = raw_data
    elif kind == 'receipt':
        session.purchase_date = parse_date(str(recognized.get('purchase_date') or ''))
        session.raw_ocr_data = {**session.raw_ocr_data, kind: recognized}
    else:
        session.raw_ocr_data = {**session.raw_ocr_data, kind: recognized}
    return True


def _label_confirmation(session, receipt_prompt=True):
    label_data = session.raw_ocr_data.get('label') or {}
    product = label_data.get('product') or {}
    name = str(product.get('name') or session.article or 'Товар').strip()
    url = str(product.get('url') or '').strip()
    if not url and session.article:
        url = 'https://pinel.ru/search/?' + parse.urlencode({'q': session.article})
    product_line = f'Товар: {name}'
    if url:
        product_line += f'\nСсылка: {url}'
    article = session.article or 'не удалось распознать'
    serial = session.serial_number or 'не удалось распознать'
    text = (
        f'Отлично, товар найден 👍\n{product_line}\n'
        f'Артикул: {article}\nСерийный номер: {serial}'
    )
    return text + ('\n\nТеперь пришлите фото чека 🧾' if receipt_prompt else '')


def _registration_labels_keyboard():
    return {'inline_keyboard': [[{
        'text': 'Все товары добавлены — перейти к чеку 🧾',
        'callback_data': 'registration:labels:done',
    }]]}


def _finish_registration_labels(config, callback):
    sender = callback.get('from') or {}
    session = WarrantyCustomerSession.objects.get(telegram_user_id=str(sender.get('id')))
    products = session.raw_ocr_data.get('products') or []
    if session.mode != session.Mode.REGISTRATION or session.step != session.Step.LABEL or not products:
        _answer_callback(config, callback, 'Сначала пришлите хотя бы одну этикетку.')
        return
    _answer_callback(config, callback)
    _continue_registration_label_validation(config, session)


def _continue_registration_label_validation(config, session):
    raw_data = dict(session.raw_ocr_data)
    products = list(raw_data.get('products') or [])
    for index, item in enumerate(products):
        missing = 'article' if not str(item.get('article') or '').strip() else (
            'serial_number' if not str(item.get('serial_number') or '').strip() else ''
        )
        if not missing:
            continue
        raw_data['editing_product_index'] = index
        session.raw_ocr_data = raw_data
        session.article = str(item.get('article') or '')
        session.serial_number = str(item.get('serial_number') or '')
        session.step = session.Step.ARTICLE if missing == 'article' else session.Step.SERIAL
        session.save()
        field_name = 'артикул' if missing == 'article' else 'серийный номер'
        _send(config, session, f'У товара №{index + 1} не удалось распознать {field_name}. Напишите его вручную.')
        return
    raw_data.pop('editing_product_index', None)
    session.raw_ocr_data = raw_data
    session.step = session.Step.RECEIPT
    session.save()
    _send(config, session, f'Добавлено товаров: {len(products)}. Теперь пришлите фото общего чека 🧾')


def _save_manually_entered_product_field(config, session, field, value):
    index = session.raw_ocr_data.get('editing_product_index')
    if session.mode != session.Mode.REGISTRATION or index is None:
        return False
    raw_data = dict(session.raw_ocr_data)
    products = list(raw_data.get('products') or [])
    if not 0 <= int(index) < len(products):
        return False
    products[int(index)] = {**products[int(index)], field: value[:255]}
    raw_data['products'] = products
    session.raw_ocr_data = raw_data
    session.article = str(products[int(index)].get('article') or '')
    session.serial_number = str(products[int(index)].get('serial_number') or '')
    session.save()
    _continue_registration_label_validation(config, session)
    return True


def _handle_message(config, message):
    sender = message.get('from') or {}
    session, _ = WarrantyCustomerSession.objects.get_or_create(
        telegram_user_id=str(sender.get('id')), defaults={'chat_id': str(message['chat']['id'])},
    )
    session.chat_id = str(message['chat']['id'])
    session.username = str(sender.get('username') or '')[:255]
    _remember(session, message.get('message_id'))
    session.save(update_fields=('chat_id', 'username', 'telegram_message_ids', 'updated_at'))
    text = (message.get('text') or '').strip()
    if text in ('/start', '/new'):
        _clear_active_documents(session)
        session.full_name = session.phone = session.article = session.serial_number = ''
        session.purchase_date = session.external_claim_id = None
        session.selected_registration = None
        session.raw_ocr_data = {}
        session.step = session.Step.MENU
        session.save()
        _send(config, session, f'{config.welcome_text}\n\nЧто хотите сделать?', reply_markup=_menu_keyboard())
        return
    if session.step == session.Step.LABEL and _save_photo(config, session, message, 'label'):
        if session.mode == session.Mode.REGISTRATION:
            session.save()
            _send(
                config, session,
                _label_confirmation(session, receipt_prompt=False)
                + '\n\nПришлите остальные этикетки. Когда все товары добавлены, нажмите кнопку ниже — затем пришлёте общий чек.',
                reply_markup=_registration_labels_keyboard(),
            )
        else:
            session.step = session.Step.RECEIPT
            session.save()
            _send(config, session, _label_confirmation(session))
    elif session.step == session.Step.WARRANTY_CARD and _save_photo(config, session, message, 'warranty_card'):
        _request_contacts(config, session)
    elif session.step == session.Step.RECEIPT and _save_photo(config, session, message, 'receipt'):
        if session.mode == session.Mode.CLAIM:
            session.step = session.Step.WARRANTY_CARD; session.save(); _send(config, session, 'Чек получил. Теперь пришлите фото гарантийного талона.')
        else:
            _request_contacts(config, session)
    elif session.step == session.Step.PHONE:
        value = (message.get('contact') or {}).get('phone_number') or text
        phone = _phone(value)
        if not phone: _send(config, session, 'Не удалось проверить номер. Введите его, например +79991234567.'); return
        session.phone = phone; session.step = session.Step.FULL_NAME; session.save(); _send(config, session, 'Укажите ФИО полностью.', reply_markup={'remove_keyboard': True})
    elif session.step == session.Step.FULL_NAME:
        if len(text.split()) < 2: _send(config, session, 'Пожалуйста, укажите фамилию, имя и отчество (если есть).'); return
        session.full_name = text[:255]; _show_next(config, session)
    elif session.step == session.Step.ARTICLE and text:
        if not _save_manually_entered_product_field(config, session, 'article', text):
            session.article = text[:255]; _show_next(config, session)
    elif session.step == session.Step.SERIAL and text:
        if not _save_manually_entered_product_field(config, session, 'serial_number', text):
            session.serial_number = text[:255]; _show_next(config, session)
    elif session.step == session.Step.PURCHASE_DATE:
        try: session.purchase_date = datetime.strptime(text, '%d.%m.%Y').date()
        except ValueError: _send(config, session, 'Введите дату в формате ДД.ММ.ГГГГ.'); return
        _show_next(config, session)
    else:
        _route_to_support(config, session, message)


def _create_claim(config, callback):
    sender = callback.get('from') or {}
    with transaction.atomic():
        session = WarrantyCustomerSession.objects.select_for_update().get(telegram_user_id=str(sender.get('id')))
        if session.step == session.Step.SUBMITTED:
            _answer_callback(config, callback, 'Обращение уже оформлено.')
            return
        if session.step != session.Step.READY:
            _answer_callback(config, callback, 'Сначала заполните все данные.')
            return
        if not session.external_claim_id:
            result = BitrixWarrantyClient().call('claims.create', {'fields': {
                'UF_FIO': session.full_name, 'UF_PHONE': session.phone, 'UF_TYPE': '1',
                'UF_PRODUCT_NAME': session.article, 'UF_ARTICLE': session.article,
                'UF_SERIAL_NUMBER': session.serial_number, 'UF_DATE_OF_PURCHASE': session.purchase_date.isoformat(),
                'UF_COMMENT': f'Создано клиентским Telegram-ботом. Telegram user ID: {session.telegram_user_id}',
            }})
            session.external_claim_id = int(result['id'])
            session.save(update_fields=('external_claim_id', 'updated_at'))
    claim_documents = list(_active_documents(session))
    if session.selected_registration_id:
        claim_documents.extend(filter(None, (
            session.selected_registration.label_document,
            session.selected_registration.receipt_document,
        )))
    for document in {item.pk: item for item in claim_documents}.values():
        document.file.open('rb')
        content = document.file.read()
        document.file.close()
        BitrixWarrantyClient().call('claims.files.add', {'id': session.external_claim_id, 'file': {
            'name': document.file.name.rsplit('/', 1)[-1], 'contentType': document.content_type,
            'contentBase64': base64.b64encode(content).decode(),
            'checksumSha256': __import__('hashlib').sha256(content).hexdigest(),
        }})
    blank = BitrixWarrantyClient().call('claims.blank', {'id': session.external_claim_id})
    session.step = session.Step.SUBMITTED; session.last_error = ''; session.save()
    WarrantyCustomerProfile.objects.filter(telegram_user_id=session.telegram_user_id, consent_revoked_at__isnull=True).update(
        chat_id=session.chat_id, username=session.username, full_name=session.full_name, phone=session.phone,
    )
    _answer_callback(config, callback, 'Рекламация оформлена!')
    _send(config, session, f'Рекламация №{session.external_claim_id} оформлена. Бланк: {blank["url"]}')
    document_message = _telegram(config, 'sendDocument', {'chat_id': session.chat_id, 'document': blank['url'], 'caption': f'Бланк рекламации №{session.external_claim_id}'})
    if isinstance(document_message, dict):
        _remember(session, document_message.get('message_id'))
        session.save(update_fields=('telegram_message_ids', 'updated_at'))


@csrf_exempt
def customer_bot_webhook(request):
    if request.method != 'POST': return HttpResponse(status=405)
    config = WarrantyCustomerBotSettings.get_solo()
    supplied = request.headers.get('X-Telegram-Bot-Api-Secret-Token', '')
    if not config.is_enabled or not config.webhook_secret_token or not secrets.compare_digest(supplied, config.webhook_secret_token):
        return HttpResponse(status=403)
    try:
        update = json.loads(request.body)
        update_id = int(update['update_id'])
        update_log, created = WarrantyCustomerUpdate.objects.get_or_create(update_id=update_id)
        if not created:
            return JsonResponse({'ok': True})
        if update.get('callback_query'):
            callback = update['callback_query']
            data = callback.get('data') or ''
            if data == 'flow:registration': _start_flow(config, callback, WarrantyCustomerSession.Mode.REGISTRATION)
            elif data == 'flow:claim': _start_flow(config, callback, WarrantyCustomerSession.Mode.CLAIM)
            elif data == 'consent:accept': _accept_consent(config, callback)
            elif data == 'consent:decline': _decline_consent(config, callback)
            elif data == 'registration:labels:done': _finish_registration_labels(config, callback)
            elif data.startswith('claim:product:'): _select_claim_product(config, callback, data.rsplit(':', 1)[-1])
            elif data == 'warranty:activate': _activate_registration(config, callback)
            elif data == 'warranty:create': _create_claim(config, callback)
        elif update.get('message'):
            message = update['message']
            if not _handle_support_reply(config, message):
                _handle_message(config, message)
    except (ValueError, KeyError, WarrantyCustomerSession.DoesNotExist, WarrantyCustomerProfile.DoesNotExist, WarrantyProductRegistration.DoesNotExist):
        return JsonResponse({'error': 'invalid update'}, status=400)
    except (RuntimeError, BitrixSyncError) as exc:
        if 'update_log' in locals():
            update_log.delete()
        config.webhook_last_error = str(exc)[:2000]
        config.save(update_fields=('webhook_last_error', 'updated_at'))
        return JsonResponse({'error': str(exc)}, status=503)
    return JsonResponse({'ok': True})
