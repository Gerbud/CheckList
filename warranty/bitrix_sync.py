import hashlib
import hmac
import json
from datetime import date
from decimal import Decimal, InvalidOperation
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from warranty.models import (
    WarrantyAttachment,
    WarrantyBitrixOutbox,
    WarrantyBitrixSyncState,
    WarrantyClaim,
    WarrantyHistoryEvent,
    WarrantyTelegramThread,
)


STATUS_FROM_BITRIX = {
    '1': WarrantyClaim.Status.NEW,
    '2': WarrantyClaim.Status.SERVICE_DECISION,
    '3': WarrantyClaim.Status.CLOSED,
    '4': WarrantyClaim.Status.IN_PROGRESS,
    '5': WarrantyClaim.Status.CUSTOMER_WAIT,
}
TYPE_FROM_BITRIX = {'1': WarrantyClaim.WarrantyType.WARRANTY, '2': WarrantyClaim.WarrantyType.NON_WARRANTY}


class BitrixSyncError(RuntimeError):
    pass


def _string(value):
    return '' if value is None else str(value).strip()


def _bool(value):
    return value in (True, 1, '1', 'Y', 'y', 'true', 'True')


def _datetime(value):
    parsed = parse_datetime(_string(value)) if value else None
    if parsed and timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed)
    return parsed


def _date(value):
    if not value:
        return None
    if isinstance(value, date):
        return value
    return parse_date(_string(value)[:10])


class BitrixWarrantyClient:
    def __init__(self, url=None, secret=None):
        self.url = url or settings.BITRIX_WARRANTY_SYNC_URL
        self.secret = secret or settings.BITRIX_WARRANTY_SYNC_SECRET
        if not self.url or not self.secret:
            raise BitrixSyncError('BITRIX_WARRANTY_SYNC_URL и BITRIX_WARRANTY_SYNC_SECRET должны быть настроены.')

    def call(self, action, payload=None):
        body = json.dumps({'action': action, 'payload': payload or {}}, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        timestamp = str(int(timezone.now().timestamp()))
        signature = hmac.new(self.secret.encode(), timestamp.encode() + b'.' + body, hashlib.sha256).hexdigest()
        request = Request(
            self.url,
            data=body,
            headers={
                'Content-Type': 'application/json',
                'X-Warranty-Timestamp': timestamp,
                'X-Warranty-Signature': signature,
            },
            method='POST',
        )
        try:
            with urlopen(request, timeout=settings.BITRIX_WARRANTY_SYNC_TIMEOUT) as response:
                result = json.loads(response.read().decode('utf-8'))
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise BitrixSyncError(f'Ошибка обращения к Bitrix: {exc}') from exc
        if not result.get('ok'):
            raise BitrixSyncError(result.get('error') or 'Bitrix вернул неизвестную ошибку.')
        return result.get('result', {})


@transaction.atomic
def import_claim_rows(rows):
    imported = 0
    for row in rows:
        external_id = int(row['ID'])
        source_status = _string(row.get('UF_STATUS'))
        try:
            repair_price = Decimal(_string(row.get('UF_PRICE'))) if row.get('UF_PRICE') not in (None, '') else None
        except InvalidOperation:
            repair_price = None
        claim, _ = WarrantyClaim.objects.update_or_create(
            source='bitrix',
            external_id=external_id,
            defaults={
                'status': STATUS_FROM_BITRIX.get(source_status, WarrantyClaim.Status.NEW),
                'source_status': source_status,
                'warranty_type': TYPE_FROM_BITRIX.get(_string(row.get('UF_TYPE')), WarrantyClaim.WarrantyType.WARRANTY),
                'customer_name': _string(row.get('UF_FIO')),
                'phone': _string(row.get('UF_PHONE')),
                'email': _string(row.get('UF_EMAIL')),
                'external_user_id': _string(row.get('UF_USER_ID')),
                'external_created_by_id': _string(row.get('UF_CREATE_BY')),
                'created_by_name': _string(row.get('CREATED_BY_NAME')),
                'product_name': _string(row.get('UF_PRODUCT_NAME')),
                'external_product_id': _string(row.get('UF_PRODUCT_ID')),
                'serial_number': _string(row.get('UF_SERIAL_NUMBER')),
                'defect': _string(row.get('UF_DEFECT')),
                'equipment': _string(row.get('UF_EQUIPMENT')),
                'comment': _string(row.get('UF_COMMENT')),
                'purchase_date': _date(row.get('UF_DATE_OF_PURCHASE')),
                'repair_price': repair_price,
                'product_remains_with_customer': _bool(row.get('UF_PRODUCT_REMAINS_WITH_CLIENT')),
                'purchased_from_us': _bool(row.get('UF_PURCHASED_FROM_US')),
                'source_created_at': _datetime(row.get('UF_CREATE_DATE')),
                'source_updated_at': _datetime(row.get('SOURCE_UPDATED_AT')),
                'raw_source_data': row,
            },
        )
        WarrantyTelegramThread.objects.get_or_create(
            claim=claim,
            defaults={'title': f'Гарантия #{external_id}: {claim.product_name}'[:255]},
        )
        for event in row.get('HISTORY', []):
            event_id = _string(event.get('ID'))
            WarrantyHistoryEvent.objects.update_or_create(
                claim=claim,
                external_id=event_id,
                defaults={
                    'kind': WarrantyHistoryEvent.Kind.CHANGE,
                    'text': _string(event.get('UF_CHANGES')),
                    'actor_external_id': _string(event.get('UF_USER_ID')),
                    'actor_name': _string(event.get('ACTOR_NAME')),
                    'occurred_at': _datetime(event.get('UF_DATE')) or timezone.now(),
                    'payload': event,
                },
            )
        for attachment in row.get('FILES', []):
            WarrantyAttachment.objects.update_or_create(
                claim=claim,
                external_file_id=_string(attachment.get('ID')),
                defaults={
                    'original_name': _string(attachment.get('ORIGINAL_NAME')),
                    'content_type': _string(attachment.get('CONTENT_TYPE')),
                    'size': int(attachment.get('FILE_SIZE') or 0),
                    'source_path': _string(attachment.get('SRC')),
                },
            )
        imported += 1
    return imported


def synchronize(limit=100):
    client = BitrixWarrantyClient()
    state = WarrantyBitrixSyncState.get_solo()
    result = client.call('claims.list', {
        'sinceClaimId': state.claim_cursor,
        'sinceHistoryId': state.history_cursor,
        'limit': limit,
    })
    imported = import_claim_rows(result.get('claims', []))
    state.claim_cursor = max(state.claim_cursor, int(result.get('claimCursor') or 0))
    state.history_cursor = max(state.history_cursor, int(result.get('historyCursor') or 0))
    state.last_success_at = timezone.now()
    state.last_error = ''
    state.save()

    sent = 0
    errors = 0
    queryset = WarrantyBitrixOutbox.objects.select_related('claim').filter(
        status__in=(WarrantyBitrixOutbox.Status.PENDING, WarrantyBitrixOutbox.Status.ERROR),
    )[:limit]
    for item in queryset:
        item.status = WarrantyBitrixOutbox.Status.SENDING
        item.attempts += 1
        item.save(update_fields=('status', 'attempts'))
        try:
            client.call('claims.update', {'id': item.claim.external_id, 'fields': item.payload})
        except BitrixSyncError as exc:
            item.status = WarrantyBitrixOutbox.Status.ERROR
            item.last_error = str(exc)[:2000]
            errors += 1
        else:
            item.status = WarrantyBitrixOutbox.Status.SENT
            item.last_error = ''
            item.sent_at = timezone.now()
            sent += 1
        item.save(update_fields=('status', 'attempts', 'last_error', 'sent_at'))
    return {'imported': imported, 'sent': sent, 'errors': errors}
