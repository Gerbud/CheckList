from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from checklists.telegram_queue import enqueue_telegram_message


@dataclass(frozen=True)
class TelegramAction:
    method: str
    payload: dict
    idempotency_key: str
    message_type: str
    chat_id: str
    store: object = None


_ACTION_COLLECTOR = ContextVar('telegram_action_collector', default=None)


@contextmanager
def collect_telegram_actions():
    actions = []
    token = _ACTION_COLLECTOR.set(actions)
    try:
        yield actions
    finally:
        _ACTION_COLLECTOR.reset(token)


def emit_telegram_action(action):
    collector = _ACTION_COLLECTOR.get()
    if collector is not None:
        collector.append(action)
        return action
    return enqueue_telegram_message(
        store=action.store,
        chat_id=action.chat_id,
        method=action.method,
        message_type=action.message_type,
        idempotency_key=action.idempotency_key,
        payload=action.payload,
    )
