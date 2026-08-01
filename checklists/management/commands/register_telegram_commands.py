from django.core.management.base import BaseCommand, CommandError

from checklists.telegram_client import TelegramAPIError
from checklists.telegram_commands import register_bot_commands


class Command(BaseCommand):
    help = 'Регистрирует системное меню команд Telegram-бота.'

    def handle(self, *args, **options):
        try:
            register_bot_commands()
        except TelegramAPIError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS('Команды Telegram зарегистрированы.'))
