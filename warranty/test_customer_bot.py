from datetime import date

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

from warranty.customer_bot import _accept_consent, _activate_registration, _create_claim, _extract_ocr_fields, _handle_support_reply, _next_missing, _phone, _recognize, _route_to_support
from warranty.models import WarrantyCustomerBotSettings, WarrantyCustomerProfile, WarrantyCustomerSession, WarrantyCustomerSupportMessage, WarrantyCustomerSupportThread, WarrantyProductRegistration


pytestmark = pytest.mark.django_db


def test_phone_normalization():
    assert _phone('8 (999) 123-45-67') == '+79991234567'
    assert _phone('+7 999 123 45 67') == '+79991234567'
    assert _phone('123') == ''


def test_ocr_text_fields_are_extracted():
    label = _extract_ocr_fields('Артикул: GD40LM46SP Серийный номер: GW-2026-9911', 'label')
    receipt = _extract_ocr_fields('КАССОВЫЙ ЧЕК 20.08.2026 14:31', 'receipt')
    assert label['article'] == 'GD40LM46SP'
    assert label['serial_number'] == 'GW-2026-9911'
    assert receipt['purchase_date'] == '2026-08-20'


def test_tesseract_is_used_when_free_ocr_fails(monkeypatch):
    config = WarrantyCustomerBotSettings.get_solo()
    config.ocr_space_api_key = 'free-key'
    monkeypatch.setattr('warranty.customer_bot._prepare_ocr_image', lambda content: (content, 'image/jpeg'))
    monkeypatch.setattr('warranty.customer_bot._ocr_space', lambda *args: (_ for _ in ()).throw(RuntimeError('down')))
    monkeypatch.setattr('warranty.customer_bot._tesseract', lambda *args: 'Артикул A-77 Серийный номер SN-991')
    assert _recognize(config, b'image', 'image/jpeg', 'label')['serial_number'] == 'SN-991'


def test_openai_has_priority_over_free_ocr(monkeypatch):
    config = WarrantyCustomerBotSettings.get_solo()
    config.ocr_api_key = 'openai-key'
    monkeypatch.setattr('warranty.customer_bot._prepare_ocr_image', lambda content: (content, 'image/jpeg'))
    monkeypatch.setattr('warranty.customer_bot._openai_ocr', lambda *args: {'article': 'AI-1', 'serial_number': 'AI-SN'})
    monkeypatch.setattr('warranty.customer_bot._ocr_space', lambda *args: pytest.fail('OCR.space should not be called'))
    result = _recognize(config, b'image', 'image/jpeg', 'label')
    assert result['article'] == 'AI-1'
    assert result['provider'] == 'openai'


def test_customer_bot_admin_has_webhook_buttons(client):
    admin = get_user_model().objects.create_superuser('customer-bot-admin', 'admin@example.com', 'secret')
    config = WarrantyCustomerBotSettings.get_solo()
    client.force_login(admin)
    response = client.get(reverse('admin:warranty_warrantycustomerbotsettings_change', args=[config.pk]))
    assert response.status_code == 200
    assert 'Создать webhook' in response.content.decode()
    assert 'Проверить webhook' in response.content.decode()


def test_register_webhook_replaces_legacy_secret(client, monkeypatch):
    admin = get_user_model().objects.create_superuser('webhook-admin', 'webhook@example.com', 'secret')
    config = WarrantyCustomerBotSettings.get_solo()
    config.bot_token = 'new-token'
    config.webhook_secret_token = 'https://legacy.example/webhook/'
    config.save()
    calls = []
    monkeypatch.setattr('warranty.customer_bot._telegram', lambda obj, method, payload: calls.append((method, payload)) or True)
    client.force_login(admin)
    response = client.post(
        reverse('admin:warranty_warrantycustomerbotsettings_change', args=[config.pk]),
        {
            'bot_token': 'new-token', 'is_enabled': 'on', 'ocr_api_key': '',
            'ocr_model': 'gpt-4.1-mini', 'ocr_space_api_key': '',
            'tesseract_command': 'tesseract', 'welcome_text': 'Здравствуйте!',
            'personal_data_operator': 'ИП Тест', 'personal_data_operator_address': 'Москва',
            'privacy_policy_url': 'https://example.com/privacy/',
            'consent_withdrawal_contact': 'privacy@example.com', 'consent_version': '1.0',
            '_register_webhook': 'Создать webhook',
        },
    )
    assert response.status_code == 302
    config.refresh_from_db()
    assert not config.webhook_secret_token.startswith('http')
    assert calls[0][0] == 'setWebhook'
    assert calls[0][1]['secret_token'] == config.webhook_secret_token


def test_consent_is_saved_with_version_text_and_telegram_evidence(monkeypatch):
    config = WarrantyCustomerBotSettings.get_solo()
    session = WarrantyCustomerSession.objects.create(
        telegram_user_id='501', chat_id='601', username='buyer',
        mode=WarrantyCustomerSession.Mode.REGISTRATION,
        step=WarrantyCustomerSession.Step.CONSENT,
    )
    monkeypatch.setattr('warranty.customer_bot._telegram', lambda *args, **kwargs: {})
    monkeypatch.setattr('warranty.customer_bot._send', lambda *args, **kwargs: {})
    _accept_consent(config, {'id': 'cb-consent', 'from': {'id': 501}, 'message': {'message_id': 701}})
    profile = WarrantyCustomerProfile.objects.get(telegram_user_id='501')
    session.refresh_from_db()
    assert profile.consent_version == config.consent_version
    assert profile.consent_message_id == '701'
    assert config.personal_data_operator in profile.consent_text
    assert profile.consent_accepted_at is not None
    assert session.step == session.Step.LABEL


def test_purchase_registration_is_idempotent(monkeypatch):
    config = WarrantyCustomerBotSettings.get_solo()
    profile = WarrantyCustomerProfile.objects.create(
        telegram_user_id='502', chat_id='602', consent_version='1.0',
        consent_text='Согласие', consent_accepted_at=timezone.now(),
    )
    session = WarrantyCustomerSession.objects.create(
        telegram_user_id='502', chat_id='602', full_name='Иван Иванов', phone='+79991234567',
        article='A-500', serial_number='SN-500', purchase_date=date(2026, 8, 20),
        mode=WarrantyCustomerSession.Mode.REGISTRATION, step=WarrantyCustomerSession.Step.READY,
    )
    monkeypatch.setattr('warranty.customer_bot._telegram', lambda *args, **kwargs: {})
    monkeypatch.setattr('warranty.customer_bot._send', lambda *args, **kwargs: {})
    callback = {'id': 'cb-register', 'from': {'id': 502}}
    _activate_registration(config, callback)
    session.step = session.Step.READY
    session.save(update_fields=('step',))
    _activate_registration(config, callback)
    profile.refresh_from_db()
    assert WarrantyProductRegistration.objects.filter(profile=profile).count() == 1
    assert profile.phone == '+79991234567'


def test_arbitrary_messages_use_one_forum_topic_per_customer(monkeypatch):
    config = WarrantyCustomerBotSettings.get_solo()
    config.support_group_id = '-100777'
    session = WarrantyCustomerSession.objects.create(telegram_user_id='900', chat_id='901', username='buyer')
    calls = []

    def telegram(config, method, payload):
        calls.append((method, payload))
        if method == 'createForumTopic': return {'message_thread_id': 500}
        return {'message_id': 1000 + len(calls)}

    monkeypatch.setattr('warranty.customer_bot._telegram', telegram)
    monkeypatch.setattr('warranty.customer_bot._send', lambda *args, **kwargs: {})
    _route_to_support(config, session, {'message_id': 10, 'text': 'Нужна помощь'})
    _route_to_support(config, session, {'message_id': 11, 'text': 'Есть вопрос'})
    thread = WarrantyCustomerSupportThread.objects.get(telegram_user_id='900')
    assert thread.message_thread_id == '500'
    assert WarrantyCustomerSupportMessage.objects.filter(thread=thread, direction='customer').count() == 2
    assert [method for method, payload in calls].count('createForumTopic') == 1
    assert [method for method, payload in calls].count('copyMessage') == 2


def test_support_topic_reply_is_copied_to_customer_once(monkeypatch):
    config = WarrantyCustomerBotSettings.get_solo()
    config.support_group_id = '-100777'
    thread = WarrantyCustomerSupportThread.objects.create(
        telegram_user_id='910', customer_chat_id='911', support_chat_id='-100777', message_thread_id='510',
    )
    calls = []
    monkeypatch.setattr('warranty.customer_bot._telegram', lambda config, method, payload: calls.append((method, payload)) or {'message_id': 701})
    message = {'message_id': 601, 'message_thread_id': 510, 'chat': {'id': -100777}, 'from': {'id': 42}, 'text': 'Ответ'}
    assert _handle_support_reply(config, message) is True
    assert _handle_support_reply(config, message) is True
    assert WarrantyCustomerSupportMessage.objects.filter(thread=thread, direction='support').count() == 1
    assert len(calls) == 1
    assert calls[0][1]['chat_id'] == '911'


def test_missing_recognized_fields_are_requested_in_order():
    session = WarrantyCustomerSession.objects.create(telegram_user_id='1', chat_id='1', full_name='Иван Иванов', phone='+79991234567')
    assert 'артикул' in _next_missing(session).lower()
    assert session.step == session.Step.ARTICLE
    session.article = 'A-100'
    assert 'серийный' in _next_missing(session).lower()
    session.serial_number = 'SN-1'
    assert 'дату покупки' in _next_missing(session).lower()
    session.purchase_date = date(2026, 8, 20)
    assert 'Проверьте данные' in _next_missing(session)
    assert session.step == session.Step.READY


def test_create_claim_is_resumable_and_does_not_create_twice(monkeypatch):
    config = WarrantyCustomerBotSettings.get_solo()
    session = WarrantyCustomerSession.objects.create(
        telegram_user_id='77', chat_id='88', full_name='Иван Иванов', phone='+79991234567',
        article='A-100', serial_number='SN-1', purchase_date=date(2026, 8, 20),
        step=WarrantyCustomerSession.Step.READY,
    )
    calls = []

    def fake_call(self, action, payload=None):
        calls.append(action)
        if action == 'claims.create': return {'id': 501}
        if action == 'claims.blank': return {'url': 'https://pinel.ru/upload/blank.pdf'}
        return {}

    monkeypatch.setattr('warranty.customer_bot.BitrixWarrantyClient.call', fake_call)
    monkeypatch.setattr('warranty.customer_bot._telegram', lambda *args, **kwargs: {})
    monkeypatch.setattr('warranty.customer_bot._send', lambda *args, **kwargs: {})
    callback = {'id': 'cb-1', 'from': {'id': 77}}
    _create_claim(config, callback)
    _create_claim(config, callback)
    session.refresh_from_db()
    assert session.external_claim_id == 501
    assert session.step == session.Step.SUBMITTED
    assert calls.count('claims.create') == 1
    assert calls.count('claims.blank') == 1
