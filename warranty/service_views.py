import base64
import hashlib
import uuid

from django.core.files.base import ContentFile
from django.db import transaction
from django.shortcuts import render
from django.utils import timezone

from warranty.bitrix_sync import BitrixSyncError, BitrixWarrantyClient
from warranty.customer_bot import _consent_text
from warranty.models import WarrantyCustomerBotSettings, WarrantyCustomerDocument, WarrantyCustomerProfile, WarrantyCustomerSession, WarrantyProductRegistration
from warranty.service_forms import WarrantyServiceForm


def _browser_identity(request):
    identity = request.session.get('warranty_service_identity')
    if not identity:
        identity = f'web:{uuid.uuid4()}'
        request.session['warranty_service_identity'] = identity
    return identity


def _save_document(session, kind, upload):
    content = upload.read()
    return WarrantyCustomerDocument.objects.create(
        session=session,
        kind=kind,
        telegram_file_id='',
        telegram_message_id='',
        content_type=upload.content_type or 'application/octet-stream',
        file=ContentFile(content, name=upload.name),
    )


def _send_document(client, claim_id, document):
    document.file.open('rb')
    try:
        content = document.file.read()
    finally:
        document.file.close()
    client.call('claims.files.add', {'id': claim_id, 'file': {
        'name': document.file.name.rsplit('/', 1)[-1],
        'contentType': document.content_type,
        'contentBase64': base64.b64encode(content).decode(),
        'checksumSha256': hashlib.sha256(content).hexdigest(),
    }})


def _persist_submission(request, form):
    config = WarrantyCustomerBotSettings.get_solo()
    data = form.cleaned_data
    identity = _browser_identity(request)
    session = WarrantyCustomerSession.objects.create(
        telegram_user_id=identity,
        chat_id='',
        mode=data['flow'],
        step=WarrantyCustomerSession.Step.READY,
        full_name=data['full_name'],
        phone=data['phone'],
        article=data['article'].strip(),
        serial_number=data['serial_number'].strip(),
        purchase_date=data['purchase_date'],
    )
    documents = [
        _save_document(session, WarrantyCustomerDocument.Kind.LABEL, data['label_photo']),
        _save_document(session, WarrantyCustomerDocument.Kind.RECEIPT, data['receipt_photo']),
    ]
    if data.get('warranty_card_photo'):
        documents.append(_save_document(session, WarrantyCustomerDocument.Kind.WARRANTY_CARD, data['warranty_card_photo']))
    consent_text = _consent_text(config).replace('бот', 'сервис')
    profile, _ = WarrantyCustomerProfile.objects.update_or_create(
        telegram_user_id=identity,
        defaults={
            'chat_id': '', 'username': '', 'full_name': data['full_name'], 'phone': data['phone'],
            'consent_version': config.consent_version, 'consent_text': consent_text,
            'consent_message_id': '', 'consent_accepted_at': timezone.now(), 'consent_revoked_at': None,
        },
    )
    if data['flow'] == WarrantyServiceForm.FLOW_REGISTRATION:
        registration, created = WarrantyProductRegistration.objects.get_or_create(
            profile=profile,
            serial_number=session.serial_number,
            defaults={
                'article': session.article,
                'purchase_date': session.purchase_date,
                'label_document': documents[0],
                'receipt_document': documents[1],
                'raw_ocr_data': {'source': 'web'},
            },
        )
        session.step = session.Step.SUBMITTED
        session.save(update_fields=('step', 'updated_at'))
        return {'kind': 'registration', 'number': registration.pk, 'created': created}

    client = BitrixWarrantyClient()
    result = client.call('claims.create', {'fields': {
        'UF_FIO': session.full_name,
        'UF_PHONE': session.phone,
        'UF_TYPE': '1',
        'UF_PRODUCT_NAME': session.article,
        'UF_ARTICLE': session.article,
        'UF_SERIAL_NUMBER': session.serial_number,
        'UF_DATE_OF_PURCHASE': session.purchase_date.isoformat(),
        'UF_DEFECT': data['defect'].strip(),
        'UF_COMMENT': 'Создано клиентом на service.pinel.ru',
    }})
    session.external_claim_id = int(result['id'])
    session.save(update_fields=('external_claim_id', 'updated_at'))
    for document in documents:
        _send_document(client, session.external_claim_id, document)
    blank = client.call('claims.blank', {'id': session.external_claim_id})
    session.step = session.Step.SUBMITTED
    session.save(update_fields=('step', 'updated_at'))
    return {'kind': 'claim', 'number': session.external_claim_id, 'blank_url': blank.get('url', '')}


def service_home(request):
    initial_flow = request.GET.get('flow')
    if initial_flow not in dict(WarrantyServiceForm.FLOW_CHOICES):
        initial_flow = WarrantyServiceForm.FLOW_REGISTRATION
    form = WarrantyServiceForm(request.POST or None, request.FILES or None, initial={'flow': initial_flow})
    result = None
    service_error = ''
    if request.method == 'POST' and form.is_valid():
        try:
            with transaction.atomic():
                result = _persist_submission(request, form)
            request.session.pop('warranty_service_identity', None)
        except BitrixSyncError:
            service_error = 'Сервис временно не смог передать обращение. Проверьте поля и попробуйте ещё раз через несколько минут.'
    config = WarrantyCustomerBotSettings.get_solo()
    return render(request, 'warranty/service.html', {
        'form': form,
        'result': result,
        'service_error': service_error,
        'privacy_policy_url': config.privacy_policy_url,
        'operator': config.personal_data_operator,
    })
