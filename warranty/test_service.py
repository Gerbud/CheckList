from datetime import date
from io import BytesIO

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from PIL import Image

from warranty.models import WarrantyCustomerDocument, WarrantyCustomerProfile, WarrantyCustomerSession, WarrantyProductRegistration


pytestmark = pytest.mark.django_db


def _image(name):
    content = BytesIO()
    Image.new('RGB', (20, 20), '#35a936').save(content, 'JPEG')
    return SimpleUploadedFile(name, content.getvalue(), content_type='image/jpeg')


def _payload(flow='registration'):
    return {
        'flow': flow,
        'full_name': 'Иванов Иван Иванович',
        'phone': '8 999 123-45-67',
        'article': '2506807',
        'serial_number': 'GW-2026-100',
        'purchase_date': date.today().isoformat(),
        'consent': 'on',
        'label_photo': _image('label.jpg'),
        'receipt_photo': _image('receipt.jpg'),
    }


def test_service_page_is_public_and_mobile_ready(client):
    response = client.get(reverse('warranty:service_home'))
    content = response.content.decode()
    assert response.status_code == 200
    assert 'width=device-width' in content
    assert 'Активировать гарантию' in content
    assert 'Greenworks' in content


def test_web_registration_saves_profile_product_and_documents(client, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    response = client.post(reverse('warranty:service_home'), _payload())
    assert response.status_code == 200
    assert 'Покупка зарегистрирована' in response.content.decode()
    assert WarrantyCustomerProfile.objects.count() == 1
    assert WarrantyProductRegistration.objects.count() == 1
    assert WarrantyCustomerDocument.objects.count() == 2
    session = WarrantyCustomerSession.objects.get()
    assert session.phone == '+79991234567'
    assert session.step == session.Step.SUBMITTED


def test_claim_requires_card_and_defect(client):
    response = client.post(reverse('warranty:service_home'), _payload('claim'))
    content = response.content.decode()
    assert response.status_code == 200
    assert 'Добавьте фото гарантийного талона' in content
    assert 'Коротко опишите неисправность' in content


def test_claim_is_created_in_bitrix_with_all_documents(client, settings, tmp_path, monkeypatch):
    settings.MEDIA_ROOT = tmp_path
    calls = []

    def fake_call(self, action, payload=None):
        calls.append((action, payload))
        if action == 'claims.create':
            return {'id': 901}
        if action == 'claims.blank':
            return {'url': 'https://pinel.ru/claim-901.pdf'}
        return {}

    monkeypatch.setattr('warranty.service_views.BitrixWarrantyClient.call', fake_call)
    payload = _payload('claim')
    payload.update(defect='Двигатель останавливается под нагрузкой', warranty_card_photo=_image('card.jpg'))
    response = client.post(reverse('warranty:service_home'), payload)
    assert response.status_code == 200
    assert 'Заявка №901 оформлена' in response.content.decode()
    assert [action for action, _ in calls].count('claims.files.add') == 3
    fields = calls[0][1]['fields']
    assert fields['UF_DEFECT'] == 'Двигатель останавливается под нагрузкой'


def test_service_domain_uses_public_page_at_root(client):
    response = client.get('/', HTTP_HOST='service.pinel.ru')
    assert response.status_code == 200
    assert 'Гарантия без поездки' in response.content.decode()
