from datetime import date

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from warranty.customer_bot import _create_claim, _extract_ocr_fields, _next_missing, _phone, _recognize
from warranty.models import WarrantyCustomerBotSettings, WarrantyCustomerSession


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
            '_register_webhook': 'Создать webhook',
        },
    )
    assert response.status_code == 302
    config.refresh_from_db()
    assert not config.webhook_secret_token.startswith('http')
    assert calls[0][0] == 'setWebhook'
    assert calls[0][1]['secret_token'] == config.webhook_secret_token


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
