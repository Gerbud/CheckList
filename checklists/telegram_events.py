from dataclasses import dataclass


@dataclass(frozen=True)
class TelegramVariable:
    code: str
    title: str
    example: str
    description: str = ''

    @property
    def token(self):
        return f'{{{self.code}}}'


@dataclass(frozen=True)
class TelegramEvent:
    code: str
    title: str
    description: str
    category: str
    variables: tuple[TelegramVariable, ...]

    @property
    def variable_codes(self):
        return frozenset(variable.code for variable in self.variables)


class TelegramEventCategory:
    STAGES = 'stages'
    TASKS = 'tasks'
    BINDING = 'binding'
    SYSTEM = 'system'
    TEST = 'test'

    CHOICES = (
        (STAGES, 'Этапы'),
        (TASKS, 'Задачи'),
        (BINDING, 'Привязка'),
        (SYSTEM, 'Системные'),
        (TEST, 'Тестовые'),
    )


VARIABLES = {
    'store_name': TelegramVariable(
        'store_name', 'Название магазина', 'Магазин на Ленина',
        'Название магазина, к которому относится событие.',
    ),
    'date': TelegramVariable(
        'date', 'Дата', '18.07.2026', 'Рабочая дата события.',
    ),
    'stage_name': TelegramVariable(
        'stage_name', 'Этап', 'Утренние задачи', 'Название этапа чек-листа.',
    ),
    'deadline': TelegramVariable(
        'deadline', 'Дедлайн', '10:30', 'Время, до которого нужно завершить этап.',
    ),
    'task_text': TelegramVariable(
        'task_text', 'Задача', 'Проверить витрину', 'Краткий текст разовой задачи.',
    ),
    'task_description': TelegramVariable(
        'task_description', 'Описание задачи', 'Сверить ценники и выкладку',
        'Подробная инструкция к разовой задаче.',
    ),
    'employee_name': TelegramVariable(
        'employee_name', 'Сотрудник', 'Анна Иванова',
        'Имя сотрудника, который выполнил действие.',
    ),
    'comment': TelegramVariable(
        'comment', 'Комментарий', 'Нужна проверка директора',
        'Комментарий, причина или дополнительная информация.',
    ),
    'task_url': TelegramVariable(
        'task_url', 'Ссылка на задачу', 'https://example.test/tasks/42/',
        'Прямая ссылка на разовую задачу.',
    ),
    'checklist_url': TelegramVariable(
        'checklist_url', 'Ссылка на чек-лист', 'https://example.test/checklist/',
        'Прямая ссылка на ежедневный чек-лист.',
    ),
    'remaining_count': TelegramVariable(
        'remaining_count', 'Осталось пунктов', '3',
        'Количество пунктов, которые ещё не завершены.',
    ),
    'failed_count': TelegramVariable(
        'failed_count', 'Ошибок', '1',
        'Количество пунктов со статусом ошибки.',
    ),
}


def _variables(*codes):
    return tuple(VARIABLES[code] for code in codes)


TELEGRAM_EVENTS = (
    TelegramEvent(
        'test_message',
        'Тестовое сообщение',
        'Проверка доставки сообщений для выбранного магазина.',
        TelegramEventCategory.TEST,
        _variables('store_name', 'date'),
    ),
    TelegramEvent(
        'stage_reminder_30',
        'Напоминание за 30 минут',
        'Предупреждение о приближении дедлайна этапа.',
        TelegramEventCategory.STAGES,
        _variables(
            'store_name',
            'date',
            'stage_name',
            'deadline',
            'remaining_count',
            'checklist_url',
        ),
    ),
    TelegramEvent(
        'stage_reminder_10',
        'Напоминание за 10 минут',
        'Срочное предупреждение о приближении дедлайна этапа.',
        TelegramEventCategory.STAGES,
        _variables(
            'store_name',
            'date',
            'stage_name',
            'deadline',
            'remaining_count',
            'checklist_url',
        ),
    ),
    TelegramEvent(
        'stage_closed',
        'Этап закрыт',
        'Уведомление об успешном завершении этапа.',
        TelegramEventCategory.STAGES,
        _variables(
            'store_name', 'date', 'stage_name', 'employee_name', 'checklist_url'
        ),
    ),
    TelegramEvent(
        'stage_overdue',
        'Этап просрочен',
        'Уведомление о незавершённом вовремя этапе.',
        TelegramEventCategory.STAGES,
        _variables(
            'store_name',
            'date',
            'stage_name',
            'deadline',
            'remaining_count',
            'checklist_url',
        ),
    ),
    TelegramEvent(
        'incomplete_tasks',
        'Невыполненные задачи',
        'Сводка незавершённых и проблемных пунктов.',
        TelegramEventCategory.STAGES,
        _variables(
            'store_name',
            'date',
            'stage_name',
            'remaining_count',
            'failed_count',
            'comment',
            'checklist_url',
        ),
    ),
    TelegramEvent(
        'task_created',
        'Разовая задача создана',
        'Уведомление о новой разовой задаче.',
        TelegramEventCategory.TASKS,
        _variables(
            'store_name',
            'date',
            'stage_name',
            'task_text',
            'task_description',
            'task_url',
        ),
    ),
    TelegramEvent(
        'task_completed',
        'Разовая задача выполнена',
        'Уведомление об успешном выполнении разовой задачи.',
        TelegramEventCategory.TASKS,
        _variables(
            'store_name',
            'date',
            'stage_name',
            'task_text',
            'employee_name',
            'comment',
            'task_url',
        ),
    ),
    TelegramEvent(
        'task_failed',
        'Разовая задача не выполнена',
        'Уведомление о проблеме при выполнении разовой задачи.',
        TelegramEventCategory.TASKS,
        _variables(
            'store_name',
            'date',
            'stage_name',
            'task_text',
            'employee_name',
            'comment',
            'task_url',
        ),
    ),
    TelegramEvent(
        'telegram_binding_pending',
        'Привязка ожидает подтверждения',
        'Код и инструкция для новой Telegram-привязки.',
        TelegramEventCategory.BINDING,
        _variables('comment'),
    ),
    TelegramEvent(
        'telegram_binding_approved',
        'Привязка подтверждена',
        'Подтверждение успешной привязки пользователя.',
        TelegramEventCategory.BINDING,
        _variables('store_name'),
    ),
    TelegramEvent(
        'telegram_delivery_failed',
        'Ошибка доставки',
        'Системное уведомление о недоставленном сообщении.',
        TelegramEventCategory.SYSTEM,
        _variables('store_name', 'date', 'comment'),
    ),
)

TELEGRAM_EVENTS_BY_CODE = {event.code: event for event in TELEGRAM_EVENTS}
TELEGRAM_EVENT_CHOICES = tuple((event.code, event.title) for event in TELEGRAM_EVENTS)
TELEGRAM_CATEGORY_LABELS = dict(TelegramEventCategory.CHOICES)


def get_telegram_event(event_code):
    try:
        return TELEGRAM_EVENTS_BY_CODE[event_code]
    except KeyError as exc:
        raise ValueError(f'Неизвестное событие Telegram: {event_code}') from exc
