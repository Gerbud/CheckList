# Технический отчёт: этап 4.0

Дата: 18 июля 2026 года.

## Исходное состояние и связь с предыдущим этапом

Работа продолжает этап 3.9, описанный в
`CODEX_REPORT_STAGE_3_9_DIRECTOR_SYSTEM_ADMIN.md`. Перед изменениями в проекте
уже существовали общий store context, `resolve_managed_store(request)`,
директорские функции для system admin, web-задачи `StoreAdHocTask`, Telegram
webhook, входящая и исходящая очереди, polling fallback и общие breadcrumbs.

Фактической последней миграцией была `0016_telegram_webhook_and_inbound_queue`.
Миграции `0001`–`0016` не изменялись. Исторический отчёт 3.9 не
перезаписывался в рамках этапа 4.0; создан отдельный отчёт и общий индекс.

## Синхронные Telegram-ответы

Update классифицируется централизованно в
`checklists/telegram_update_processor.py` как `synchronous`, `background` или
`ignored`. Бизнес-обработчик не зависит от `HttpRequest` и формирует
структурированные идемпотентные `TelegramAction`.

Синхронно отвечают:

- `/start`, `/menu`, `/help`, `/task`, `/tasks`, `/cancel`;
- callback InlineKeyboard;
- выбор даты и этапа;
- ввод даты, текста и описания;
- изменение шага, отмена и возврат в меню;
- подтверждение одной задачи;
- фильтры краткого списка задач.

Webhook больше не отправляет отдельное «Принято». Для каждого реального
действия выполняется одна попытка через `tauto.gerbud.ru` с таймаутом не более
двух секунд. При временной транспортной ошибке реальный payload помещается в
`TelegramOutboundMessage` с уникальным fallback-ключом. Ошибки Telegram 4xx
считаются неповторяемыми для webhook; безопасная диагностика содержит HTTP
status, `error_code` и `description`, но не token и не URL с token.

`answerCallbackQuery` формируется первым действием. Дубликат `update_id`
возвращает HTTP 200, но не выполняет бизнес-логику и доставку повторно.
Повторное подтверждение не создаёт вторую задачу. Синхронные update не создают
`TelegramInboundJob` и не исполняются worker повторно.

Background-классификация сохранена для тяжёлых отчётов (`/report`, `/reports`,
`/analytics`), channel posts, будущих массовых операций, внешних интеграций и
повторной обработки временных ошибок. Их продолжает обрабатывать
`process_telegram_inbound_queue`. Polling использует тот же общий processor.

## Меню бота и команды

Добавлено главное меню:

- «➕ Поставить задачу»;
- «📋 Задачи магазина»;
- «⚠️ Проблемные задачи»;
- «❓ Помощь».

На шагах диалога доступны «Назад», «Отмена» и «Главное меню». После создания
показываются дата, этап и текст, а также действия «Создать ещё», «Задачи
магазина» и «Главное меню».

`/tasks` показывает русские статусы, этап, дату, нормальный текст, проблемный
комментарий, завершившего сотрудника и ссылку на web-карточку. Доступны
фильтры «Сегодня», «Завтра», «Активные», «Проблемные» и «Все».

Без активной binding `/start` показывает Telegram ID и одноразовый код, не
раскрывая магазин. Активная binding показывает название доступного магазина.
Неактивные binding и Store не получают магазинные данные.

Добавлена команда:

```text
python manage.py register_telegram_commands
```

Она вызывает `setMyCommands`; проверка использует `getMyCommands`. В
system-admin интерфейсе доступны кнопки регистрации/проверки, дата последней
операции и безопасная ошибка.

## Аналитика и Store Health

Добавлен отдельный `checklists/reporting_v2.py`. Вычисления не выполняются в
templates и не сохраняются как редактируемые поля базы.

Store Health:

- `Критично` — обязательный вопрос без ответа или незавершённый этап после
  дедлайна;
- `Требует внимания` — failed, поздний этап, revision, просроченная/failed
  задача, отсутствие действий назначенного сотрудника или Telegram-ошибка;
- `Нормально` — перечисленные нарушения отсутствуют.

На главной аналитики причины выводятся конкретными количествами. Метрики имеют
текстовый статус и сравнение с предыдущим равным периодом. При нулевой базе не
показывается вводящий в заблуждение процент.

Проблемы сотрудников:

- назначенная смена без участия;
- ответы «Не выполнено»;
- пропуски обязательных действий;
- невыполненные и просроченные задачи;
- revisions;
- действия после дедлайна.

Статус сотрудника вычисляется как «Без проблем», «Требует внимания» или
«Критичная ситуация», рядом отображаются причины.

## Новые и переработанные страницы

- `/director/reports/` — Store Health, «Что требует внимания», KPI, сравнение
  и динамика;
- `/director/reports/daily/` — три этапа, сроки, ответы, участники, задачи и
  уведомления;
- `/director/reports/employees/` — сортируемая аналитика сотрудников;
- `/director/reports/employees/<id>/` — карточка сотрудника;
- `/director/reports/revisions/` — сводка переходов и изменения после
  дедлайна;
- `/director/reports/tasks/` — срезы и показатели задач;
- `/director/reports/problems/` — единый drill-down;
- `/director/reports/recurring/` — повторяющиеся вопросы, задачи, этапы и дни
  недели;
- `/system-admin/reports/` — системная сводка активных магазинов.

System admin видит директорские отчёты только выбранного сервером Store.
Директор видит только собственный Store. Store ID не принимается из GET.
Чужой employee ID возвращает нейтральный 404.

Фильтры используют GET и сохраняются в URL. Некорректные даты не вызывают
500; период свыше 366 дней ограничивается. Таблицы имеют безопасный
mobile-scroll, KPI и проблемы представлены карточками, график имеет
fallback-таблицу.

CSV с UTF-8 BOM реализован для:

- ежедневного отчёта;
- сотрудников;
- задач;
- revisions;
- проблем и повторяющихся проблем.

Экспорт учитывает текущий Store, период и применимые фильтры.

## Изменённые и новые файлы этапа 4.0

Telegram:

- `checklists/telegram_actions.py`;
- `checklists/telegram_update_processor.py`;
- `checklists/telegram_bot.py`;
- `checklists/telegram_webhook.py`;
- `checklists/telegram_client.py`;
- `checklists/telegram_commands.py`;
- `checklists/telegram_views.py`;
- `checklists/management/commands/register_telegram_commands.py`;
- `templates/checklists/telegram/settings.html`.

Отчёты:

- `checklists/reporting_v2.py`;
- `checklists/portal_views.py`;
- `checklists/portal_context.py`;
- `checklists/urls.py`;
- `templates/base.html`;
- templates `reports_index`, `report_daily`, `report_employees`,
  `report_employee_detail`, `report_revisions`, `report_tasks`,
  `report_problems`, `report_recurring`;
- `templates/checklists/system_admin/reports.html`;
- общий period-filter template и существующие breadcrumbs/navigation.

Тесты и документация:

- `checklists/test_telegram_integration.py`;
- `checklists/test_reports_v2.py`;
- `README.md`;
- `PLAN.md`;
- `CODEX_REPORT_INDEX.md`;
- этот отчёт.

## Миграция

Создана и применена
`0017_telegram_bot_commands.py`. Она добавляет в singleton
`TelegramSystemSettings`:

- `bot_commands_registered_at`;
- `bot_commands_last_checked_at`;
- `bot_commands_last_error`.

Миграция не выполняет сетевых запросов, сохраняет существующие данные и
использует переносимые nullable `DateTimeField`/`TextField` для SQLite и
MySQL 8.0.

## Тесты

Добавлено 10 новых Telegram-сценариев и 29 тестов аналитики v2. Общее
количество тестов: 304.

Проверены немедленные ответы, отсутствие «Принято», callback ack, все шаги
задачи, дедупликация, реальный fallback payload, background job,
set/getMyCommands, роли, Store scope, три состояния Store Health, проблемы,
карточка сотрудника, повторяемость, filters, CSV BOM, breadcrumbs,
mobile-markers, пустые состояния и ограничение запросов.

## Результаты команд

```text
python manage.py makemigrations
No changes detected

python manage.py migrate
Applying checklists.0017_telegram_bot_commands... OK

python manage.py makemigrations --check --dry-run
No changes detected

python manage.py check
System check identified no issues (0 silenced).

pytest -q checklists/test_telegram_integration.py
69 passed, 1 warning

pytest -q checklists/test_reports_v2.py
29 passed, 1 warning

pytest -q checklists/test_portals.py
63 passed, 1 warning

pytest -q
304 passed, 1 warning in 52.27s

pip check
No broken requirements found.

git diff --check
ошибок нет
```

Единственное предупреждение — transitional warning Django о схеме
`forms.URLField` в Django 6.0; на Django 5.2 и результаты тестов не влияет.

## Известные ограничения

- Реальный HTTP Telegram не вызывался: все интеграционные запросы mock.
- Миграция автоматически проверена на SQLite; production-подобный MySQL 8.0
  в этом прогоне не запускался.
- Календарь рабочих/выходных дней магазина ещё отсутствует, поэтому отдельная
  метрика «рабочий день без чек-листа» требует следующего этапа.
- Группировка похожих задач использует только регистр и пробелы, без NLP.
- Графики намеренно реализованы лёгким CSS и fallback-таблицами; расширенная
  визуализация и краткосрочный кэш агрегатов остаются следующим этапом.
- Среднее время реакции сотрудника подготовлено в структуре отчёта, но требует
  согласованного определения начального события для бизнес-метрики.

## Deployment на Beget

После доставки кода:

1. активировать production virtualenv и выполнить `pip install -r requirements.txt`;
2. выполнить `python manage.py migrate`;
3. выполнить `python manage.py register_telegram_commands`;
4. в system-admin проверить bot commands и заново проверить/зарегистрировать
   webhook;
5. убедиться, что `SITE_URL`, token и webhook secret заданы безопасно;
6. сохранить cron без изменений:
   `process_telegram_inbound_queue`, `schedule_telegram_notifications`,
   `process_telegram_queue`;
7. выполнить `python manage.py check` и smoke-test `/start`, `/task`, `/tasks`;
8. проверить права записи логов и работу Gunicorn.

Обычные команды после deployment не зависят от cron. Cron доставляет
fallback, уведомления и background jobs.

## Следующий рекомендуемый этап

Проверить миграции и конкурентные очереди на MySQL 8.0, добавить календарь
рабочих дней, production monitoring cron/webhook, кэш краткой аналитической
сводки и CI. После стабилизации метрик можно расширить SVG-графики и определить
единую бизнес-точку начала для среднего времени реакции.

## Git diff --stat

```text
33 files changed, 5097 insertions(+), 208 deletions(-)
```

Обычный `git diff --stat` не включает untracked-файлы этапов 3.9–4.0, в том
числе миграции, новые сервисы, templates, тесты и этот отчёт. Они перечислены
ниже.

## Git status --short

```text
 M .env.example
 M CODEX_REPORT_STAGE_3_9_DIRECTOR_SYSTEM_ADMIN.md
 M PLAN.md
 M README.md
 M checklists/access_control.py
 M checklists/admin.py
 M checklists/apps.py
 M checklists/forms.py
 M checklists/management_services.py
 M checklists/models.py
 M checklists/notifications.py
 M checklists/portal_forms.py
 M checklists/portal_views.py
 M checklists/reporting.py
 M checklists/services.py
 M checklists/test_portals.py
 M checklists/test_web.py
 M checklists/urls.py
 M checklists/views.py
 M config/__init__.py
 M config/settings.py
 M templates/base.html
 M templates/checklists/_answer_sections.html
 M templates/checklists/daily_checklist.html
 M templates/checklists/director/checklist_detail.html
 M templates/checklists/director/questions.html
 M templates/checklists/director/report_daily.html
 M templates/checklists/director/report_employees.html
 M templates/checklists/director/report_revisions.html
 M templates/checklists/director/reports_index.html
 M templates/checklists/system_admin/audit.html
 M templates/checklists/system_admin/dashboard.html
 M templates/checklists/system_admin/stores.html
?? CODEX_REPORT_INDEX.md
?? CODEX_REPORT_STAGE_4_0_TELEGRAM_SYNC_REPORTS.md
?? checklists/ad_hoc_tasks.py
?? checklists/management/commands/poll_telegram_updates.py
?? checklists/management/commands/process_telegram_inbound_queue.py
?? checklists/management/commands/process_telegram_queue.py
?? checklists/management/commands/register_telegram_commands.py
?? checklists/management/commands/schedule_telegram_notifications.py
?? checklists/management/commands/seed_order_count_questions.py
?? checklists/migrations/0008_alter_auditlog_action.py
?? checklists/migrations/0009_alter_auditlog_action.py
?? checklists/migrations/0010_answerrevision_daily_item_and_more.py
?? checklists/migrations/0011_telegrampendingbinding_telegramupdatelog_and_more.py
?? checklists/migrations/0012_create_default_telegram_templates.py
?? checklists/migrations/0013_expand_default_telegram_task_templates.py
?? checklists/migrations/0014_redesign_telegram_templates.py
?? checklists/migrations/0015_finalize_telegram_template_redesign.py
?? checklists/migrations/0016_telegram_webhook_and_inbound_queue.py
?? checklists/migrations/0017_telegram_bot_commands.py
?? checklists/portal_context.py
?? checklists/reporting_v2.py
?? checklists/signals.py
?? checklists/static/checklists/telegram_template_editor.js
?? checklists/telegram_actions.py
?? checklists/telegram_bot.py
?? checklists/telegram_client.py
?? checklists/telegram_commands.py
?? checklists/telegram_events.py
?? checklists/telegram_inbound.py
?? checklists/telegram_queue.py
?? checklists/telegram_reminders.py
?? checklists/telegram_services.py
?? checklists/telegram_templates.py
?? checklists/telegram_update_processor.py
?? checklists/telegram_views.py
?? checklists/telegram_webhook.py
?? checklists/test_integer_answers.py
?? checklists/test_reports_v2.py
?? checklists/test_telegram_integration.py
?? templates/checklists/_breadcrumbs.html
?? templates/checklists/_portal_navigation.html
?? templates/checklists/director/_report_period_filter.html
?? templates/checklists/director/question_confirm_delete.html
?? templates/checklists/director/report_employee_detail.html
?? templates/checklists/director/report_problems.html
?? templates/checklists/director/report_recurring.html
?? templates/checklists/director/report_tasks.html
?? templates/checklists/director/task_detail.html
?? templates/checklists/director/task_form.html
?? templates/checklists/director/tasks.html
?? templates/checklists/system_admin/audit_confirm_clear.html
?? templates/checklists/system_admin/reports.html
?? templates/checklists/system_admin/store_confirm_delete.html
?? templates/checklists/telegram/
```

Рабочее дерево намеренно не закоммичено.
