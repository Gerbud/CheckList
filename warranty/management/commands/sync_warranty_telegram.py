from django.core.management.base import BaseCommand, CommandError

from warranty.telegram import refresh_existing_claim_messages, sync_warranty_topics


class Command(BaseCommand):
    help = 'Создаёт, закрывает, восстанавливает и обновляет иконки тем гарантийных обращений.'

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=50)
        parser.add_argument(
            '--refresh-existing-messages', action='store_true',
            help='Обновить только сохранённые стартовые сообщения существующих тем.',
        )

    def handle(self, *args, **options):
        if not 1 <= options['limit'] <= 200:
            raise CommandError('--limit должен быть от 1 до 200.')
        if options['refresh_existing_messages']:
            result = refresh_existing_claim_messages(options['limit'])
            self.stdout.write('Сообщений обновлено: {updated}; пропущено: {skipped}; rate limit: {rate_limited}; ошибок: {failed}.'.format(**result))
        else:
            result = sync_warranty_topics(options['limit'])
            self.stdout.write('Создано: {created}; закрыто: {closed}; восстановлено: {reopened}; иконок и сообщений обновлено: {updated}; rate limit: {rate_limited}; ошибок: {failed}.'.format(**result))
