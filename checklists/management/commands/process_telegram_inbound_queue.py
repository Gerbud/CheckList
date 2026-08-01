from django.core.management.base import BaseCommand, CommandError

from checklists.telegram_inbound import process_inbound_queue


class Command(BaseCommand):
    help = 'Обрабатывает очередь входящих Telegram-команд.'

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=50)
        parser.add_argument('--retry-failed', action='store_true')
        parser.add_argument('--store-code')
        parser.add_argument('--max-attempts', type=int, default=3)

    def handle(self, *args, **options):
        if options['limit'] < 1:
            raise CommandError('--limit должен быть положительным.')
        if options['max_attempts'] < 1:
            raise CommandError('--max-attempts должен быть положительным.')
        result = process_inbound_queue(
            limit=options['limit'],
            retry_failed=options['retry_failed'],
            store_code=options['store_code'],
            max_attempts=options['max_attempts'],
        )
        self.stdout.write(
            self.style.SUCCESS(
                'Захвачено: {claimed}; завершено: {completed}; ошибок: {failed}.'.format(
                    **result
                )
            )
        )
