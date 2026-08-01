from django.core.management.base import BaseCommand, CommandError

from checklists.telegram_queue import process_telegram_queue


class Command(BaseCommand):
    help = 'Обрабатывает безопасно захваченную очередь исходящих Telegram-сообщений.'

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=100)
        parser.add_argument('--retry-failed', action='store_true')
        parser.add_argument('--store-code')

    def handle(self, *args, **options):
        if not 1 <= options['limit'] <= 1000:
            raise CommandError('--limit должен быть от 1 до 1000.')
        result = process_telegram_queue(
            limit=options['limit'],
            retry_failed=options['retry_failed'],
            store_code=options['store_code'],
        )
        self.stdout.write(
            self.style.SUCCESS(
                'Возвращено после timeout: {recovered}; захвачено: {claimed}; '
                'отправлено: {sent}; ошибок: {failed}.'.format(
                    **result
                )
            )
        )
