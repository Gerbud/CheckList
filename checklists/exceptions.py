class ChecklistServiceError(Exception):
    """Базовая ошибка бизнес-логики чек-листов."""


class DuplicateDailyChecklistError(ChecklistServiceError):
    """Ежедневный чек-лист сотрудника на дату уже существует."""


class TemplateConfigurationError(ChecklistServiceError):
    """Опубликованный шаблон магазина не найден или неоднозначен."""


class OperationNotAllowedError(ChecklistServiceError):
    """Пользователь не имеет права выполнить операцию."""


class ChecklistLockedError(ChecklistServiceError):
    """Завершённый чек-лист заблокирован для изменений."""


class AnswerValidationError(ChecklistServiceError):
    """Ответ нарушает бизнес-правила пункта чек-листа."""


class ChecklistCompletionError(ChecklistServiceError):
    """Чек-лист пока нельзя завершить."""


class InvalidTemplateVersionStateError(ChecklistServiceError):
    """Версия шаблона находится в неподходящем состоянии."""
