import json
from io import StringIO
from types import SimpleNamespace

import pytest
from django.contrib.admin.sites import AdminSite
from django.contrib.auth.models import User
from django.core.management import call_command
from django.urls import reverse

from checklists.models import EmployeeProfile
from warranty.forms import WarrantyClaimUpdateForm
from warranty.admin import WarrantyTelegramSettingsAdmin
from warranty.models import WarrantyBitrixOutbox, WarrantyClaim, WarrantyHistoryEvent, WarrantyTelegramMessage, WarrantyTelegramSettings, WarrantyTelegramStatusButton, WarrantyTelegramThread
from warranty.bitrix_sync import import_claim_rows
from warranty.services import update_claim
from warranty.telegram import _claim_message, _status_keyboard, record_warranty_update, update_claim_topic_message
import warranty.telegram as warranty_telegram


@pytest.mark.django_db
def test_bitrix_settings_is_system_admin_only_and_hides_secret(client, settings):
    settings.BITRIX_WARRANTY_SYNC_URL = 'https://pinel.example/warranty-sync/'
    settings.BITRIX_WARRANTY_SYNC_SECRET = 'must-never-be-rendered'
    ordinary = User.objects.create_user('ordinary-bitrix')
    client.force_login(ordinary)
    assert client.get(reverse('warranty:bitrix_settings')).status_code == 403

    admin = User.objects.create_user('bitrix-settings-admin')
    EmployeeProfile.objects.create(user=admin, role=EmployeeProfile.Role.SYSTEM_ADMIN, is_active=True)
    client.force_login(admin)
    response = client.get(reverse('warranty:bitrix_settings'))

    assert response.status_code == 200
    content = response.content.decode()
    assert 'https://pinel.example/warranty-sync/' in content
    assert 'HMAC-секрет' in content
    assert settings.BITRIX_WARRANTY_SYNC_SECRET not in content


@pytest.mark.django_db
def test_bitrix_settings_health_check(client, monkeypatch):
    admin = User.objects.create_user('bitrix-health-admin')
    EmployeeProfile.objects.create(user=admin, role=EmployeeProfile.Role.SYSTEM_ADMIN, is_active=True)
    client.force_login(admin)

    monkeypatch.setattr('warranty.views.BitrixWarrantyClient.call', lambda self, action: {'version': '1.0.0'})
    response = client.post(reverse('warranty:bitrix_settings'), {'action': 'check'}, follow=True)

    assert response.status_code == 200
    assert 'Bitrix отвечает. Версия модуля: 1.0.0.' in response.content.decode()


def test_warranty_telegram_settings_admin_only_shows_peer_id():
    model_admin = WarrantyTelegramSettingsAdmin(WarrantyTelegramSettings, AdminSite())

    assert model_admin.get_fields(None) == (
        'peer_id', 'use_forum_topics', 'is_enabled',
    )


@pytest.mark.django_db
def test_warranty_list_is_system_admin_only(client):
    ordinary = User.objects.create_user('ordinary', password='secret')
    client.force_login(ordinary)
    assert client.get(reverse('warranty:claim_list')).status_code == 403

    admin = User.objects.create_user('warranty-admin', password='secret')
    EmployeeProfile.objects.create(user=admin, role=EmployeeProfile.Role.SYSTEM_ADMIN, is_active=True)
    client.force_login(admin)
    assert client.get(reverse('warranty:claim_list')).status_code == 200

    WarrantyClaim.objects.create(external_id=999, assigned_to=None)
    response = client.get(reverse('warranty:claim_list'))
    assert response.status_code == 200
    assert '—' in response.content.decode()


@pytest.mark.django_db
def test_bitrix_import_is_idempotent(tmp_path):
    payload = {'claims': [{
        'ID': 125, 'UF_STATUS': '1', 'UF_TYPE': '1', 'UF_FIO': 'Иван Иванов',
        'UF_PHONE': '+79990000000', 'UF_PRODUCT_NAME': 'Газонокосилка',
        'UF_CREATE_DATE': '2026-08-20T10:00:00+03:00',
        'HISTORY': [{'ID': 7, 'UF_CHANGES': 'Статус изменён', 'UF_DATE': '2026-08-20T11:00:00+03:00'}],
        'FILES': [{'ID': 9, 'ORIGINAL_NAME': 'photo.jpg', 'SRC': '/upload/photo.jpg'}],
    }]}
    export_file = tmp_path / 'warranty.json'
    export_file.write_text(json.dumps(payload), encoding='utf-8')
    for _ in range(2):
        call_command('import_bitrix_warranty', str(export_file), stdout=StringIO())
    claim = WarrantyClaim.objects.get(external_id=125)
    assert claim.customer_name == 'Иван Иванов'
    assert claim.history.count() == 1
    assert claim.attachments.count() == 1


@pytest.mark.django_db
def test_close_and_reopen_preserve_telegram_thread():
    actor = User.objects.create_user('admin')
    claim = WarrantyClaim.objects.create(external_id=1, status=WarrantyClaim.Status.NEW)
    form = WarrantyClaimUpdateForm({'status': WarrantyClaim.Status.CLOSED, 'priority': WarrantyClaim.Priority.NORMAL, 'comment': ''}, instance=claim)
    assert form.is_valid(), form.errors
    update_claim(claim=claim, form=form, actor=actor)
    thread = WarrantyTelegramThread.objects.get(claim=claim)
    assert thread.state == WarrantyTelegramThread.State.ARCHIVED

    claim.refresh_from_db()
    form = WarrantyClaimUpdateForm({'status': WarrantyClaim.Status.IN_PROGRESS, 'priority': WarrantyClaim.Priority.NORMAL, 'comment': ''}, instance=claim)
    assert form.is_valid(), form.errors
    update_claim(claim=claim, form=form, actor=actor)
    thread.refresh_from_db()
    assert thread.state == WarrantyTelegramThread.State.RESTORE_PENDING
    assert WarrantyHistoryEvent.objects.filter(claim=claim).count() == 2


@pytest.mark.django_db
def test_warranty_peer_id_is_normalized_for_bot_api():
    settings = WarrantyTelegramSettings.get_solo()
    settings.peer_id = '3894555747'
    settings.save()
    assert settings.chat_id == '-1003894555747'


@pytest.mark.django_db
def test_site_claim_update_is_queued_for_bitrix():
    actor = User.objects.create_user('admin-sync')
    claim = WarrantyClaim.objects.create(
        source='bitrix', external_id=77, source_status='1', status=WarrantyClaim.Status.NEW,
    )
    form = WarrantyClaimUpdateForm({
        'status': WarrantyClaim.Status.IN_PROGRESS,
        'priority': WarrantyClaim.Priority.NORMAL,
        'comment': 'Принято сервисным центром',
    }, instance=claim)
    assert form.is_valid(), form.errors

    update_claim(claim=claim, form=form, actor=actor)

    queued = WarrantyBitrixOutbox.objects.get(claim=claim)
    assert queued.payload == {'UF_STATUS': '4', 'UF_COMMENT': 'Принято сервисным центром'}
    assert queued.status == WarrantyBitrixOutbox.Status.PENDING


@pytest.mark.django_db
def test_telegram_claim_message_has_clickable_product_and_phone(settings):
    settings.WARRANTY_PRODUCT_URL_TEMPLATE = 'https://shop.example/catalog/sku/{product_id}/'
    settings.WARRANTY_CLAIM_URL_TEMPLATE = 'https://pinel.example/claims/?search_str={claim_id}'
    claim = WarrantyClaim.objects.create(
        external_id=81,
        status=WarrantyClaim.Status.DIAGNOSTICS,
        product_name='Дрель & шуруповёрт',
        external_product_id='2178',
        customer_name='Иван <Иванов>',
        phone='+7 (999) 123-45-67',
        defect='Не включается',
        purchased_from_us=True,
        product_remains_with_customer=True,
    )

    message = _claim_message(claim)

    assert '<a href="https://shop.example/catalog/sku/2178/">Дрель &amp; шуруповёрт</a>' in message
    assert 'Открыть товар' not in message
    assert '<a href="https://pinel.example/claims/?search_str=81">Открыть обращение на сайте</a>' in message
    assert '<a href="tel:+79991234567">+7 (999) 123-45-67</a>' in message
    assert 'Иван &lt;Иванов&gt;' in message
    assert '🏷 <b>Статус:</b> Диагностика #статус_диагностика' in message
    assert '🛠 <b>Тип ремонта:</b> По гарантии' in message
    assert '<b>Куплено у нас:</b> Да' in message
    assert '<b>Товар находится:</b> у клиента' in message
    assert message.endswith('🔗 <a href="https://pinel.example/claims/?search_str=81">Открыть обращение на сайте</a>')


@pytest.mark.django_db
def test_telegram_claim_message_shows_our_location():
    claim = WarrantyClaim.objects.create(
        external_id=90,
        purchased_from_us=False,
        product_remains_with_customer=False,
    )

    message = _claim_message(claim)

    assert '<b>Куплено у нас:</b> Нет' in message
    assert '<b>Товар находится:</b> у нас' in message


@pytest.mark.django_db
def test_telegram_claim_message_shows_non_warranty_repair():
    claim = WarrantyClaim.objects.create(
        external_id=91,
        warranty_type=WarrantyClaim.WarrantyType.NON_WARRANTY,
    )

    assert '<b>Тип ремонта:</b> Не по гарантии' in _claim_message(claim)


@pytest.mark.django_db
def test_existing_topic_update_edits_only_recorded_intro(monkeypatch):
    claim = WarrantyClaim.objects.create(
        external_id=92,
        warranty_type=WarrantyClaim.WarrantyType.NON_WARRANTY,
    )
    thread = WarrantyTelegramThread.objects.create(
        claim=claim, chat_id='-100123', topic_id='500',
        state=WarrantyTelegramThread.State.ACTIVE,
    )
    intro = WarrantyTelegramMessage.objects.create(
        thread=thread, telegram_message_id='1000', direction='outbound',
        sender_name='Telegram bot', text='Старый текст',
        payload={'message_thread_id': 500},
    )
    WarrantyTelegramMessage.objects.create(
        thread=thread, telegram_message_id='1001', direction='inbound',
        sender_name='Иван', text='Обсуждение',
    )
    calls = []
    monkeypatch.setattr(
        warranty_telegram, '_config',
        lambda: (SimpleNamespace(chat_id='-100123'), SimpleNamespace()),
    )
    monkeypatch.setattr(
        warranty_telegram, 'send_telegram_request',
        lambda method, payload, **kwargs: calls.append((method, payload)),
    )

    assert update_claim_topic_message(thread) is True

    intro.refresh_from_db()
    assert calls[0][0] == 'editMessageText'
    assert calls[0][1]['message_id'] == 1000
    assert '<b>Тип ремонта:</b> Не по гарантии' in intro.text
    assert thread.messages.get(telegram_message_id='1001').text == 'Обсуждение'


@pytest.mark.django_db
def test_current_topic_intro_is_selected_over_legacy_bot_message(monkeypatch):
    claim = WarrantyClaim.objects.create(
        external_id=93,
        warranty_type=WarrantyClaim.WarrantyType.NON_WARRANTY,
    )
    thread = WarrantyTelegramThread.objects.create(
        claim=claim, chat_id='-100123', topic_id='501',
        state=WarrantyTelegramThread.State.ACTIVE,
    )
    WarrantyTelegramMessage.objects.create(
        thread=thread, telegram_message_id='1002', direction='outbound',
        sender_name='Telegram bot', text='Старое сообщение',
        payload={'message_thread_id': 400},
    )
    current = WarrantyTelegramMessage.objects.create(
        thread=thread, telegram_message_id='502', direction='outbound',
        sender_name='Telegram bot', text='Текущее первое сообщение',
        payload={'message_thread_id': 501},
    )
    calls = []

    def fake_send(method, payload, **kwargs):
        calls.append((method, payload))
        return SimpleNamespace(data={'result': True})

    monkeypatch.setattr(
        warranty_telegram, '_config',
        lambda: (SimpleNamespace(chat_id='-100123'), SimpleNamespace()),
    )
    monkeypatch.setattr(warranty_telegram, 'send_telegram_request', fake_send)

    assert update_claim_topic_message(thread) is True

    current.refresh_from_db()
    assert calls[0][1]['message_id'] == 502
    assert 'Не по гарантии' in current.text
    assert [method for method, _ in calls] == ['editMessageText']


@pytest.mark.django_db
def test_warranty_topic_message_is_recorded_by_ids():
    claim = WarrantyClaim.objects.create(external_id=82)
    thread = WarrantyTelegramThread.objects.create(
        claim=claim,
        chat_id='-100123',
        topic_id='456',
        state=WarrantyTelegramThread.State.ACTIVE,
    )
    update = {'message': {
        'message_id': 789,
        'message_thread_id': 456,
        'date': 1787212800,
        'chat': {'id': -100123},
        'from': {'id': 10, 'first_name': 'Иван'},
        'text': 'Принято в работу',
    }}

    assert record_warranty_update(update) is True
    saved = WarrantyTelegramMessage.objects.get(thread=thread)
    assert saved.telegram_message_id == '789'
    assert saved.sender_external_id == '10'
    assert saved.text == 'Принято в работу'


@pytest.mark.django_db
def test_telegram_document_is_attached_to_topic_claim(monkeypatch, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    settings.BITRIX_WARRANTY_SYNC_URL = 'https://pinel.example/warranty-sync/'
    settings.BITRIX_WARRANTY_SYNC_SECRET = 'test-secret'
    claim = WarrantyClaim.objects.create(external_id=83)
    thread = WarrantyTelegramThread.objects.create(
        claim=claim,
        chat_id='-100123',
        topic_id='457',
        state=WarrantyTelegramThread.State.ACTIVE,
    )
    update = {'message': {
        'message_id': 790,
        'message_thread_id': 457,
        'date': 1787212800,
        'chat': {'id': -100123},
        'from': {'id': 10, 'first_name': 'Иван'},
        'caption': 'Фото дефекта',
        'document': {
            'file_id': 'telegram-file-id',
            'file_unique_id': 'stable-file-id',
            'file_name': 'defect.jpg',
            'mime_type': 'image/jpeg',
            'file_size': 12,
        },
    }}
    downloads = []
    monkeypatch.setattr(
        warranty_telegram,
        '_config',
        lambda: (SimpleNamespace(), SimpleNamespace()),
    )
    monkeypatch.setattr(
        warranty_telegram,
        '_download_telegram_file',
        lambda file_id, bot: (downloads.append(file_id) or b'image-content', 'documents/defect.jpg'),
    )
    uploaded = []
    monkeypatch.setattr(
        'warranty.bitrix_sync.BitrixWarrantyClient.add_attachment',
        lambda self, claim_id, attachment: uploaded.append((claim_id, attachment.original_name)),
    )

    assert record_warranty_update(update) is True
    assert record_warranty_update(update) is True

    attachment = claim.attachments.get()
    assert attachment.original_name == 'defect.jpg'
    assert attachment.content_type == 'image/jpeg'
    assert attachment.file.read() == b'image-content'
    assert attachment.source_path == 'telegram:telegram-file-id'
    assert downloads == ['telegram-file-id']
    assert uploaded == [
        (claim.external_id, 'defect.jpg'),
        (claim.external_id, 'defect.jpg'),
    ]
    saved = WarrantyTelegramMessage.objects.get(thread=thread)
    assert saved.payload['attachments'][0]['file_unique_id'] == 'stable-file-id'


@pytest.mark.django_db
def test_edited_telegram_message_keeps_original_text():
    claim = WarrantyClaim.objects.create(external_id=831)
    thread = WarrantyTelegramThread.objects.create(
        claim=claim, chat_id='-100123', topic_id='4571',
        state=WarrantyTelegramThread.State.ACTIVE,
    )
    base_message = {
        'message_id': 791,
        'message_thread_id': 4571,
        'chat': {'id': -100123},
        'from': {'id': 42, 'first_name': 'Иван'},
        'date': 1_700_000_000,
    }

    assert record_warranty_update({'message': {**base_message, 'text': 'Первый текст'}}) is True
    assert record_warranty_update({'edited_message': {**base_message, 'text': 'Исправленный текст'}}) is True

    saved = WarrantyTelegramMessage.objects.get(thread=thread)
    assert saved.original_text == 'Первый текст'
    assert saved.text == 'Исправленный текст'
    assert saved.edited_at is not None
    assert saved.payload['last_edited_message']['text'] == 'Исправленный текст'


@pytest.mark.django_db
def test_customer_wait_has_seeded_handover_button():
    claim = WarrantyClaim.objects.create(
        external_id=84, status=WarrantyClaim.Status.CUSTOMER_WAIT,
    )

    keyboard = _status_keyboard(claim)

    button = WarrantyTelegramStatusButton.objects.get(
        source_status=WarrantyClaim.Status.CUSTOMER_WAIT,
        label='Выдано клиенту',
    )
    assert keyboard == {'inline_keyboard': [[{
        'text': 'Выдано клиенту',
        'callback_data': f'warranty:{claim.pk}:{button.pk}',
    }]]}


@pytest.mark.django_db
def test_warranty_status_callback_closes_claim_and_queues_bitrix(monkeypatch):
    claim = WarrantyClaim.objects.create(
        external_id=85,
        source='bitrix',
        source_status='5',
        status=WarrantyClaim.Status.CUSTOMER_WAIT,
    )
    thread = WarrantyTelegramThread.objects.create(
        claim=claim,
        chat_id='-100123',
        topic_id='458',
        state=WarrantyTelegramThread.State.ACTIVE,
    )
    button = WarrantyTelegramStatusButton.objects.get(
        source_status=WarrantyClaim.Status.CUSTOMER_WAIT,
        label='Выдано клиенту',
    )
    calls = []

    def fake_send(method, payload, **kwargs):
        calls.append((method, payload))
        return SimpleNamespace(data={
            'result': {'message_id': 901, 'message_thread_id': 458}
            if method == 'sendMessage' else True,
        })

    monkeypatch.setattr(
        warranty_telegram,
        '_config',
        lambda: (SimpleNamespace(chat_id='-100123'), SimpleNamespace()),
    )
    monkeypatch.setattr(warranty_telegram, 'send_telegram_request', fake_send)
    update = {'callback_query': {
        'id': 'callback-1',
        'data': f'warranty:{claim.pk}:{button.pk}',
        'from': {
            'id': 10,
            'first_name': 'Иван',
            'last_name': 'Петров',
            'username': 'ivan_petrov',
        },
        'message': {
            'message_id': 800,
            'message_thread_id': 458,
            'chat': {'id': -100123},
        },
    }}

    assert record_warranty_update(update) is True

    claim.refresh_from_db()
    thread.refresh_from_db()
    assert claim.status == WarrantyClaim.Status.CLOSED
    assert thread.state == WarrantyTelegramThread.State.CLOSE_PENDING
    assert WarrantyBitrixOutbox.objects.get(claim=claim).payload['UF_STATUS'] == '3'
    history = WarrantyHistoryEvent.objects.get(claim=claim)
    assert history.actor_name == 'Иван Петров (@ivan_petrov)'
    assert history.actor_external_id == '10'
    assert history.payload == {
        'source': 'telegram_callback',
        'telegram_username': 'ivan_petrov',
        'button_id': button.pk,
        'button_label': 'Выдано клиенту',
    }
    saved_message = WarrantyTelegramMessage.objects.get(thread=thread)
    assert saved_message.telegram_message_id == '901'
    assert saved_message.sender_external_id == '10'
    assert saved_message.sender_name == 'Иван Петров (@ivan_petrov)'
    assert 'Изменил: Иван Петров (@ivan_petrov)' in saved_message.text
    assert calls[-1][1]['text'] == saved_message.text
    assert [method for method, payload in calls] == [
        'answerCallbackQuery', 'editMessageText', 'sendMessage',
    ]


@pytest.mark.django_db
def test_status_change_queues_topic_icon_update():
    actor = User.objects.create_user('icon-admin')
    claim = WarrantyClaim.objects.create(external_id=86, status=WarrantyClaim.Status.NEW)
    thread = WarrantyTelegramThread.objects.create(
        claim=claim, chat_id='-100123', topic_id='459',
        state=WarrantyTelegramThread.State.ACTIVE,
    )
    form = WarrantyClaimUpdateForm({
        'status': WarrantyClaim.Status.DIAGNOSTICS,
        'priority': WarrantyClaim.Priority.NORMAL,
        'comment': '',
    }, instance=claim)
    assert form.is_valid(), form.errors

    update_claim(claim=claim, form=form, actor=actor)

    thread.refresh_from_db()
    assert thread.state == WarrantyTelegramThread.State.STATUS_UPDATE_PENDING


@pytest.mark.django_db
def test_bitrix_status_change_notifies_topic_with_actor(monkeypatch):
    claim = WarrantyClaim.objects.create(
        external_id=860, source='bitrix', source_status='1',
        status=WarrantyClaim.Status.NEW,
    )
    thread = WarrantyTelegramThread.objects.create(
        claim=claim, chat_id='-100123', topic_id='4590',
        state=WarrantyTelegramThread.State.ACTIVE,
    )
    import_claim_rows([{
        'ID': 860,
        'UF_STATUS': '4',
        'HISTORY': [{
            'ID': 701,
            'UF_CHANGES': 'Изменён статус',
            'UF_USER_ID': '42',
            'ACTOR_NAME': 'Анна Смирнова',
            'UF_DATE': '2026-08-20T12:00:00+03:00',
        }],
    }])
    calls = []

    def fake_send(method, payload, **kwargs):
        calls.append((method, payload))
        if method == 'sendMessage':
            return SimpleNamespace(data={'result': {'message_id': 990}})
        if method == 'getForumTopicIconStickers':
            return SimpleNamespace(data={'result': [
                {'emoji': '🛠', 'custom_emoji_id': 'work-icon'},
            ]})
        return SimpleNamespace(data={'result': True})

    warranty_telegram._forum_topic_icons.cache_clear()
    monkeypatch.setattr(
        warranty_telegram, '_config',
        lambda: (SimpleNamespace(chat_id='-100123'), SimpleNamespace()),
    )
    monkeypatch.setattr(warranty_telegram, 'send_telegram_request', fake_send)

    result = warranty_telegram.sync_warranty_topics()

    thread.refresh_from_db()
    event = WarrantyHistoryEvent.objects.get(
        claim=claim, payload__source='bitrix_status_sync',
    )
    message = WarrantyTelegramMessage.objects.get(thread=thread)
    assert result['updated'] == 1
    assert thread.state == WarrantyTelegramThread.State.ACTIVE
    assert message.telegram_message_id == '990'
    assert message.sender_external_id == '42'
    assert message.sender_name == 'Анна Смирнова'
    assert 'Новый → В работе' in message.text
    assert 'Изменил: Анна Смирнова' in message.text
    assert event.payload['telegram_notification_pending'] is False
    assert event.payload['telegram_message_id'] == '990'
    assert calls[0] == ('sendMessage', {
        'chat_id': '-100123',
        'message_thread_id': 4590,
        'text': message.text,
    })


@pytest.mark.django_db
def test_topic_sync_uses_status_custom_emoji(monkeypatch):
    claim = WarrantyClaim.objects.create(
        external_id=87, status=WarrantyClaim.Status.READY,
    )
    thread = WarrantyTelegramThread.objects.create(
        claim=claim, chat_id='-100123', topic_id='460',
        state=WarrantyTelegramThread.State.STATUS_UPDATE_PENDING,
    )
    calls = []

    def fake_send(method, payload, **kwargs):
        calls.append((method, payload))
        if method == 'getForumTopicIconStickers':
            return SimpleNamespace(data={'result': [
                {'emoji': '✅', 'custom_emoji_id': 'ready-icon'},
            ]})
        return SimpleNamespace(data={'result': True})

    warranty_telegram._forum_topic_icons.cache_clear()
    monkeypatch.setattr(
        warranty_telegram, '_config',
        lambda: (SimpleNamespace(chat_id='-100123'), SimpleNamespace()),
    )
    monkeypatch.setattr(warranty_telegram, 'send_telegram_request', fake_send)

    result = warranty_telegram.sync_warranty_topics()

    thread.refresh_from_db()
    assert result['updated'] == 1
    assert thread.state == WarrantyTelegramThread.State.ACTIVE
    assert calls[-1] == ('editForumTopic', {
        'chat_id': '-100123',
        'message_thread_id': 460,
        'icon_custom_emoji_id': 'ready-icon',
    })


@pytest.mark.django_db
def test_retryable_topic_icon_error_stays_queued(monkeypatch):
    claim = WarrantyClaim.objects.create(
        external_id=88, status=WarrantyClaim.Status.DIAGNOSTICS,
    )
    thread = WarrantyTelegramThread.objects.create(
        claim=claim, chat_id='-100123', topic_id='461',
        state=WarrantyTelegramThread.State.STATUS_UPDATE_PENDING,
    )
    monkeypatch.setattr(
        warranty_telegram, 'update_claim_topic_icon',
        lambda thread: (_ for _ in ()).throw(
            warranty_telegram.TelegramAPIError('request timeout', retryable=True),
        ),
    )

    result = warranty_telegram.sync_warranty_topics()

    thread.refresh_from_db()
    assert result['failed'] == 1
    assert thread.state == WarrantyTelegramThread.State.STATUS_UPDATE_PENDING
    assert thread.last_error == 'request timeout'


@pytest.mark.django_db
def test_already_current_topic_icon_is_success(monkeypatch):
    claim = WarrantyClaim.objects.create(
        external_id=89, status=WarrantyClaim.Status.READY,
    )
    thread = WarrantyTelegramThread.objects.create(
        claim=claim, chat_id='-100123', topic_id='462',
        state=WarrantyTelegramThread.State.STATUS_UPDATE_PENDING,
    )

    def fake_send(method, payload, **kwargs):
        if method == 'getForumTopicIconStickers':
            return SimpleNamespace(data={'result': [
                {'emoji': '✅', 'custom_emoji_id': 'ready-icon'},
            ]})
        raise warranty_telegram.TelegramAPIError(
            'Bad Request: TOPIC_NOT_MODIFIED', status_code=400, retryable=False,
        )

    warranty_telegram._forum_topic_icons.cache_clear()
    monkeypatch.setattr(
        warranty_telegram, '_config',
        lambda: (SimpleNamespace(chat_id='-100123'), SimpleNamespace()),
    )
    monkeypatch.setattr(warranty_telegram, 'send_telegram_request', fake_send)

    result = warranty_telegram.sync_warranty_topics()

    thread.refresh_from_db()
    assert result['updated'] == 1
    assert thread.state == WarrantyTelegramThread.State.ACTIVE
