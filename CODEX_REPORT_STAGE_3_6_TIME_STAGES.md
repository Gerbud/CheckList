# Технический отчёт: этап 3.6

Дата выполнения: 16 июля 2026 года.

## Изменения моделей

- Добавлена модель `DailyChecklistStage` с этапами `opening`, `during_day`,
  `closing`, состояниями `locked`, `available`, `overdue`, `completed`,
  `completed_late` и временными полями `opens_at`, `deadline_at`,
  `completed_at`.
- Добавлено обычное уникальное ограничение для пары
  `daily_checklist + section_code`.
- В `DailyChecklistItem` добавлено неизменяемое поле `display_order` типа
  `PositiveBigIntegerField`; исходный `item_sort_order` сохранён.
- Пользовательская сортировка снимков изменена на
  `section_sort_order, display_order, id`; добавлен составной индекс по
  `daily_checklist, section_code, display_order`.
- В `AuditLog.Action` добавлено действие `checklist_stage_completed`.
- В admin этапы доступны только для просмотра; для снимков показываются и
  случайный `display_order`, и исходный `item_sort_order`.

## Расписание и правила времени

Все границы строятся как timezone-aware значения в `Store.timezone`, после
чего Django хранит их с учётом `USE_TZ=True`:

| Этап | Открытие | Дедлайн |
|---|---:|---:|
| Утренние задачи (`opening`) | 00:00 | 11:00 |
| Дневные задачи (`during_day`) | 11:00 | 20:00 |
| Вечерние задачи (`closing`) | 20:00 | 00:00 следующего дня |

Ровно в 11:00 утренний этап уже `overdue`, а дневной `available`; ровно в
20:00 аналогично переключаются дневной и вечерний; в 00:00 следующего дня
незавершённый вечерний этап уже `overdue`. Заполнение после дедлайна разрешено.
Завершение не позднее дедлайна даёт `completed`, после дедлайна —
`completed_late`.

Фактическое состояние вычисляется при запросе функцией `get_stage_state`; при
изменяющих пользовательских операциях постоянный статус синхронизируется.
Браузерное время не участвует в проверках.

## Случайный порядок пунктов

При создании снимка вычисляется SHA-256 от стабильного секрета,
`employee.id`, даты, `section_code` и идентификатора исходного пункта. Первые
64 бита, ограниченные знаковым диапазоном MySQL `BIGINT`, сохраняются в
`display_order`. Коллизии разрешаются вторичной сортировкой по `id`.

Используется `RANDOMIZATION_SECRET`, а при его отсутствии — `SECRET_KEY`.
Секрет не попадает в HTML или базу. Его необходимо сохранять неизменным после
начала эксплуатации. Порядок старых дневных чек-листов не пересчитывается.

## Сервисный слой

Добавлены `build_stage_schedule`, `get_stage_state`, `get_current_stage` и
`complete_checklist_stage`. Создание дневного чек-листа атомарно создаёт три
этапа и снимки с `display_order`.

Сохранение ответа и завершение этапа повторно проверяют серверное время,
владельца/роль и магазин. Будущий и завершённый этапы блокируются, просроченный
остаётся редактируемым. Изменяющие операции используют `transaction.atomic` и
`select_for_update` в порядке «дневной чек-лист → этапы → ответы».

Аудит завершения этапа содержит `section_code`, `deadline_at`, `completed_at`
и итог `completed`/`completed_late`, а также IP и User-Agent при наличии.
После третьего завершённого этапа `DailyChecklist` закрывается автоматически,
а его `completed_at` равен максимальному известному времени завершения этапа.

`reopen_daily_checklist` принимает необязательный `section_code`: можно открыть
один этап либо все этапы, не изменяя исторические границы времени. Повторное
завершение после дедлайна остаётся поздним.

## URL и интерфейс

- `/checklist/today/` перенаправляет на актуальный этап;
- `/checklist/today/opening/`;
- `/checklist/today/during-day/`;
- `/checklist/today/closing/`.

Dashboard создаёт дневной чек-лист при первом посещении и показывает три
mobile-first карточки со статусом, интервалом, количеством выполненных и
оставшихся пунктов и результатом завершения. Будущие этапы серые, активные
выделены, просроченные красные, завершённые вовремя зелёные, последние 30 минут
до дедлайна — жёлтые.

На dashboard и странице этапа передаются ISO 8601 значения серверного времени,
открытия и дедлайна. Небиблиотечный JavaScript обновляет таймер раз в секунду и
показывает время до открытия, остаток до дедлайна либо длительность просрочки.
Без JavaScript остаются абсолютные серверные даты и время. Прямая загрузка и
POST будущего этапа запрещены; подмена `section_code` в POST также даёт 403;
завершённый этап доступен только для чтения. Кабинет руководителя не добавлялся.

## Миграция существующих данных

Создана миграция
`0003_dailycheckliststage_alter_dailychecklistitem_options_and_more`:

- создаёт три этапа для каждого существующего `DailyChecklist`;
- рассчитывает timezone-aware границы по часовому поясу магазина;
- вычисляет и сохраняет `display_order` существующих снимков;
- не изменяет ответы и существующий аудит;
- для старого полностью завершённого дня помечает три этапа завершёнными и
  использует общий `DailyChecklist.completed_at` как техническое время;
- если старое `completed_at` равно `NULL`, оставляет времена этапов `NULL` и не
  создаёт вымышленные исторические данные.

Миграция использует переносимые типы, обычное уникальное ограничение и индекс,
совместимые с SQLite и MySQL 8.0. Тест миграции подтверждает сохранение статуса
и комментария исторического ответа.

## Тесты и проверки

Итоговый набор: **74 теста**, все прошли. В него входят прежние 41 тест и новые
сценарии расписания, точных границ, блокировок, просрочки, завершения,
автозавершения дня, аудита, повторного открытия, случайного порядка, миграции,
таймеров и подмены раздела. Шесть параметризованных Django test client
сценариев проверяют время до 11:00, ровно 11:00, между 11:00 и 20:00, ровно
20:00, после 20:00 и ровно 00:00 следующего дня.

Результаты финального запуска:

```text
python manage.py migrate
No migrations to apply.

python manage.py makemigrations --check --dry-run
No changes detected

python manage.py check
System check identified no issues (0 silenced).

pytest -q
74 passed in 10.40s

pip check
No broken requirements found.
```

`pip check` дополнительно сообщил только о недоступности пользовательского
каталога кеша pip; кеш был отключён, на целостность зависимостей это не влияет.

## Ограничения

- Реальный MySQL 8.0 в этой среде не запускался: команда `docker` отсутствует.
  Схема и миграция проверены переносимыми Django API и существующими тестами
  MySQL-совместимости, но перед production нужен прогон на MySQL 8.0.
- Временные окна пока фиксированы и не настраиваются отдельно для магазина.
- Для исторического завершённого дня невозможно восстановить времена отдельных
  этапов; используется описанное техническое значение всего дня.
- Для неизвестного часового пояса новая операция создания завершается ошибкой;
  миграция старых данных безопасно использует UTC, чтобы не потерять записи.
- Интерфейс повторного открытия этапа руководителем намеренно не реализован.

## Git diff --stat

Стандартный `git diff --stat` не включает неотслеживаемые новые файлы:

```text
 .env.example                              |   2 +
 PLAN.md                                   |   3 +
 README.md                                 |  25 +-
 checklists/admin.py                       |  25 ++
 checklists/models.py                      |  90 +++++++-
 checklists/services.py                    | 363 +++++++++++++++++++++++++++---
 checklists/test_web.py                    |  74 +++---
 checklists/tests.py                       |  21 +-
 checklists/urls.py                        |  18 ++
 checklists/views.py                       | 183 +++++++++++++--
 config/settings.py                        |   4 +
 templates/base.html                       |   1 +
 templates/checklists/daily_checklist.html |  27 ++-
 templates/checklists/dashboard.html       |  30 ++-
 14 files changed, 775 insertions(+), 91 deletions(-)
```

## Git status

```text
 M .env.example
 M PLAN.md
 M README.md
 M checklists/admin.py
 M checklists/models.py
 M checklists/services.py
 M checklists/test_web.py
 M checklists/tests.py
 M checklists/urls.py
 M checklists/views.py
 M config/settings.py
 M templates/base.html
 M templates/checklists/daily_checklist.html
 M templates/checklists/dashboard.html
?? CODEX_REPORT_STAGE_3_6_TIME_STAGES.md
?? checklists/migrations/0003_dailycheckliststage_alter_dailychecklistitem_options_and_more.py
?? checklists/static/
?? checklists/test_time_stages.py
?? store-checklist.env
```

`store-checklist.env` существовал до этапа 3.6, не открывался и не изменялся.
Git-коммит не создавался.
