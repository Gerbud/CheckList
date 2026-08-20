from django.core.management.base import BaseCommand, CommandError

from warranty.greenworks import refresh_catalog


class Command(BaseCommand):
    help = 'Обновляет локальный индекс чертежей Greenworks по артикулам.'

    def add_arguments(self, parser):
        parser.add_argument('--timeout', type=float, default=30)

    def handle(self, *args, **options):
        try:
            result = refresh_catalog(timeout=options['timeout'])
        except Exception as exc:
            raise CommandError(f'Не удалось обновить каталог Greenworks: {exc}') from exc
        self.stdout.write(self.style.SUCCESS(
            f'Каталог Greenworks обновлён: {result["articles"]} артикулов, {result["pages"]} стр.'
        ))
