"""Classification and shared entry points for every Telegram transport."""

from dataclasses import dataclass


class UpdateMode:
    SYNCHRONOUS = 'synchronous'
    BACKGROUND = 'background'
    IGNORED = 'ignored'


BACKGROUND_COMMANDS = {'/report', '/reports', '/analytics'}

SERVICE_MESSAGE_FIELDS = {
    'new_chat_members',
    'left_chat_member',
    'new_chat_title',
    'new_chat_photo',
    'delete_chat_photo',
    'group_chat_created',
    'supergroup_chat_created',
    'channel_chat_created',
    'message_auto_delete_timer_changed',
    'migrate_to_chat_id',
    'migrate_from_chat_id',
    'pinned_message',
    'forum_topic_created',
    'forum_topic_closed',
    'forum_topic_reopened',
    'forum_topic_edited',
    'general_forum_topic_hidden',
    'general_forum_topic_unhidden',
}


def _is_service_message(message):
    return any(field in message for field in SERVICE_MESSAGE_FIELDS)


def classify_telegram_update(update):
    message = update.get('message') or {}
    callback = update.get('callback_query') or {}
    source = callback.get('from') or message.get('from') or {}
    if source.get('is_bot'):
        return UpdateMode.IGNORED
    if callback:
        return UpdateMode.SYNCHRONOUS
    if message:
        if _is_service_message(message):
            return UpdateMode.IGNORED
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
