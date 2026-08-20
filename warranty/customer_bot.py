import base64
import json
import re
import secrets
from datetime import datetime
from urllib import error, request

from django.core.files.base import ContentFile
from django.db import transaction
from django.http import HttpResponse, JsonResponse
from django.utils.dateparse import parse_date
from django.views.decorators.csrf import csrf_exempt

from warranty.bitrix_sync import BitrixSyncError, BitrixWarrantyClient
from warranty.models import WarrantyCustomerBotSettings, WarrantyCustomerDocument, WarrantyCustomerSession, WarrantyCustomerUpdate


def _telegram(config, method, payload):
    url = f'https://api.telegram.org/bot{config.bot_token}/{method}'
    body = json.dumps(payload, ensure_ascii=False).encode()
    try:
        with request.urlopen(request.Request(url, data=body, headers={'Content-Type': 'application/json'}), timeout=15) as response:
            data = json.loads(response.read())
    except (error.URLError, TimeoutError, ValueError) as exc:
        raise RuntimeError('Telegram временно недоступен.') from exc
    if not data.get('ok'):
        raise RuntimeError('Telegram не принял запрос.')
    return data.get('result')


def _send(config, session, text, **extra):
    result = _telegram(config, 'sendMessage', {'chat_id': session.chat_id, 'text': text, **extra})
    if isinstance(result, dict) and result.get('message_id'):
        _remember(session, result['message_id'])
        session.save(update_fields=('telegram_message_ids', 'updated_at'))
    return result


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


def _recognize(config, content, content_type, kind):
    if not config.ocr_api_key:
        return {}
    wanted = 'article и serial_number' if kind == 'label' else 'purchase_date'
    payload = {
        'model': config.ocr_model,
        'messages': [{'role': 'user', 'content': [
            {'type': 'text', 'text': f'Распознай {wanted}. Верни только JSON с ключами article, serial_number, purchase_date (YYYY-MM-DD). Не угадывай: неизвестное значение — пустая строка.'},
            {'type': 'image_url', 'image_url': {'url': f'data:{content_type};base64,{base64.b64encode(content).decode()}'}}
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
            response_data = json.loads(response.read())
        return json.loads(response_data['choices'][0]['message']['content'])
    except (error.URLError, TimeoutError, ValueError, KeyError, IndexError):
        return {}


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
        extra['reply_markup'] = {'inline_keyboard': [[{'text': 'Оформить рекламацию', 'callback_data': 'warranty:create'}]]}
    _send(config, session, text, **extra)


def _save_photo(config, session, message, kind):
    photos = message.get('photo') or []
    if not photos:
        _send(config, session, 'Пожалуйста, пришлите именно фотографию.')
        return False
    file_id = photos[-1]['file_id']
    content, content_type, extension = _download_photo(config, file_id)
    WarrantyCustomerDocument.objects.create(
        session=session, kind=kind, telegram_file_id=file_id,
        telegram_message_id=str(message['message_id']), content_type=content_type,
        file=ContentFile(content, name=f'{kind}-{message["message_id"]}.{extension}'),
    )
    recognized = _recognize(config, content, content_type, kind)
    session.raw_ocr_data = {**session.raw_ocr_data, kind: recognized}
    if kind == 'label':
        session.article = str(recognized.get('article') or '').strip()[:255]
        session.serial_number = str(recognized.get('serial_number') or '').strip()[:255]
    elif kind == 'receipt':
        session.purchase_date = parse_date(str(recognized.get('purchase_date') or ''))
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
        session.documents.all().delete()
        session.full_name = session.phone = session.article = session.serial_number = ''
        session.purchase_date = session.external_claim_id = None
        session.raw_ocr_data = {}
        session.step = session.Step.LABEL
        session.save()
        _send(config, session, config.welcome_text)
        return
    if session.step == session.Step.LABEL and _save_photo(config, session, message, 'label'):
        session.step = session.Step.WARRANTY_CARD; session.save(); _send(config, session, 'Теперь пришлите фото гарантийного талона.')
    elif session.step == session.Step.WARRANTY_CARD and _save_photo(config, session, message, 'warranty_card'):
        session.step = session.Step.RECEIPT; session.save(); _send(config, session, 'Пришлите фото чека.')
    elif session.step == session.Step.RECEIPT and _save_photo(config, session, message, 'receipt'):
        session.step = session.Step.PHONE; session.save(); _send(config, session, 'Укажите номер телефона или поделитесь контактом кнопкой ниже.', reply_markup={'keyboard': [[{'text': 'Поделиться номером', 'request_contact': True}]], 'resize_keyboard': True, 'one_time_keyboard': True})
    elif session.step == session.Step.PHONE:
        value = (message.get('contact') or {}).get('phone_number') or text
        phone = _phone(value)
        if not phone: _send(config, session, 'Не удалось проверить номер. Введите его, например +79991234567.'); return
        session.phone = phone; session.step = session.Step.FULL_NAME; session.save(); _send(config, session, 'Укажите ФИО полностью.', reply_markup={'remove_keyboard': True})
    elif session.step == session.Step.FULL_NAME:
        if len(text.split()) < 2: _send(config, session, 'Пожалуйста, укажите фамилию, имя и отчество (если есть).'); return
        session.full_name = text[:255]; _show_next(config, session)
    elif session.step == session.Step.ARTICLE and text:
        session.article = text[:255]; _show_next(config, session)
    elif session.step == session.Step.SERIAL and text:
        session.serial_number = text[:255]; _show_next(config, session)
    elif session.step == session.Step.PURCHASE_DATE:
        try: session.purchase_date = datetime.strptime(text, '%d.%m.%Y').date()
        except ValueError: _send(config, session, 'Введите дату в формате ДД.ММ.ГГГГ.'); return
        _show_next(config, session)
    else:
        _send(config, session, 'Чтобы начать новое оформление, отправьте /new.')


def _create_claim(config, callback):
    sender = callback.get('from') or {}
    with transaction.atomic():
        session = WarrantyCustomerSession.objects.select_for_update().get(telegram_user_id=str(sender.get('id')))
        if session.step == session.Step.SUBMITTED:
            _telegram(config, 'answerCallbackQuery', {'callback_query_id': callback['id'], 'text': 'Обращение уже оформлено.'})
            return
        if session.step != session.Step.READY:
            _telegram(config, 'answerCallbackQuery', {'callback_query_id': callback['id'], 'text': 'Сначала заполните все данные.'})
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
    for document in session.documents.all():
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
    _telegram(config, 'answerCallbackQuery', {'callback_query_id': callback['id'], 'text': 'Рекламация оформлена!'})
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
        if update.get('callback_query'): _create_claim(config, update['callback_query'])
        elif update.get('message'): _handle_message(config, update['message'])
    except (ValueError, KeyError, WarrantyCustomerSession.DoesNotExist):
        return JsonResponse({'error': 'invalid update'}, status=400)
    except (RuntimeError, BitrixSyncError) as exc:
        if 'update_log' in locals():
            update_log.delete()
        return JsonResponse({'error': str(exc)}, status=503)
    return JsonResponse({'ok': True})
