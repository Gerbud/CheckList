from django.core.management.base import BaseCommand, CommandError

from checklists.models import Store
from checklists.telegram_reminders import (
    schedule_employee_schedule_reminders,
    schedule_telegram_notifications,
)


class Command(BaseCommand):
    help = 'Идемпотентно ставит в Telegram-очередь напоминания по этапам.'

    def add_arguments(self, parser):
        parser.add_argument('--store-code')

    def handle(self, *args, **options):
        store_code = options['store_code']
        if store_code and not Store.objects.filter(code=store_code).exists():
            raise CommandError('Магазин с таким кодом не найден.')
        checklist_created = schedule_telegram_notifications(
            store_code=store_code
        )
        schedule_created = schedule_employee_schedule_reminders(
            store_code=store_code
        )
        created = checklist_created + schedule_created
        self.stdout.write(
            self.style.SUCCESS(
                f'Поставлено в очередь: {created}. '
                f'Чек-листы: {checklist_created}; '
                f'графики: {schedule_created}.'
            )
        )
