from django.core.management.base import BaseCommand, CommandError

from warranty.telegram import sync_warranty_topics


class Command(BaseCommand):
    help = 'Создаёт, закрывает и восстанавливает темы гарантийных обращений через рабочего бота и его gateway.'

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=50)

    def handle(self, *args, **options):
        if not 1 <= options['limit'] <= 200:
            raise CommandError('--limit должен быть от 1 до 200.')
        result = sync_warranty_topics(options['limit'])
        self.stdout.write('Создано: {created}; закрыто: {closed}; восстановлено: {reopened}; rate limit: {rate_limited}; ошибок: {failed}.'.format(**result))
