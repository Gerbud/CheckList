from datetime import date

import pytest

from warranty.customer_bot import _create_claim, _next_missing, _phone
from warranty.models import WarrantyCustomerBotSettings, WarrantyCustomerSession


pytestmark = pytest.mark.django_db


def test_phone_normalization():
    assert _phone('8 (999) 123-45-67') == '+79991234567'
    assert _phone('+7 999 123 45 67') == '+79991234567'
    assert _phone('123') == ''


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
