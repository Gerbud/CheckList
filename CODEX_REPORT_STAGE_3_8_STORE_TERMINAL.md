# Технический отчёт: этап 3.8

Дата проверки: 16.07.2026. Ветка: `main`. Git-коммит не создавался.

## 1. Новая схема авторизации

- Основной сценарий использует один общий Django User для терминала магазина.
- Технический аккаунт однозначно отделён от `EmployeeProfile`: один User нельзя назначить обоими типами аккаунта.
- Терминал не получает `staff` или `superuser` права и работает только со своим магазином.
- Перед редактированием выбирается активный `StoreEmployee` своего магазина. Выбор идёт через POST с CSRF, а ID проверяется по базе.
- Выбр хранится в Django session до ручной смены, выхода или завершения этапа.
- Личный PIN, его хэш, проверка, rate limit, формы, env-переменные и тесты не реализованы в соответствии с уточнением задачи.
- Существующие индивидуальные `EmployeeProfile` продолжают работать без выбора физического сотрудника.

## 2. Модели и данные

### `StoreTerminalAccount`

One-to-one связи с `Store` и Django User, флаг активности и timestamps. Валидация запрещает admin/staff и совмещение с индивидуальным профилем.

### `StoreEmployee`

Поля: `store`, `first_name`, `last_name`, `display_name`, nullable `personnel_number`, `is_active`, `sort_order`, `created_at`, `updated_at`. Табельный номер уникален внутри магазина, если задан. Сотрудника с историей участия нельзя удалять через модель; используется `is_active=False`. Admin физическое удаление запрещает.

### `DailyShiftAssignment`

Хранит магазин, сотрудника, дату, признак ответственного, границы смены, комментарий, автора и timestamps. Обычное unique-ограничение запрещает дубль `store + employee + work_date`. Неактивного или чужого сотрудника назначить нельзя. В admin есть action копирования состава на следующий день.

### Привязка действий

- `DailyChecklist` поддерживает ровно один тип владельца: `employee` или `terminal_account`.
- `ChecklistAnswer` хранит `answered_by_employee` и `last_edited_by_employee`; исходный `answered_by` User сохранён.
- `DailyChecklistStage` хранит фактического завершившего и последнего редактора, а также первого завершившего, первое время завершения и счётчик reopen.
- `AuditLog` хранит и технического `actor`, и nullable фактического `employee`.

## 3. Контролируемое изменение ответов

- Первый фактический ответ не требует причину изменения.
- Изменение статуса, комментария, удаление комментария или смена фактического редактора уже сохранённого ответа требует отдельную причину минимум 5 символов.
- Для `failed` обычный комментарий остаётся обязательным и не подменяется причиной изменения.
- `update_answer` работает в `transaction.atomic`, блокирует дневной чек-лист, этап, ответ и фактического сотрудника через `select_for_update`.
- `AnswerRevision` сохраняет старые/новые статусы и комментарии, причину, User, StoreEmployee, время, nullable IP и User-Agent. Обычные `save` и `delete` после создания запрещены; admin — только для чтения.
- Одновременно с revision создаётся отдельная запись `AuditLog`.
- Завершённый этап нельзя менять до manager/administrator reopen. После reopen первый исполнитель и время не теряются, а текущий исполнитель обновляется.

## 4. Интерфейс и отчётность

- Добавлен mobile-first экран выбора крупными карточками, именем и признаком назначения на смену.
- На dashboard и этапе видно «Сейчас заполняет: …» и POST-кнопка «Сменить сотрудника».
- После исправления видны отметка «Ответ изменён», последняя безопасная сводка и раскрываемая история. IP, User-Agent и технический аудит там не показываются.
- Подготовлены `get_employee_stage_participation`, `get_missing_employee_actions`, `get_shift_completion_report`. Они сравнивают назначения с первыми ответами, revisions и завершениями этапов и возвращают участие по трём этапам, число первых/изменённых ответов, завершённые этапы и отсутствие участия.

## 5. Команда начальных данных

`python manage.py seed_store_terminal` идемпотентно создаёт непривилегированного User и `StoreTerminalAccount` для магазина `5-planets`. Логин и пароль берутся только из `STORE_TERMINAL_USERNAME` и `STORE_TERMINAL_PASSWORD`; пароль проходит Django validators. `--with-demo-employees` разрешён только при `DEBUG=True`.

## 6. Миграция данных

Создана `checklists/migrations/0006_dailycheckliststage_first_completed_at_and_more.py`:

- создаёт новые модели, связи и обычные MySQL-совместимые unique/check constraints;
- для каждого существующего `EmployeeProfile` создаёт `StoreEmployee`;
- связывает исторические ответы и AuditLog по User, где связь однозначна;
- связывает завершившего этап только по имеющейся аудит-записи;
- не изобретает фактического сотрудника, если его нельзя однозначно восстановить.

Первый запуск data migration выявил, что historical Django User не содержит model method `get_full_name`. Миграция была исправлена: display name формируется напрямую из полей `first_name`, `last_name`, `username`. Неудачная транзакция SQLite откатилась, повторный запуск завершился успешно. Отдельный migration-тест подтверждает сохранение статуса, комментария и связей ответа.

## 7. Файлы этапа 3.8

Созданы:

- `checklists/migrations/0006_dailycheckliststage_first_completed_at_and_more.py`;
- `checklists/management/commands/seed_store_terminal.py`;
- `checklists/test_store_terminal.py`;
- `templates/checklists/select_employee.html`;
- `CODEX_REPORT_STAGE_3_8_STORE_TERMINAL.md`.

Изменены для этапа:

- `checklists/models.py`, `services.py`, `views.py`, `forms.py`, `urls.py`, `admin.py`;
- `checklists/notifications.py`, `checklists/test_time_stages.py`, `checklists/test_mysql_compat.py`;
- `templates/checklists/daily_checklist.html`, `_answer_sections.html`, `dashboard.html`;
- `.env.example`, `README.md`, `PLAN.md`.

Репозиторий до этапа 3.8 уже содержал незакоммиченные изменения этапов 3.6–3.7; они не откатывались и входят в общий `git diff --stat` ниже.

## 8. Тесты и проверки

Добавлен 21 тест терминального сценария. Полный набор вырос с 103 до 124 тесов.

```text
python manage.py makemigrations
No changes detected

python manage.py migrate
Operations to perform:
  Apply all migrations: admin, auth, checklists, contenttypes, sessions
Running migrations:
  No migrations to apply.

python manage.py makemigrations --check --dry-run
No changes detected

python manage.py check
System check identified no issues (0 silenced).

pytest -q
124 passed in 16.33s

pip check
No broken requirements found.

git diff --check
ошибок форматирования diff нет
```

Проверены: изоляция магазинов, активность сотрудника, session и смена выбора, POST/CSRF, подмена ID, привязка ответа/этапа/аудита, разные исполнители одного этапа, revision и её неизменяемость, безопасное HTML-отображение, reopen, смены, отчёты, старые индивидуальные аккаунты, seed-команда и data migration.

MySQL 8.0: новые unique-ограничения не имеют условий, индексируемые строки короткие, nullable-поля и миграция используют переносимые Django operations. Автоматические тесты выполнены на SQLite; реальный MySQL-сервер в этом прогоне не использовался.

## 9. Ограничения и следующий этап

- Без личного подтверждения система доверяет выбору сотрудника на физически защищённом терминале. Это осознанное ограничение уточнённой схемы.
- Полноценный кабинет руководителя не создавался; доступны admin и сервисные функции отчёта.
- Изоляция транзакций и блокировки `select_for_update` должны быть дополнительно проверены параллельными запросами на реальном MySQL 8.0.
- Следующий этап: создать кабинет руководителя на базе подготовленных отчётов, добавить UI повторного открытия этапа с причиной, затем прогнать весь suite в `docker-compose.mysql.yml`.

## 10. `git diff --stat`

```text
 .env.example                                     |   7 +
 .gitignore                                       |   1 +
 PLAN.md                                          |   9 +-
 README.md                                        | 100 ++-
 checklists/admin.py                              | 236 ++++++++
 checklists/forms.py                              |  26 +-
 checklists/management/commands/seed_checklist.py |  16 +
 checklists/models.py                             | 667 +++++++++++++++++++-
 checklists/services.py                           | 735 +++++++++++++++++++++--
 checklists/test_mysql_compat.py                  |  49 ++
 checklists/test_web.py                           |  74 ++-
 checklists/tests.py                              |  37 +-
 checklists/urls.py                               |  28 +
 checklists/views.py                              | 381 ++++++++++--
 config/settings.py                               |  11 +
 templates/base.html                              |   1 +
 templates/checklists/_answer_sections.html       |  29 +
 templates/checklists/daily_checklist.html        |  42 +-
 templates/checklists/dashboard.html              |  56 +-
 19 files changed, 2371 insertions(+), 134 deletions(-)
```

Неотслеживаемые файлы, включая новые миграции и тесты, штатный `git diff --stat` не считает.

## 11. `git status --short`

```text
 M .env.example
 M .gitignore
 M PLAN.md
 M README.md
 M checklists/admin.py
 M checklists/forms.py
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
 M templates/checklists/_answer_sections.html
 M templates/checklists/daily_checklist.html
 M templates/checklists/dashboard.html
?? CODEX_REPORT_STAGE_3_6_3_7.md
?? CODEX_REPORT_STAGE_3_6_TIME_STAGES.md
?? CODEX_REPORT_STAGE_3_8_STORE_TERMINAL.md
?? checklists/management/commands/seed_store_terminal.py
?? checklists/management/commands/send_checklist_notifications.py
?? checklists/migrations/0003_dailycheckliststage_alter_dailychecklistitem_options_and_more.py
?? checklists/migrations/0004_storechecklistschedule.py
?? checklists/migrations/0005_alter_auditlog_action_storenotificationsettings_and_more.py
?? checklists/migrations/0006_dailycheckliststage_first_completed_at_and_more.py
?? checklists/notifications.py
?? checklists/static/
?? checklists/test_notifications.py
?? checklists/test_store_terminal.py
?? checklists/test_time_stages.py
?? templates/checklists/select_employee.html
```
