from django.utils import timezone

from checklists.models import TelegramSystemSettings
from checklists.telegram_client import TelegramAPIError, send_telegram_request


BOT_COMMANDS = (
    {'command': 'start', 'description': 'Главное меню'},
    {'command': 'help', 'description': 'Помощь'},
    {'command': 'menu', 'description': 'Главное меню'},
    {'command': 'tasks', 'description': 'Задачи магазина'},
    {'command': 'newtask', 'description': 'Поставить задачу'},
    {'command': 'status', 'description': 'Статус магазина'},
    {'command': 'myid', 'description': 'Мой Telegram ID'},
)


def register_bot_commands(config=None):
    config = config or TelegramSystemSettings.get_solo()
    try:
        response = send_telegram_request(
            'setMyCommands',
            {'commands': list(BOT_COMMANDS)},
            system_settings=config,
        )
    except TelegramAPIError as exc:
        config.bot_commands_last_error = str(exc)
        config.save(update_fields=('bot_commands_last_error', 'updated_at'))
        raise
    config.bot_commands_registered_at = timezone.now()
    config.bot_commands_last_error = ''
    config.save(
        update_fields=(
            'bot_commands_registered_at',
            'bot_commands_last_error',
            'updated_at',
        )
    )
    return response


def get_bot_commands(config=None):
    config = config or TelegramSystemSettings.get_solo()
    try:
        response = send_telegram_request(
            'getMyCommands',
            {},
            system_settings=config,
        )
    except TelegramAPIError as exc:
        config.bot_commands_last_error = str(exc)
        config.bot_commands_last_checked_at = timezone.now()
        config.save(
            update_fields=(
                'bot_commands_last_error',
                'bot_commands_last_checked_at',
                'updated_at',
            )
        )
        raise
    config.bot_commands_last_checked_at = timezone.now()
    config.bot_commands_last_error = ''
    config.save(
        update_fields=(
            'bot_commands_last_checked_at',
            'bot_commands_last_error',
            'updated_at',
        )
    )
    return response.data.get('result') or []
