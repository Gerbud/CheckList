from django.core.management.base import BaseCommand, CommandError

from checklists.models import TelegramSystemSettings
from checklists.telegram_update_processor import poll_telegram_updates
from checklists.telegram_client import TelegramAPIError


class Command(BaseCommand):
    help = 'Получает Telegram updates polling-методом и дедуплицирует update_id.'

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=100)
        parser.add_argument('--timeout', type=int, default=0)
        parser.add_argument('--force', action='store_true')

    def handle(self, *args, **options):
        if not 1 <= options['limit'] <= 100:
            raise CommandError('--limit должен быть от 1 до 100.')
        if not 0 <= options['timeout'] <= 50:
            raise CommandError('--timeout должен быть от 0 до 50 секунд.')
        config = TelegramSystemSettings.get_solo()
        if (
            config.incoming_mode == TelegramSystemSettings.IncomingMode.WEBHOOK
            and config.webhook_is_enabled
            and not options['force']
        ):
            self.stdout.write(
                self.style.WARNING(
                    'Polling отключён: активен Telegram webhook. '
                    'Используйте --force только для диагностики.'
                )
            )
            return
        try:
            result = poll_telegram_updates(
                limit=options['limit'],
                timeout=options['timeout'],
            )
        except TelegramAPIError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(
            self.style.SUCCESS(
                'Получено: {received}; обработано: {processed}; '
                'дубликатов: {duplicate}; ошибок: {failed}.'.format(**result)
            )
        )
