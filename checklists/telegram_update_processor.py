"""Classification and shared entry points for every Telegram transport."""

from dataclasses import dataclass


class UpdateMode:
    SYNCHRONOUS = 'synchronous'
    BACKGROUND = 'background'
    IGNORED = 'ignored'


BACKGROUND_COMMANDS = {'/report', '/reports', '/analytics'}


def classify_telegram_update(update):
    message = update.get('message') or {}
    callback = update.get('callback_query') or {}
    source = callback.get('from') or message.get('from') or {}
    if source.get('is_bot'):
        return UpdateMode.IGNORED
    if callback:
        return UpdateMode.SYNCHRONOUS
    if message:
        text = str(message.get('text', '')).strip()
        command = text.split()[0].split('@')[0].lower() if text.startswith('/') else ''
        return (
            UpdateMode.BACKGROUND
            if command in BACKGROUND_COMMANDS
            else UpdateMode.SYNCHRONOUS
        )
    if update.get('channel_post') or update.get('edited_channel_post'):
        return UpdateMode.BACKGROUND
    return UpdateMode.IGNORED


@dataclass(frozen=True)
class TelegramProcessResult:
    outcome: str
    processing_mode: str
    actions: tuple = ()
    created_task_id: int | None = None
    safe_error: str = ''


def process_telegram_update(*args, **kwargs):
    from checklists.telegram_bot import process_telegram_update as processor
    return processor(*args, **kwargs)


def process_logged_telegram_update(*args, **kwargs):
    from checklists.telegram_bot import process_logged_telegram_update as processor
    return processor(*args, **kwargs)


def poll_telegram_updates(*args, **kwargs):
    from checklists.telegram_bot import poll_telegram_updates as poller
    return poller(*args, **kwargs)
