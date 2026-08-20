import json
from io import StringIO

import pytest
from django.contrib.auth.models import User
from django.core.management import call_command
from django.urls import reverse

from checklists.models import EmployeeProfile
from warranty.forms import WarrantyClaimUpdateForm
from warranty.models import WarrantyBitrixOutbox, WarrantyClaim, WarrantyHistoryEvent, WarrantyTelegramSettings, WarrantyTelegramThread
from warranty.services import update_claim


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
