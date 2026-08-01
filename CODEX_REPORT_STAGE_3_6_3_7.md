# Технический отчёт: корректировка этапа 3.6 и этап 3.7

Дата выполнения: 16 июля 2026 года.

## Сохранённая реализация этапа 3.6

Существующая незакоммиченная реализация этапа 3.6 не удалялась и не
создавалась заново. Сохранены:

- `DailyChecklistStage` и его жизненный цикл;
- timezone-aware расчёты, граничные состояния и серверные проверки;
- автоматическое завершение `DailyChecklist` после третьего этапа;
- `display_order` на SHA-256 и неизменяемые снимки пунктов;
- mobile-first карточки, маршруты этапов и секундный JavaScript-таймер;
- сервисы `build_stage_schedule`, `get_stage_state`, `get_current_stage`,
  `complete_checklist_stage` и точечное повторное открытие;
- миграция `0003`, которая не переписывалась задним числом.

Файл `store-checklist.env` подтверждён как неотслеживаемый и добавлен в
`.gitignore`. Его содержимое не открывалось, не копировалось и не выводилось.

## Настраиваемое расписание

Добавлена модель `StoreChecklistSchedule` с отношением one-to-one к магазину и
полями:

- `opening_time` — 09:00;
- `morning_deadline` — 11:00;
- `daytime_deadline` — 20:00;
- `closing_deadline` — 22:00;
- `warning_minutes_before` — 30;
- `notifications_enabled`, `is_active` и timestamps.

Фактическое расписание новых дневных чек-листов:

| Этап | Открытие | Дедлайн |
|---|---:|---:|
| Утренние задачи | 09:00 | 11:00 |
| Дневные задачи | 11:00 | 20:00 |
| Вечерние задачи | 20:00 | 22:00 |

Времена берутся из записи магазина и рассчитываются в `Store.timezone`.
`Model.clean()` проверяет строгий порядок времён, положительное предупреждение
и то, что предупреждение не длиннее самого короткого этапа. Безопасное для
SQLite и MySQL 8.0 ограничение БД дополнительно требует
`warning_minutes_before > 0`. `build_stage_schedule` повторяет полную проверку
перед созданием этапов. Admin использует ModelForm и показывает русские поля с
подсказкой о применении изменений только к новым чек-листам.

Существующие `DailyChecklistStage.opens_at/deadline_at` не меняются. Миграция
`0004` создаёт только настройки расписания для существующих магазинов. Команда
`seed_checklist` идемпотентно создаёт расписание 09:00/11:00/20:00/22:00 для
«5 Планет», но не перезаписывает изменения администратора.

Порог жёлтого состояния теперь передаётся в интерфейс как вычисленный
`warning_at`. В views, шаблонах и JavaScript больше нет жёстких 30 минут.

## Модели Telegram-уведомлений

### StoreNotificationSettings

One-to-one настройка магазина содержит `telegram_chat_id`, отдельные флаги
`warning_enabled`, `overdue_enabled`, `completed_late_enabled`, общий
`is_active` и timestamps. Для активной настройки chat ID обязателен.
`warning_minutes_before` не дублируется и берётся только из
`StoreChecklistSchedule`.

### ChecklistNotification

Очередь уведомлений содержит:

- ссылку на `DailyChecklistStage`;
- тип `deadline_warning`, `overdue` или `completed_late`;
- `scheduled_for`;
- статус `pending`, `sending`, `sent` или `failed`;
- число попыток, время начала отправки и время успешной отправки;
- nullable Telegram message ID и безопасный текст последней ошибки;
- timestamps.

Добавлены индексы по статусу, плановому времени и паре «этап + тип». Обычное
уникальное ограничение на «этап + тип» защищает от дублей и переносимо между
SQLite и MySQL 8.0.

## Планирование и защита от дублей

Реализованы функции:

- `schedule_stage_notifications`;
- `schedule_due_notifications`;
- `process_due_notifications`;
- `claim_notification`;
- `send_notification`;
- `create_completed_late_notification`.

Предупреждение планируется на `deadline_at - warning_minutes_before`,
просрочка — на дедлайн, позднее завершение — на фактическое `completed_at`.
Типы учитывают глобальный флаг, активность расписания, общий флаг уведомлений и
индивидуальные настройки магазина. Завершённые вовремя этапы не получают
warning/overdue при обработке, а позднее завершение создаётся ровно один раз.

`claim_notification` выполняет `transaction.atomic` и `select_for_update`,
переводит `pending`/`failed` в `sending`, увеличивает attempts и фиксирует
`sending_started_at`. Запись `sending` старше 10 минут считается stale и может
быть захвачена повторно. Уникальное ограничение и атомарный claim предотвращают
двойную отправку двумя конкурентными обработчиками.

Транзакция захвата завершается до сетевого запроса. Результат HTTP сохраняется
отдельной транзакцией с повторной блокировкой записи.

## Telegram-клиент и безопасность

Клиент реализован без Telegram framework, через стандартный `urllib`, и
отправляет `sendMessage` с HTML parse mode, отключённым preview и настраиваемым
timeout. Динамические названия магазина, сотрудника и этапа HTML-экранируются.
Время сообщений переводится в `Store.timezone`.

Обрабатываются timeout, HTTP error, сетевые ошибки, `ok=false`, невалидный JSON
и отсутствие message ID. Исключения преобразуются в безопасные сообщения без
URL запроса. Секретное значение Telegram хранится только в окружении и не
попадает в admin, `ChecklistNotification`, traceback приложения, AuditLog или
отчёт.

В AuditLog добавлены действия `telegram_notification_sent` и
`telegram_notification_failed`. Chat ID сохраняется только в маскированном
виде; message ID сохраняется в записи очереди.

## Admin и management-команда

В admin добавлены `StoreChecklistSchedule`, `StoreNotificationSettings` и
`ChecklistNotification`. Для очереди показываются магазин, сотрудник, этап,
тип, плановое время, статус, attempts и sent_at. Поля очереди доступны только
для чтения; action «Повторить неудачные уведомления» переводит только
`failed → pending`.

Команда:

```text
python manage.py send_checklist_notifications [--dry-run] [--at ISO_DATETIME] [--limit N]
```

выводит `created`, `sent`, `skipped`, `failed`. `--dry-run` не создаёт записи,
не меняет статусы и не выполняет HTTP, а показывает masked-описания сообщений.

Пример cron добавлен в README без реальных путей:

```cron
*/5 * * * * cd /path/to/project && /path/to/.venv/bin/python manage.py send_checklist_notifications >> /path/to/logs/telegram-notifications.log 2>&1
```

## Миграции

- `0003_dailycheckliststage_alter_dailychecklistitem_options_and_more` —
  исходная миграция этапа 3.6, не изменялась в рамках текущего задания;
- `0004_storechecklistschedule` — модель расписания и начальные настройки
  существующих магазинов без изменения исторических этапов;
- `0005_alter_auditlog_action_storenotificationsettings_and_more` — настройки
  магазина, очередь Telegram и новые действия аудита.

Все три миграции применены.

## Тесты и проверки

Итоговый набор: **103 теста**, все прошли. Из них 22 сценария относятся к
Telegram. Проверены настраиваемые границы 09:00–22:00, неизменность старых
этапов, новое расписание, валидация, динамический warning, планирование типов,
отсутствие дублей, атомарный claim, stale sending, отсутствие транзакции во
время HTTP, timeout, HTTP error, `ok=false`, invalid JSON, сохранение message
ID, HTML escaping, часовой пояс магазина, глобальные и типовые выключатели,
маскированный аудит и безопасный dry-run. Все HTTP-вызовы в тестах mock.

Финальные результаты:

```text
python manage.py migrate
No migrations to apply.

python manage.py makemigrations --check --dry-run
No changes detected

python manage.py check
System check identified no issues (0 silenced).

pytest -q
103 passed in 12.43s

pip check
No broken requirements found.

python manage.py send_checklist_notifications --dry-run --limit 5
created=0 sent=0 skipped=0 failed=0

git diff --check
Ошибок нет.
```

Предупреждение pip касалось только недоступного пользовательского каталога
кеша; кеш был отключён, проверка зависимостей успешна.

Реальные Telegram-сообщения не отправлялись.

## Ограничения

- Реальный MySQL 8.0 в текущем окружении не запускался; Docker отсутствует.
  Использованы переносимые Django constraints/indexes и расширены тесты
  MySQL-совместимости.
- Доставка имеет семантику at-least-once: если Telegram принял сообщение, но
  ответ потерян из-за timeout, повтор после failed/stale теоретически может
  создать дубль на стороне Telegram.
- Нужен внешний cron; фонового worker-процесса в проекте нет.
- Автоматический exponential backoff и ограничение числа попыток пока не
  реализованы; нагрузка ограничивается параметром `--limit`.
- Интерфейс руководителя для уведомлений не создавался; настройки доступны в
  стандартном Django admin.

## Git diff --stat

Стандартный `git diff --stat` не включает новые неотслеживаемые файлы:

```text
 .env.example                                     |   5 +
 .gitignore                                       |   1 +
 PLAN.md                                          |   5 +
 README.md                                        |  68 +++-
 checklists/admin.py                              | 117 +++++++
 checklists/management/commands/seed_checklist.py |  16 +
 checklists/models.py                             | 334 +++++++++++++++++-
 checklists/services.py                           | 413 +++++++++++++++++++++--
 checklists/test_mysql_compat.py                  |  14 +
 checklists/test_web.py                           |  74 ++--
 checklists/tests.py                              |  37 +-
 checklists/urls.py                               |  18 +
 checklists/views.py                              | 187 ++++++++--
 config/settings.py                               |  11 +
 templates/base.html                              |   1 +
 templates/checklists/daily_checklist.html        |  27 +-
 templates/checklists/dashboard.html              |  30 +-
 17 files changed, 1267 insertions(+), 91 deletions(-)
```

## Git status

```text
 M .env.example
 M .gitignore
 M PLAN.md
 M README.md
 M checklists/admin.py
 M checklists/management/commands/seed_checklist.py
 M checklists/models.py
 M checklists/services.py
 M checklists/test_mysql_compat.py
 M checklists/test_web.py
 M checklists/tests.py
 M checklists/urls.py
 M checklists/views.py
 M config/settings.py
 M templates/base.html
 M templates/checklists/daily_checklist.html
 M templates/checklists/dashboard.html
?? CODEX_REPORT_STAGE_3_6_3_7.md
?? CODEX_REPORT_STAGE_3_6_TIME_STAGES.md
?? checklists/management/commands/send_checklist_notifications.py
?? checklists/migrations/0003_dailycheckliststage_alter_dailychecklistitem_options_and_more.py
?? checklists/migrations/0004_storechecklistschedule.py
?? checklists/migrations/0005_alter_auditlog_action_storenotificationsettings_and_more.py
?? checklists/notifications.py
?? checklists/static/
?? checklists/test_notifications.py
?? checklists/test_time_stages.py
```

`store-checklist.env` отсутствует в status, потому что игнорируется точным
правилом `.gitignore`. Git-коммит не создавался.
