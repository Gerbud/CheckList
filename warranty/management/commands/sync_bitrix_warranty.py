from django.core.management.base import BaseCommand, CommandError

from warranty.bitrix_sync import BitrixSyncError, synchronize


class Command(BaseCommand):
    help = 'Двусторонняя синхронизация гарантийных обращений с модулем 1С-Битрикс.'

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=100)

    def handle(self, *args, **options):
        try:
            result = synchronize(limit=max(1, min(options['limit'], 500)))
        except BitrixSyncError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(
            'Получено: {imported}; отправлено: {sent}; ошибок: {errors}'.format(**result)
        ))
