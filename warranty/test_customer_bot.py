from datetime import date

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

from warranty.customer_bot import OpenAIModelUnavailable, _accept_consent, _activate_registration, _answer_callback, _consult_about_product, _create_claim, _extract_ocr_fields, _finish_registration_labels, _handle_message, _handle_support_reply, _label_confirmation, _menu_keyboard, _next_missing, _openai_ocr, _openai_product_answer, _phone, _product_search_query, _recognize, _request_contacts, _route_to_support, _start_product_consultation, _start_support_chat
from warranty.models import WarrantyClaim, WarrantyCustomerBotSettings, WarrantyCustomerConsultationMessage, WarrantyCustomerProfile, WarrantyCustomerSession, WarrantyCustomerSupportMessage, WarrantyCustomerSupportThread, WarrantyProductRegistration


pytestmark = pytest.mark.django_db


def test_phone_normalization():
    assert _phone('8 (999) 123-45-67') == '+79991234567'
    assert _phone('+7 999 123 45 67') == '+79991234567'
    assert _phone('123') == ''


def test_main_menu_does_not_offer_support_before_consultation():
    buttons = [button for row in _menu_keyboard()['inline_keyboard'] for button in row]
    assert not any(button.get('callback_data') == 'support:start' for button in buttons)


def test_main_menu_has_greenworks_consultation_button():
    buttons = [button for row in _menu_keyboard()['inline_keyboard'] for button in row]
    assert {'text': '🌿 Подобрать товар Greenworks', 'callback_data': 'product:consultation'} in buttons


def test_product_question_is_reduced_to_catalog_keywords():
    assert _product_search_query('Какой триммер Greenworks выбрать для небольшого участка?') == 'триммер Greenworks'
    assert _product_search_query('Нужна газонокосилка 40V для 6 соток') == 'газонокосилка 40V Greenworks'
    assert _product_search_query('Как поменять шпулю?') == 'триммер Greenworks'


def test_product_consultation_uses_its_own_session_mode(monkeypatch):
    config = WarrantyCustomerBotSettings.get_solo()
    config.ocr_api_key = 'openai-key'
    sent = []
    monkeypatch.setattr('warranty.customer_bot._answer_callback', lambda *args: None)
    monkeypatch.setattr('warranty.customer_bot._send', lambda config, session, text, **kwargs: sent.append(text))
    _start_product_consultation(config, {
        'id': 'consult-callback', 'from': {'id': 710, 'username': 'buyer'},
        'message': {'chat': {'id': 711}},
    })
    session = WarrantyCustomerSession.objects.get(telegram_user_id='710')
    assert session.mode == session.Mode.CONSULTATION
    assert session.step == session.Step.CONSULTATION
    assert 'Greenworks' in sent[-1]
    assert '/start' not in sent[-1]


def test_consultation_message_is_answered_by_openai_not_support(monkeypatch):
    config = WarrantyCustomerBotSettings.get_solo()
    config.ocr_api_key = 'openai-key'
    session = WarrantyCustomerSession.objects.create(
        telegram_user_id='720', chat_id='721', mode=WarrantyCustomerSession.Mode.CONSULTATION,
        step=WarrantyCustomerSession.Step.CONSULTATION,
    )
    sent = []
    monkeypatch.setattr('warranty.customer_bot._consult_about_product', lambda config, session, text, message_id='': sent.append((text, message_id)))
    monkeypatch.setattr('warranty.customer_bot._route_to_support', lambda *args: pytest.fail('must not route to support'))
    _handle_message(config, {'message_id': 801, 'chat': {'id': 721}, 'from': {'id': 720}, 'text': 'Какой триммер выбрать?'})
    assert sent == [('Какой триммер выбрать?', 801)]


def test_ai_dialogue_is_saved_with_both_telegram_message_ids(monkeypatch):
    config = WarrantyCustomerBotSettings.get_solo()
    config.ocr_api_key = 'openai-key'
    session = WarrantyCustomerSession.objects.create(
        telegram_user_id='730', chat_id='731', mode=WarrantyCustomerSession.Mode.CONSULTATION,
        step=WarrantyCustomerSession.Step.CONSULTATION,
    )
    monkeypatch.setattr('warranty.customer_bot._pinel_product_context', lambda question: ([], 'https://pinel.ru/search/?q=test'))
    monkeypatch.setattr('warranty.customer_bot._openai_product_answer', lambda *args: 'Полезный ответ https://pinel.ru/search/?q=test')
    monkeypatch.setattr('warranty.customer_bot._send', lambda *args, **kwargs: {'message_id': 902})
    _consult_about_product(config, session, 'Вопрос клиента', 901)
    saved = WarrantyCustomerConsultationMessage.objects.get(session=session)
    assert saved.customer_message_id == '901'
    assert saved.assistant_message_id == '902'
    assert saved.question == 'Вопрос клиента'


def test_claim_status_question_reports_that_customer_has_no_claims(monkeypatch):
    config = WarrantyCustomerBotSettings.get_solo()
    config.ocr_api_key = 'openai-key'
    session = WarrantyCustomerSession.objects.create(
        telegram_user_id='735', chat_id='736', mode=WarrantyCustomerSession.Mode.CONSULTATION,
        step=WarrantyCustomerSession.Step.CONSULTATION,
    )
    sent = []
    monkeypatch.setattr('warranty.customer_bot._refresh_claim_cache', lambda: None)
    monkeypatch.setattr('warranty.customer_bot._send', lambda config, session, text, **kwargs: sent.append((text, kwargs)) or {'message_id': 905})
    monkeypatch.setattr('warranty.customer_bot._openai_product_answer', lambda *args: pytest.fail('claim status must not use OpenAI'))
    _consult_about_product(config, session, 'Что с моей рекламацией?', 904)
    assert 'не найдено' in sent[-1][0]
    assert 'reply_markup' not in sent[-1][1]
    assert WarrantyCustomerConsultationMessage.objects.filter(session=session).count() == 1


def test_claim_status_question_returns_real_status_and_support_option(monkeypatch):
    config = WarrantyCustomerBotSettings.get_solo()
    config.ocr_api_key = 'openai-key'
    session = WarrantyCustomerSession.objects.create(
        telegram_user_id='737', chat_id='738', mode=WarrantyCustomerSession.Mode.CONSULTATION,
        step=WarrantyCustomerSession.Step.CONSULTATION,
    )
    WarrantyCustomerProfile.objects.create(
        telegram_user_id='737', chat_id='738', phone='+7 (999) 123-45-67',
        consent_version='1.0', consent_text='Согласие', consent_accepted_at=timezone.now(),
    )
    WarrantyClaim.objects.create(
        external_id=1501, phone='8 999 123 45 67', product_name='Снегоуборщик Greenworks GD40STK5',
        article='2600007UG', status=WarrantyClaim.Status.IN_PROGRESS,
    )
    sent = []
    monkeypatch.setattr('warranty.customer_bot._refresh_claim_cache', lambda: None)
    monkeypatch.setattr('warranty.customer_bot._send', lambda config, session, text, **kwargs: sent.append((text, kwargs)) or {'message_id': 907})
    monkeypatch.setattr('warranty.customer_bot._openai_product_answer', lambda *args: pytest.fail('claim status must not use OpenAI'))
    _consult_about_product(config, session, 'Что с моей рекламацией по снегоуборщику?', 906)
    text, kwargs = sent[-1]
    assert '№1501' in text
    assert 'В работе' in text
    assert 'GD40STK5' in text
    assert 'consultation:support' in str(kwargs['reply_markup'])


def test_ai_dialogue_is_shared_with_support_and_message_id_is_saved(monkeypatch):
    config = WarrantyCustomerBotSettings.get_solo()
    config.support_group_id = '-100777'
    session = WarrantyCustomerSession.objects.create(telegram_user_id='740', chat_id='741')
    consultation = WarrantyCustomerConsultationMessage.objects.create(
        session=session, question='Как заменить шпулю?', answer='Безопасная инструкция',
        customer_message_id='910', assistant_message_id='911',
    )
    calls = []

    def telegram(config, method, payload):
        calls.append((method, payload))
        if method == 'createForumTopic': return {'message_thread_id': 550}
        return {'message_id': 920 + len(calls)}

    monkeypatch.setattr('warranty.customer_bot._telegram', telegram)
    monkeypatch.setattr('warranty.customer_bot._send', lambda *args, **kwargs: {})
    _start_support_chat(config, {
        'id': 'support-after-ai', 'from': {'id': 740}, 'message': {'chat': {'id': 741}},
    })
    consultation.refresh_from_db()
    thread = WarrantyCustomerSupportThread.objects.get(telegram_user_id='740')
    assert consultation.support_message_id
    assert consultation.shared_with_support_at is not None
    assert consultation.support_message_id in thread.telegram_message_ids
    assert any('Как заменить шпулю?' in call[1].get('text', '') for call in calls if call[0] == 'sendMessage')


def test_product_answer_removes_external_links_and_adds_pinel_source(monkeypatch):
    config = WarrantyCustomerBotSettings.get_solo()
    config.ocr_api_key = 'openai-key'
    response = {'choices': [{'message': {'content': 'Подойдёт эта модель. https://competitor.example/item'}}]}

    class FakeResponse:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self): return __import__('json').dumps(response).encode()

    monkeypatch.setattr('warranty.customer_bot.request.urlopen', lambda *args, **kwargs: FakeResponse())
    answer = _openai_product_answer(
        config, 'Нужен триммер', [], 'https://pinel.ru/search/?q=trimmer',
    )
    assert 'competitor.example' not in answer
    assert 'https://pinel.ru/search/?q=trimmer' in answer


def test_support_button_invites_customer_to_write_here(monkeypatch):
    config = WarrantyCustomerBotSettings.get_solo()
    sent = []
    monkeypatch.setattr('warranty.customer_bot._telegram', lambda *args, **kwargs: {})
    monkeypatch.setattr('warranty.customer_bot._send', lambda config, session, text, **kwargs: sent.append(text))
    _start_support_chat(config, {
        'id': 'support-callback', 'from': {'id': 700, 'username': 'buyer'},
        'message': {'chat': {'id': 701}},
    })
    session = WarrantyCustomerSession.objects.get(telegram_user_id='700')
    assert session.step == session.Step.MENU
    assert 'прямо в этот чат' in sent[-1]


def test_support_group_id_accepts_number_without_bot_api_prefix():
    config = WarrantyCustomerBotSettings(support_group_id='4462669970')
    assert config.support_api_chat_id == '-1004462669970'
    config.support_group_id = '-1004462669970'
    assert config.support_api_chat_id == '-1004462669970'


def test_expired_callback_answer_does_not_break_customer_flow(monkeypatch):
    config = WarrantyCustomerBotSettings.get_solo()
    monkeypatch.setattr(
        'warranty.customer_bot._telegram',
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError('query is too old')),
    )
    assert _answer_callback(config, {'id': 'expired-callback'}, 'Спасибо!') is None


def test_label_confirmation_contains_product_link_serial_and_receipt_request():
    session = WarrantyCustomerSession(
        article='G40LT30', serial_number='SN-123',
        raw_ocr_data={'label': {'product': {
            'name': 'Триммер Greenworks',
            'url': 'https://pinel.ru/catalog/sku/111/',
        }}},
    )
    text = _label_confirmation(session)
    assert 'Триммер Greenworks' in text
    assert '<a href="https://pinel.ru/catalog/sku/111/">Триммер Greenworks</a>' in text
    assert 'Ссылка:' not in text
    assert 'SN-123' in text
    assert 'фото чека' in text


def test_label_confirmation_uses_catalog_search_when_exact_card_is_missing():
    session = WarrantyCustomerSession(
        article='GW 123', serial_number='SN-456', raw_ocr_data={'label': {}},
    )
    assert 'https://pinel.ru/search/?q=GW+123' in _label_confirmation(session)


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


def test_unavailable_openai_model_switches_to_cheapest(monkeypatch):
    config = WarrantyCustomerBotSettings.get_solo()
    config.ocr_api_key = 'openai-key'
    config.ocr_model = 'gpt-4o'
    config.save()
    calls = []

    def recognize(config, content, content_type, kind, model):
        calls.append(model)
        if model == 'gpt-4o':
            raise OpenAIModelUnavailable('disabled')
        return {'article': 'A-1', 'serial_number': 'SN-1', 'model': model}

    monkeypatch.setattr('warranty.customer_bot._openai_ocr_with_model', recognize)
    result = _openai_ocr(config, b'image', 'image/jpeg', 'label')
    config.refresh_from_db()
    assert calls == ['gpt-4o', 'gpt-4.1-nano']
    assert result['fallback_from'] == 'gpt-4o'
    assert config.ocr_model == 'gpt-4.1-nano'


def test_customer_bot_admin_has_webhook_buttons(client):
    admin = get_user_model().objects.create_superuser('customer-bot-admin', 'admin@example.com', 'secret')
    config = WarrantyCustomerBotSettings.get_solo()
    client.force_login(admin)
    response = client.get(reverse('admin:warranty_warrantycustomerbotsettings_change', args=[config.pk]))
    assert response.status_code == 200
    assert 'Создать webhook' in response.content.decode()
    assert 'Проверить webhook' in response.content.decode()
    assert '<select name="ocr_model"' in response.content.decode()
    assert 'name="consent_text_template"' in response.content.decode()


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
                'consent_text_template': config.consent_text_template,
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
    assert session.step == session.Step.PHONE


def test_consent_is_requested_immediately_before_phone(monkeypatch):
    config = WarrantyCustomerBotSettings.get_solo()
    session = WarrantyCustomerSession.objects.create(
        telegram_user_id='505', chat_id='605', mode=WarrantyCustomerSession.Mode.REGISTRATION,
        step=WarrantyCustomerSession.Step.RECEIPT,
    )
    sent = []
    monkeypatch.setattr(
        'warranty.customer_bot._send',
        lambda config, session, text, **kwargs: sent.append((text, kwargs)),
    )
    _request_contacts(config, session)
    session.refresh_from_db()
    assert session.step == session.Step.CONSENT
    assert 'Согласен ✅' in str(sent[-1][1]['reply_markup'])
    assert 'Не согласен' not in str(sent[-1][1]['reply_markup'])
    assert 'не согласен' in sent[-1][0]


def test_customer_can_decline_consent_with_text(monkeypatch):
    config = WarrantyCustomerBotSettings.get_solo()
    session = WarrantyCustomerSession.objects.create(
        telegram_user_id='506', chat_id='606', mode=WarrantyCustomerSession.Mode.REGISTRATION,
        step=WarrantyCustomerSession.Step.CONSENT,
    )
    sent = []
    monkeypatch.setattr('warranty.customer_bot._send', lambda config, session, text, **kwargs: sent.append((text, kwargs)))

    _handle_message(config, {
        'message_id': 706, 'from': {'id': 506}, 'chat': {'id': 606}, 'text': 'Не согласен',
    })

    session.refresh_from_db()
    assert session.step == session.Step.MENU
    assert 'данные не сохраняем' in sent[-1][0]


def test_other_consent_text_does_not_count_as_decline(monkeypatch):
    config = WarrantyCustomerBotSettings.get_solo()
    session = WarrantyCustomerSession.objects.create(
        telegram_user_id='507', chat_id='607', mode=WarrantyCustomerSession.Mode.REGISTRATION,
        step=WarrantyCustomerSession.Step.CONSENT,
    )
    sent = []
    monkeypatch.setattr('warranty.customer_bot._send', lambda config, session, text, **kwargs: sent.append((text, kwargs)))

    _handle_message(config, {
        'message_id': 707, 'from': {'id': 507}, 'chat': {'id': 607}, 'text': 'У меня вопрос',
    })

    session.refresh_from_db()
    assert session.step == session.Step.CONSENT
    assert 'нажмите «Согласен' in sent[-1][0]


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


def test_multiple_products_are_registered_with_one_purchase(monkeypatch):
    config = WarrantyCustomerBotSettings.get_solo()
    profile = WarrantyCustomerProfile.objects.create(
        telegram_user_id='503', chat_id='603', consent_version=config.consent_version,
        consent_text='Согласие', consent_accepted_at=timezone.now(),
    )
    session = WarrantyCustomerSession.objects.create(
        telegram_user_id='503', chat_id='603', full_name='Иван Иванов', phone='+79991234567',
        article='A-2', serial_number='SN-2', purchase_date=date(2026, 8, 20),
        mode=WarrantyCustomerSession.Mode.REGISTRATION, step=WarrantyCustomerSession.Step.READY,
        raw_ocr_data={'products': [
            {'article': 'A-1', 'serial_number': 'SN-1'},
            {'article': 'A-2', 'serial_number': 'SN-2'},
        ]},
    )
    monkeypatch.setattr('warranty.customer_bot._telegram', lambda *args, **kwargs: {})
    monkeypatch.setattr('warranty.customer_bot._send', lambda *args, **kwargs: {})
    _activate_registration(config, {'id': 'cb-register-many', 'from': {'id': 503}})
    assert set(WarrantyProductRegistration.objects.filter(profile=profile).values_list('serial_number', flat=True)) == {'SN-1', 'SN-2'}
    session.refresh_from_db()
    assert session.step == session.Step.SUBMITTED


def test_done_collecting_labels_moves_registration_to_receipt(monkeypatch):
    config = WarrantyCustomerBotSettings.get_solo()
    session = WarrantyCustomerSession.objects.create(
        telegram_user_id='504', chat_id='604', mode=WarrantyCustomerSession.Mode.REGISTRATION,
        step=WarrantyCustomerSession.Step.LABEL,
        raw_ocr_data={'products': [{'article': 'A-1', 'serial_number': 'SN-1'}]},
    )
    sent = []
    monkeypatch.setattr('warranty.customer_bot._telegram', lambda *args, **kwargs: {})
    monkeypatch.setattr('warranty.customer_bot._send', lambda config, session, text, **kwargs: sent.append(text))
    _finish_registration_labels(config, {'id': 'cb-labels-done', 'from': {'id': 504}})
    session.refresh_from_db()
    assert session.step == session.Step.RECEIPT
    assert 'Добавлено товаров: 1' in sent[-1]


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
