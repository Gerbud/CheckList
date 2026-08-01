# Stage 4.1 — Telegram Immediate Response Architecture + Breadcrumbs Fix

## Результат

Telegram-команды и `callback_query` обрабатываются непосредственно в webhook и
не ожидают запуска cron. Фоновые очереди сохранены для отчётов, аналитики,
напоминаний, массовых уведомлений и повторной доставки при временной ошибке API.

## Архитектура Telegram

- `/start`, `/help`, `/menu`, `/tasks`, `/newtask`, `/task`, `/status`,
  `/myid`, `/whoami`, `/cancel` и сценарии управления задачами исполняются
  синхронно.
- `callback_query` обрабатывается синхронно, включая `answerCallbackQuery`.
- Команды `/report`, `/reports`, `/analytics`, channel posts и тяжёлые операции
  остаются в `TelegramInboundJob`.
- Перед немедленной отправкой создаётся идемпотентная
  `TelegramOutboundMessage`. Успешная запись получает `sent`; временная ошибка
  оставляет её в `pending` для существующего cron; постоянная ошибка получает
  `failed`.
- Быстрый запрос делает одну короткую попытку через
  `https://tauto.gerbud.ru`, затем одну короткую попытку через официальный API.
  Обычная очередь сохраняет прежний алгоритм 5 попыток + 5 попыток.
- Токен бота не включается в URL или текст сохранённых ошибок.

## Защита очереди

При запуске `process_telegram_queue` сообщения в `processing`, не обновлявшиеся
более пяти минут, возвращаются в `pending`. В `last_error` сохраняется:

`Message processing timeout, returned to queue`

Возвращённые сообщения не захватываются повторно в том же запуске обработчика и
будут безопасно обработаны следующим запуском.

## Журналирование

`TelegramUpdateLog` дополнен полями:

- `command`;
- `response_status`;
- `responded_at`;
- `response_error`.

В журнале уже сохраняются Telegram user/chat ID, безопасный payload,
`created_at` и `processed_at`. Экран входящих Telegram-сообщений теперь
показывает отправителя, команду или callback, режим, время и результат ответа,
а также безопасную ошибку доставки.

Миграция: `checklists/migrations/0018_telegram_update_response_tracking.py`.

## Команды бота

`setMyCommands` регистрирует:

- `/start`;
- `/help`;
- `/menu`;
- `/tasks`;
- `/newtask`;
- `/status`;
- `/myid`.

## Хлебные крошки и роли

Создан единый helper `get_portal_home_url(user)`:

- system admin → `/system-admin/dashboard/`;
- директор → `/director/dashboard/`;
- терминальный аккаунт магазина → `/terminal/`;
- пользователь без web-роли → безопасная общая стартовая страница без
  перенаправления в кабинет директора.

Общие и Telegram-хлебные крошки используют вычисленный `portal_home_url`.
Прямые ссылки на кабинет директора удалены из навигации. Для терминала добавлен
канонический маршрут `/terminal/`, старый `/checklist/` сохранён для
совместимости.

Существующая модель прав сохранена: system admin выбирает магазин и использует
те же director views и permissions, без дублирования интерфейса.

## Изменённые файлы этапа

- `checklists/access_control.py`
- `checklists/models.py`
- `checklists/portal_context.py`
- `checklists/telegram_bot.py`
- `checklists/telegram_client.py`
- `checklists/telegram_commands.py`
- `checklists/telegram_inbound.py`
- `checklists/telegram_queue.py`
- `checklists/telegram_views.py`
- `checklists/telegram_webhook.py`
- `checklists/management/commands/process_telegram_queue.py`
- `checklists/migrations/0018_telegram_update_response_tracking.py`
- `checklists/urls.py`
- `templates/checklists/_portal_navigation.html`
- `templates/checklists/telegram/queue.html`
- `checklists/test_telegram_integration.py`
- `checklists/test_portals.py`
- `checklists/test_web.py`

## Команды после обновления

```bash
source .venv/bin/activate
python manage.py migrate
python manage.py register_telegram_commands
python manage.py check
pytest -q
```

На Beget после обновления файлов также требуется перезапуск Passenger:

```bash
touch /home/a/autobud/checklist/public_html/tmp/restart.txt
```

Существующие cron-команды нужно сохранить:

```text
process_telegram_inbound_queue
schedule_telegram_notifications
process_telegram_queue
```

Обычные команды и inline-кнопки от cron больше не зависят.

## Результаты локальной проверки

- миграция `0018_telegram_update_response_tracking` — применена успешно;
- `python manage.py makemigrations --check --dry-run` — изменений нет;
- `python manage.py check` — ошибок нет;
- `pytest -q` — `311 passed`;
- `pip check` — конфликтов зависимостей нет;
- `git diff --check` — ошибок форматирования diff нет.

Остаётся предупреждение Django о будущем изменении схемы по умолчанию для
`forms.URLField` в Django 6.0; на работу Django 5.2 оно не влияет.
