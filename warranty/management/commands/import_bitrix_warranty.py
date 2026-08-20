import json
import sys
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.core.files import File
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from warranty.models import (
    WarrantyAttachment,
    WarrantyClaim,
    WarrantyHistoryEvent,
    WarrantyTelegramThread,
)


STATUS_MAP = {
    '1': WarrantyClaim.Status.NEW,
    '2': WarrantyClaim.Status.SERVICE_DECISION,
    '3': WarrantyClaim.Status.CLOSED,
    '4': WarrantyClaim.Status.IN_PROGRESS,
    '5': WarrantyClaim.Status.CUSTOMER_WAIT,
}
TYPE_MAP = {'1': WarrantyClaim.WarrantyType.WARRANTY, '2': WarrantyClaim.WarrantyType.NON_WARRANTY}


def clean_string(value):
    return '' if value is None else str(value).strip()


def clean_bool(value):
    return value in (True, 1, '1', 'Y', 'y', 'true', 'True')


def clean_datetime(value):
    if not value:
        return None
    parsed = parse_datetime(clean_string(value))
    if parsed and timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed)
    return parsed


def clean_date(value):
    if not value:
        return None
    if isinstance(value, date):
        return value
    return parse_date(clean_string(value)[:10])


class Command(BaseCommand):
    help = 'Идемпотентно импортирует гарантийные обращения, историю и файлы из безопасного JSON-экспорта Bitrix.'

    def add_arguments(self, parser):
        parser.add_argument('payload', help='Путь к JSON или - для stdin')
        parser.add_argument('--files-root', default='', help='Каталог прочитанных с Bitrix файлов')

    @transaction.atomic
    def handle(self, *args, **options):
        try:
            raw = sys.stdin.read() if options['payload'] == '-' else Path(options['payload']).read_text(encoding='utf-8')
            payload = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            raise CommandError(f'Не удалось прочитать экспорт: {exc}') from exc
        if not isinstance(payload, dict) or not isinstance(payload.get('claims'), list):
            raise CommandError('Ожидается JSON-объект с массивом claims.')

        files_root = Path(options['files_root']).resolve() if options['files_root'] else None
        imported = 0
        for row in payload['claims']:
            external_id = int(row['ID'])
            source_status = clean_string(row.get('UF_STATUS'))
            try:
                repair_price = Decimal(clean_string(row.get('UF_PRICE'))) if row.get('UF_PRICE') not in (None, '') else None
            except InvalidOperation:
                repair_price = None
            defaults = {
                'status': STATUS_MAP.get(source_status, WarrantyClaim.Status.NEW),
                'source_status': source_status,
                'warranty_type': TYPE_MAP.get(clean_string(row.get('UF_TYPE')), WarrantyClaim.WarrantyType.WARRANTY),
                'customer_name': clean_string(row.get('UF_FIO')),
                'phone': clean_string(row.get('UF_PHONE')),
                'email': clean_string(row.get('UF_EMAIL')),
                'external_user_id': clean_string(row.get('UF_USER_ID')),
                'external_created_by_id': clean_string(row.get('UF_CREATE_BY')),
                'created_by_name': clean_string(row.get('CREATED_BY_NAME')),
                'product_name': clean_string(row.get('UF_PRODUCT_NAME')),
                'external_product_id': clean_string(row.get('UF_PRODUCT_ID')),
                'serial_number': clean_string(row.get('UF_SERIAL_NUMBER')),
                'defect': clean_string(row.get('UF_DEFECT')),
                'equipment': clean_string(row.get('UF_EQUIPMENT')),
                'comment': clean_string(row.get('UF_COMMENT')),
                'purchase_date': clean_date(row.get('UF_DATE_OF_PURCHASE')),
                'repair_price': repair_price,
                'product_remains_with_customer': clean_bool(row.get('UF_PRODUCT_REMAINS_WITH_CLIENT')),
                'purchased_from_us': clean_bool(row.get('UF_PURCHASED_FROM_US')),
                'source_created_at': clean_datetime(row.get('UF_CREATE_DATE')),
                'source_updated_at': clean_datetime(row.get('SOURCE_UPDATED_AT')),
                'raw_source_data': row,
            }
            claim, _ = WarrantyClaim.objects.update_or_create(source='bitrix', external_id=external_id, defaults=defaults)
            WarrantyTelegramThread.objects.get_or_create(
                claim=claim,
                defaults={
                    'title': f'Гарантия #{external_id}: {claim.product_name}'[:255],
                    'state': WarrantyTelegramThread.State.ARCHIVED if claim.is_closed else WarrantyTelegramThread.State.PLANNED,
                    'archived_at': timezone.now() if claim.is_closed else None,
                },
            )
            for event in row.get('HISTORY', []):
                event_id = clean_string(event.get('ID'))
                WarrantyHistoryEvent.objects.update_or_create(
                    claim=claim,
                    external_id=event_id,
                    defaults={
                        'kind': WarrantyHistoryEvent.Kind.CHANGE,
                        'text': clean_string(event.get('UF_CHANGES')),
                        'actor_external_id': clean_string(event.get('UF_USER_ID')),
                        'actor_name': clean_string(event.get('ACTOR_NAME')),
                        'occurred_at': clean_datetime(event.get('UF_DATE')) or timezone.now(),
                        'payload': event,
                    },
                )
            for attachment in row.get('FILES', []):
                file_id = clean_string(attachment.get('ID'))
                obj, _ = WarrantyAttachment.objects.update_or_create(
                    claim=claim,
                    external_file_id=file_id,
                    defaults={
                        'original_name': clean_string(attachment.get('ORIGINAL_NAME')),
                        'content_type': clean_string(attachment.get('CONTENT_TYPE')),
                        'size': int(attachment.get('FILE_SIZE') or 0),
                        'source_path': clean_string(attachment.get('SRC')),
                    },
                )
                relative_path = clean_string(attachment.get('LOCAL_PATH'))
                if files_root and relative_path:
                    source_file = (files_root / relative_path).resolve()
                    if files_root in source_file.parents and source_file.is_file() and not obj.file:
                        with source_file.open('rb') as handle:
                            obj.file.save(Path(obj.original_name or source_file.name).name, File(handle), save=True)
            imported += 1
        self.stdout.write(self.style.SUCCESS(f'Импортировано/обновлено обращений: {imported}'))
