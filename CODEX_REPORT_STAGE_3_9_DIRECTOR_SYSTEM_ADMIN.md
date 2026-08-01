# Технический отчёт: этап 3.9

Дата проверки: 16 июля 2026 года.

## Исходное состояние

- Django 5.2, локальная SQLite и ранее подготовленная конфигурация MySQL 8.0 сохранены без изменений.
- До этапа 3.9 в проекте были реализованы временные этапы чек-листа, Telegram-уведомления, общий терминал без PIN, `StoreTerminalAccount`, `StoreEmployee`, смены, ревизии ответов и аудит.
- Применённые миграции `0001`–`0006` не изменялись.
- Рабочее дерево перед этапом уже содержало незакоммиченные изменения этапов 3.6–3.8; они сохранены.
- До добавления тестов этапа 3.9 проходили 124 теста.

## Архитектура доступа

Существующий `EmployeeProfile` адаптирован как единый профиль доступа. В хранимых значениях и `choices` существуют ровно три роли:

- `store_account` — «Аккаунт магазина»;
- `store_director` — «Директор магазина»;
- `system_admin` — «Администратор системы».

Поля профиля: `user`, nullable `store`, `role`, `is_active`, `created_at`, `updated_at`. Согласованность роли и магазина проверяется моделью и DB `CheckConstraint`. Старые Python-имена ролей оставлены только как временные API-алиасы для кода этапов 2–3.8; они не входят в `choices` и не могут сохраняться как дополнительные бизнес-роли.

`StoreEmployee` остаётся отдельным фактическим исполнителем без `User`, логина и пароля. `StoreTerminalAccount` сохранён для совместимости, но активная запись обязана соответствовать активному профилю `store_account` того же магазина. Операционный лимит одного активного аккаунта обеспечивают сервис с блокировкой магазина/профилей и `OneToOneField` терминала на магазин. Условное уникальное ограничение не добавлялось, поскольку оно не переносимо на MySQL 8.0; прямые изменения ORM вне сервисного слоя считаются техническим обходом.

Единый модуль `checklists/access_control.py` содержит `get_user_role`, `get_user_store`, все требуемые `is_*`/`can_*` функции и decorators. Django superuser признаётся аварийным `system_admin`, но обычная бизнес-логика не зависит от `is_superuser`. Для входа администратора в кабинет конкретного магазина магазин выбирается только POST-запросом и хранится в подписанной Django session.

После входа используются отдельные маршруты:

- аккаунт магазина → `/checklist/`;
- директор → `/director/dashboard/`;
- системный администратор → `/system-admin/dashboard/`.

Внешний `next` не принимается как безопасный redirect.

## Миграция старых ролей

Добавлена миграция `checklists/migrations/0007_checklistitem_description_and_more.py`, совместимая с SQLite и MySQL 8.0. Data migration выполняет:

- `StoreTerminalAccount` → профиль `store_account` того же магазина;
- `manager` с магазином → `store_director`;
- `administrator` и superuser → `system_admin` без магазина;
- неоднозначный старый `employee` → неактивный профиль с `role=NULL`;
- создание недостающего профиля для терминального пользователя и superuser.

Пароли Django User не пересоздаются. Исторические `actor`, ответы, `AuditLog` и `StoreEmployee` сохраняются. Обратная миграция предусмотрена для технического rollback. Отдельный migration-test подтверждает преобразования, неизменность хэшей паролей и исторических записей.

Миграция также добавляет параметры вопросов `description`, `is_required`, `effective_from`, `effective_until`, их snapshot-поля в `DailyChecklistItem`, nullable `AuditLog.store`, новые действия аудита и constraint профиля доступа. Дополнение удаления вопросов оформлено отдельной миграцией `0008_alter_auditlog_action.py`, а безопасное удаление магазинов — миграцией `0009_alter_auditlog_action.py`. Они добавляют action-коды аудита и не изменяют миграции `0001`–`0007`.

## Кабинет директора

Реализованы dashboard и управление данными только выбранного магазина:

- сотрудники: поиск по ФИО/display name/табельному номеру, фильтр активности, пагинация, создание, редактирование, активация и деактивация без физического удаления истории;
- смены: работа по дате, копирование, добавление/изменение/удаление, массовое планирование по диапазону и дням недели, режимы create/update, подсчёт созданных/обновлённых/пропущенных записей;
- вопросы: описание, этап, обязательность, `not_applicable`, комментарий при `failed`, даты действия, активность и серверно проверяемый reorder;
- расписание: изменение через атомарный `update_store_schedule`; существующие `DailyChecklistStage` не меняются;
- Telegram: редактируется только `StoreNotificationSettings`, токен окружения не выводится; тестовая отправка — отдельный подтверждённый POST;
- отчёты: ежедневный, по сотрудникам и по ревизиям, серверные фильтры и пагинация;
- подробный просмотр чек-листа: этапы, дедлайны, опоздания, исполнители, ответы, revisions, Telegram-статусы и безопасная выборка аудита;
- повторное открытие этапа: POST, причина не короче 5 символов, неизменные `opens_at`/`deadline_at`, сохранение первого завершения и аудит причины.

Изменения шаблона выполняются созданием и публикацией новой версии. Snapshot уже созданного ежедневного чек-листа не меняется. Случайный `display_order` сохраняется, а `sort_order` остаётся исходным порядком шаблона.

Dashboard использует существующие сервисы участия, включая `get_shift_completion_report`, `get_employee_stage_participation` и `get_missing_employee_actions`, и показывает этапы, сроки, участие смены, последние revisions и безопасные ошибки Telegram.

## Кабинет администратора системы

Реализован отдельный `/system-admin/`, не заменяющий технический Django admin:

- общесистемный dashboard с состоянием магазинов, аккаунтов, сотрудников, чек-листов, этапов и Telegram;
- создание магазина через атомарный `create_store_with_defaults` с расписанием и выключенными Telegram-настройками;
- опциональное создание аккаунта магазина только при явно переданных логине и валидном пароле;
- редактирование, активация и деактивация магазина без удаления истории;
- карточка магазина с директорами, аккаунтом магазина и состоянием настроек;
- управление пользователями трёх ролей, перевод директора между магазинами, активация/деактивация;
- отдельный сброс пароля через validators и `set_password`, без пароля в AuditLog, с завершением других сессий пользователя;
- системный журнал административных действий с пагинацией и фильтром магазина.

Поле `Store.code` остаётся существующим `SlugField` и используется как slug магазина; параллельное поле не создавалось.

## Сервисный слой и аудит

В `checklists/management_services.py` реализованы требуемые сервисы магазинов, управляемых пользователей, сотрудников, смен, вопросов, расписания, уведомлений и reopen. Изменяющие сервисы используют `transaction.atomic`, проверенный объект магазина, object-level permissions, `select_for_update` в конфликтных сценариях, model validation и `AuditLog`.

HTTP-вызов тестового Telegram-сообщения выполняется вне DB-транзакции. В аудит не попадают пароли, Telegram token, session key и CSRF token. Для глобальных системных действий `AuditLog.store` может быть `NULL`, чтобы не приписывать действие случайному магазину.

Добавлены все запрошенные административные action-коды. Django admin обновлён для новых ролей и параметров вопросов; изменять бизнес-данные через технический admin может только явный superuser. Пароли и Telegram token в нём не отображаются.

## Безопасность

- Все кабинеты требуют аутентификацию и активные согласованные профиль/магазин.
- Все изменяющие portal-операции выполняются POST-запросами с CSRF.
- QuerySet директора сначала ограничивается текущим магазином; чужие ID дают нейтральный 404/403.
- `store_id` не принимается из директорских форм.
- Сотрудники и вопросы другого магазина не могут быть подставлены.
- Формы не позволяют выставлять `is_staff`/`is_superuser`.
- Пароль валидируется стандартными Django validators и сохраняется только как hash.
- Нельзя деактивировать себя или убрать последний административный доступ.
- Секреты окружения, пароли, session key и CSRF token не выводятся в HTML или AuditLog.
- Списки сотрудников, магазинов, пользователей, аудита и отчётов пагинируются.

## Новые URL

Группы маршрутов:

- `/director/dashboard/`, `/director/employees/...`, `/director/shifts/...`, `/director/questions/...`, `/director/schedule/`, `/director/notifications/`, `/director/reports/...`, `/director/checklists/...`;
- `/system-admin/dashboard/`, `/system-admin/stores/...`, `/system-admin/users/...`, `/system-admin/audit/`;
- `/accounts/redirect/` и `/checklist/` для ролевого перенаправления и терминала.

Полный перечень находится в `checklists/urls.py`.

## Файлы этапа

Новые Python-модули и тесты:

- `checklists/access_control.py`;
- `checklists/management_services.py`;
- `checklists/portal_forms.py`;
- `checklists/portal_views.py`;
- `checklists/reporting.py`;
- `checklists/test_portals.py`;
- `checklists/test_access_migration.py`;
- `checklists/migrations/0007_checklistitem_description_and_more.py`.

Новые общие templates:

- `templates/checklists/portal_form.html`;
- `templates/checklists/pagination.html`.

Новые templates директора:

- `dashboard.html`, `employees.html`, `employee_form.html`, `shifts.html`, `bulk_shifts.html`, `questions.html`, `schedule.html`, `notifications.html`;
- `reports_index.html`, `report_daily.html`, `report_employees.html`, `report_revisions.html`, `checklist_detail.html`.

Новые templates системного администратора:

- `dashboard.html`, `stores.html`, `store_detail.html`, `users.html`, `user_detail.html`, `audit.html`.

Изменены модели, admin, формы терминала, сервисы чек-листа, команды seed, URL/settings входа, базовый mobile-first layout, snapshot-вывод и два прежних теста совместимости.

## Удаление вопросов

На `/director/questions/` для каждого текущего вопроса доступны действия «Изменить», «Активировать/Деактивировать» и визуально опасная кнопка «Удалить». На мобильном действия переносятся на отдельную строку. Добавлены:

- URL `/director/questions/<id>/delete/` с именем `director_question_delete`;
- страница `templates/checklists/director/question_confirm_delete.html`;
- сервис `delete_checklist_question` и консервативная проверка истории `get_checklist_question_history`.

GET только показывает подтверждение, свойства вопроса и последствия. Изменение выполняется только POST с CSRF. Вопрос всегда выбирается через queryset текущего магазина; `store_id` из браузера не принимается. Доступ имеют директор своего магазина и system admin после серверного выбора магазина.

Правила удаления:

- неопубликованный вопрос черновика без `DailyChecklistItem`, ответа и иной истории удаляется физически; `sort_order` оставшихся вопросов его раздела нормализуется с единицы;
- вопрос опубликованной или архивной версии всегда считается историческим, даже если по нему ещё не было ответа;
- исторический вопрос физически не удаляется: текущая опубликованная версия клонируется существующим versioning-механизмом, клон вопроса удаляется только из новой версии, порядок нормализуется, затем новая версия публикуется;
- прежняя версия архивируется и остаётся доступна истории; у шаблона остаётся ровно одна текущая опубликованная версия;
- старые `DailyChecklistItem`, `ChecklistAnswer`, `AnswerRevision`, `AuditLog` и snapshot-поля не меняются;
- новые ежедневные чек-листы больше не получают исключённый вопрос;
- повторный POST распознаётся по аудиту и не создаёт лишнюю версию.

Для точной связи с историей используются существующие `DailyChecklistItem.source_item` и ответы через этот snapshot. Все конфликтные объекты — магазин, вопрос, версия и шаблон — блокируются `select_for_update` внутри `transaction.atomic`.

Добавлены AuditLog actions:

- `checklist_question_deleted` с методом `hard_delete`;
- `checklist_question_removed_from_template` с методом `removed_from_new_version`.

Аудит содержит actor, store, исходный ID, текст, section code, исходный `sort_order`, метод удаления и стандартный `created_at`; секретные значения не сохраняются.

Добавлено 6 тестов дополнения: кнопка/GET-подтверждение, физическое удаление и нормализация порядка, versioned-исключение с сохранением snapshot/ответа и проверкой нового чек-листа, чужой магазин, запрет store account, CSRF и идемпотентный повторный POST.

## Удаление магазинов

На `/system-admin/stores/` рядом с каждым магазином добавлены mobile-first действия «Открыть», «Изменить», «Активировать/Деактивировать» и опасная кнопка «Удалить». Новый URL `/system-admin/stores/<id>/delete/` с именем `system_admin_store_delete` открывает `templates/checklists/system_admin/store_confirm_delete.html`.

GET только показывает подготовленный сервисом `StoreDeletionSummary`: активность, директоров, аккаунты магазина, сотрудников, смены, шаблоны и версии, ежедневные чек-листы, ответы, revisions и блокирующие причины. Изменение выполняется только POST с CSRF и доступно исключительно `system_admin`.

Hard delete разрешён только при отсутствии всех бизнес-связей:

- `EmployeeProfile` и `StoreTerminalAccount`;
- `StoreEmployee` и `DailyShiftAssignment`;
- `ChecklistTemplate`, версий, разделов и вопросов;
- `DailyChecklist`, этапов, уведомлений, snapshots и ответов;
- `AnswerRevision`;
- AuditLog, кроме записи `store_created` для пустого магазина;
- изменённого расписания или настроенной Telegram-конфигурации.

Автоматически созданные расписание с исходными значениями и выключенные пустые `StoreNotificationSettings` не блокируют удаление. Перед hard delete они удаляются явно. Запись `store_created` переводится в глобальный аудит с `store=NULL`, затем создаётся глобальная запись `store_deleted` с ID, name, code и `method=hard_delete`; после этого Store удаляется без broken foreign keys.

Если найдена хотя бы одна бизнес-связь, физическое удаление запрещено. Сервис в `transaction.atomic` блокирует Store, профили, пользователей и терминал через `select_for_update`, после чего:

- переводит Store в `is_active=False`;
- деактивирует связанные профили директоров и аккаунта магазина;
- деактивирует соответствующих Django User и StoreTerminalAccount;
- отзывает активные DB-сессии этих пользователей существующим безопасным session-механизмом;
- сохраняет пользователей, сотрудников, смены, шаблоны, чек-листы, ответы, revisions, snapshots и AuditLog без изменений;
- создаёт `store_deactivated_with_history` с агрегированными счётчиками и `method=deactivated_with_history`.

Текущий system admin не может попасть в список отключаемых пользователей; это дополнительно проверяется сервисом. Повторный POST после hard delete или деактивации идемпотентен и не создаёт повторный аудит.

Добавлено 9 pytest-сценариев удаления магазинов: видимость действий и роли, GET без изменения, hard delete с техническими настройками и глобальным аудитом, пять отдельных блокирующих случаев, полная деактивация с отзывом сессий и сохранением ответов/revisions, повторный POST и CSRF.

## Числовые вопросы сотруднику

Реализованы два типа ответа через `ChecklistItem.AnswerType` (`TextChoices`): `status` — прежний статус выполнения, `integer` — целое неотрицательное число. Значение по умолчанию `status` сохраняет совместимость существующих вопросов. Для `integer` серверная модель и директорская форма принудительно очищают `allow_not_applicable` и `comment_required_on_failure`.

`DailyChecklistItem.answer_type_snapshot` фиксирует тип в момент создания ежедневного чек-листа. Изменение типа директором выполняется через существующее клонирование опубликованной версии и публикацию нового draft; старые snapshots не меняются. При создании числового snapshot связанный `ChecklistAnswer` получает `status=NULL`, а статусный — прежний `pending`.

В `ChecklistAnswer` добавлено `integer_value = PositiveIntegerField(null=True, blank=True)`. Статус и число взаимоисключающие, что проверяется сервисом, model validation и DB constraint. Для числа принимается только Python `int` от нуля; `bool`, строка, дробное и отрицательное значение отклоняются. Для статусного вопроса число запрещено. Значение `0` сохраняется как полноценный ответ.

В кабинете сотрудника числовой вопрос показывает описание и поле с `type=number`, `min=0`, `step=1`, `inputmode=numeric`, подписью «Укажите количество» и кнопкой «Сохранить ответ». Статусные кнопки и комментарий для него не выводятся. Сохранённое значение отображается как «Ответ: N». Все POST проходят существующие CSRF, store/daily/item/employee проверки; сервис блокирует daily, stage и answer через `transaction.atomic` и `select_for_update`, не разрешает отвечать до открытия или после закрытия этапа.

Завершение этапа различает тип snapshot: обязательный `status` требует ответ, отличный от `pending`, обязательный `integer` требует `integer_value is not NULL`; ноль считается заполненным. Аналогичный подсчёт используется в dashboard и Telegram-уведомлениях.

`AnswerRevision` расширена полями `daily_item`, `previous_integer_value`, `new_integer_value`; статусные snapshot-поля сделаны nullable для числовой ревизии. При изменении числа сохраняются старое/новое значение, технический actor, фактический `StoreEmployee`, причина и `changed_at`. Прежняя история статусов и комментариев продолжает работать.

Директорская форма получила поле «Тип ответа» с вариантами «Статус выполнения» и «Целое число». В подробном просмотре выводятся фактическое число, первый/последний сотрудник, время ответа и история числовых изменений. Ежедневный отчёт и отчёт ревизий показывают число, не преобразуя его во «Выполнено» или «Не выполнено».

Добавлена идемпотентная команда:

```text
python manage.py seed_order_count_questions --store-code 5
```

Она ищет магазин только по точному рабочему коду, проверяет наличие по `(store, section_code, answer_type, точный text)`, при необходимости один раз клонирует опубликованную версию и публикует новую. Повторный запуск не создаёт дублей; опубликованной остаётся ровно одна версия. Миграции команду автоматически не запускают.

Создаваемые обязательные числовые вопросы с описанием «Введите текущее количество заказов в указанном статусе»:

- начало дня: «Сколько заказов находится в статусе „Готов к отгрузке“?»;
- начало дня: «Сколько заказов находится в статусе „Ожидает поставки“?»;
- конец дня: «Сколько заказов находится в статусе „Готов к отгрузке“?»;
- конец дня: «Сколько заказов находится в статусе „Ожидает поставки“?».

Миграция `0010_answerrevision_daily_item_and_more.py` следует за уже находившейся в рабочем дереве миграцией `0009`; миграции `0001`–`0009` не изменялись. Она добавляет типы, snapshot, числовое значение и поля ревизии, явно заполняет существующие вопросы/snapshots значением `status`, восстанавливает `daily_item` существующих ревизий и затем делает эту связь обязательной. Операции совместимы с SQLite и MySQL 8.0.

Добавлено 18 целевых pytest-сценариев: default/snapshot, HTML-поле и отсутствие статусных кнопок, `0` и положительное число, отрицательное/дробное/строковое/пустое значения, несовместимые payload, блокировка завершения, ревизия, неизменность старого snapshot, директорский просмотр, CSRF, чужой магазин и двойной seed.

Результаты проверок:

```text
python manage.py migrate
Applying checklists.0010_answerrevision_daily_item_and_more... OK

python manage.py makemigrations --check --dry-run
No changes detected

python manage.py check
System check identified no issues (0 silenced).

pytest -q checklists/test_integer_answers.py
18 passed

pytest -q
191 passed in 37.29s

pip check
No broken requirements found.

git diff --check
ошибок нет
```

`git diff --stat` и `git status --short` приведены в итоговых разделах отчёта ниже; untracked-файлы, включая миграцию, команду и новые тесты, по правилам Git не входят в обычный `git diff --stat`, но полностью перечислены в status.

## Тесты

- Новых тестов этапа 3.9 вместе с дополнениями: 67, включая сценарии удаления вопросов, удаления магазинов с очисткой аудита и 18 тестов числовых ответов.
- Общее количество: 191.
- Проверены роли и redirect, границы порталов, store scoping, CSRF, сотрудники, смены, versioned-вопросы, расписание, Telegram mock/секреты, отчёты, reopen, создание магазина, управляемые пользователи, перевод директора, password reset и data migration.
- Все 191 тест проходят. Тест физического удаления пустого магазина синхронизирован с целевым контрактом `hard_delete_with_audit_cleanup`: проверяются metadata удалённого магазина, число удалённых audit-записей, отсутствие прежних store-scoped `AuditLog` и глобальная запись `store_deleted` с `store=NULL`.

## Обнаруженные и исправленные проблемы

- `ShiftAssignmentForm` первоначально валидировала сотрудника до присвоения проверенного магазина instance; магазин теперь задаётся до model validation.
- Новый `User` первоначально проходил `full_clean` до `set_password`; порядок исправлен, валидируется уже хэшированное обязательное поле.
- Тест миграции опубликованной версии требовал `published_at`; fixture исправлена без изменения production-кода.
- При добавлении query-string helper была выявлена регрессия парсинга даты маршрута смен; порядок функций исправлен, тесты повторены.
- Глобальный аудит больше не связывается с первым случайным магазином и использует nullable store.
- После полуночи выявилась зависимость legacy web-тестов от реального московского времени: утренний этап закономерно был ещё закрыт. В fixture зафиксировано рабочее тестовое время; production-логика расписания не изменялась.

## Результаты обязательных проверок

```text
python manage.py makemigrations
No changes detected

python manage.py migrate
No migrations to apply.

python manage.py makemigrations --check --dry-run
No changes detected

python manage.py check
System check identified no issues (0 silenced).

pytest -q
191 passed in 37.29s

pip check
No broken requirements found.

git diff --check
успешно, вывода нет
```

Предупреждение `pip` касалось только недоступного пользовательского cache-каталога и не является ошибкой зависимостей.

## Ограничения и следующий этап

- Реальная отправка Telegram не выполнялась; HTTP в тестах замокан.
- Excel-экспорт не реализован согласно заданию; отчётные сервисы отделены от views для последующего экспорта.
- Ночные смены не поддерживаются.
- Уникальность активного `store_account` обеспечивается управляемым сервисом и терминальным `OneToOneField`, а не непереносимым conditional unique index. Прямой ORM-доступ должен оставаться только техническим.
- Старые неоднозначные индивидуальные `EmployeeProfile` после data migration остаются неактивными для ручной проверки; они не повышаются до директора.
- Рекомендуемый следующий этап: ручное mobile/desktop acceptance-тестирование порталов, проверка миграции на копии MySQL 8.0, затем подготовка Excel-экспорта и фоновой доставки уведомлений.

## `git diff --stat`

Команда показывает только уже отслеживаемые файлы; новые untracked-файлы перечислены ниже в `git status`.

```text
 CODEX_REPORT_STAGE_3_9_DIRECTOR_SYSTEM_ADMIN.md    | 218 ++++++--
 checklists/forms.py                                | 112 +++-
 checklists/management_services.py                  | 592 +++++++++++++++++-
 checklists/models.py                               | 132 ++++-
 checklists/notifications.py                        |  22 +-
 checklists/portal_forms.py                         |  33 ++
 checklists/portal_views.py                         | 214 +++++++-
 checklists/reporting.py                            |  31 +-
 checklists/services.py                             |  87 ++-
 checklists/test_portals.py                         | 674 ++++++++++++++++++++-
 checklists/test_web.py                             |   6 +-
 checklists/urls.py                                 |  12 +
 checklists/views.py                                |  56 +-
 config/__init__.py                                 |   3 +
 templates/checklists/_answer_sections.html         |  18 +-
 templates/checklists/daily_checklist.html          |   2 +-
 .../checklists/director/checklist_detail.html      |   2 +-
 templates/checklists/director/questions.html       |  39 +-
 templates/checklists/director/report_daily.html    |   2 +-
 .../checklists/director/report_revisions.html      |   2 +-
 templates/checklists/system_admin/audit.html       |  47 +-
 templates/checklists/system_admin/stores.html      |   8 +-
 22 files changed, 2194 insertions(+), 118 deletions(-)
```

## `git status --short`

Отчёт обновлён, новые миграции и templates пока не отслеживаются. Коммит не создавался.

```text
 M CODEX_REPORT_STAGE_3_9_DIRECTOR_SYSTEM_ADMIN.md
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
 M templates/checklists/_answer_sections.html
 M templates/checklists/daily_checklist.html
 M templates/checklists/director/checklist_detail.html
 M templates/checklists/director/questions.html
 M templates/checklists/director/report_daily.html
 M templates/checklists/director/report_revisions.html
 M templates/checklists/system_admin/audit.html
 M templates/checklists/system_admin/stores.html
?? checklists/management/commands/seed_order_count_questions.py
?? checklists/migrations/0008_alter_auditlog_action.py
?? checklists/migrations/0009_alter_auditlog_action.py
?? checklists/migrations/0010_answerrevision_daily_item_and_more.py
?? checklists/test_integer_answers.py
?? templates/checklists/director/question_confirm_delete.html
?? templates/checklists/system_admin/audit_confirm_clear.html
?? templates/checklists/system_admin/store_confirm_delete.html
```

# Дополнение: единая интеграция Telegram

Этот раздел является актуальным продолжением отчёта после реализации
системного Telegram-бота, очереди, привязок и разовых задач. Прежняя
`ChecklistNotification` сохранена для обратной совместимости; новая
интеграция работает через `TelegramOutboundMessage`.

## Системные настройки Telegram

- Добавлена singleton-модель `TelegramSystemSettings`.
- Токен хранится в базе по требованиям этапа и редактируется только
  `system_admin`.
- Интерфейс показывает только маску; пустое поле «Новый токен» не очищает
  значение, удаление выполняется отдельным явным флажком.
- Audit содержит только факт изменения/наличия токена, но не значение.
- Новые страницы находятся в `/settings/telegram/`; директор не видит
  системную форму и токен.

## Альтернативный шлюз и fallback

- `checklists/telegram_client.py` поддерживает стандартный Telegram API path.
- По умолчанию выполняется до 5 попыток через
  `https://tauto.gerbud.ru`, затем до 5 через официальный API.
- Успех требует HTTP 2xx и JSON `ok=true`.
- Поддержаны `sendMessage`, `editMessageText`, `answerCallbackQuery`, `getMe`,
  `getChat`, `getUpdates`.
- Ошибки и технические логи не содержат token или URL с token.
- Задержки выполняются только клиентом после завершения транзакции захвата.

## Очередь сообщений

- `TelegramOutboundMessage` хранит метод, безопасный JSON payload, Store,
  chat/topic, idempotency key, статусы, раздельные счётчики попыток и
  безопасную ошибку.
- `idempotency_key` имеет обычную уникальность, переносимую на MySQL 8.0.
- Обработчик сначала захватывает строки с `select_for_update` и
  `skip_locked`, если backend это поддерживает, фиксирует `processing`,
  после чего выполняет HTTP вне транзакции.
- Зависшие `processing` старше 10 минут допускают повторный захват.
- Сообщения неактивных магазинов не отправляются.

## Telegram-чаты и Topics

- `TelegramStoreChat` разрешает несколько private/group/supergroup/channel
  назначений для каждого Store.
- Поддержаны назначения notifications/tasks/failures/all.
- `message_thread_id` передаётся в payload и проверен тестом.
- Директор управляет только чатами своего Store; `system_admin` — выбранного
  сервером Store.

## Привязка Telegram-пользователя

- `/start` создаёт `TelegramPendingBinding` с одноразовым кодом и сроком
  действия 30 минут.
- Только `system_admin` подтверждает, отклоняет, отключает или переносит
  `TelegramStoreBinding`.
- Активная привязка одного Telegram user ограничена обычной уникальностью
  `telegram_user_id`; поле `user` оставлено nullable для будущей персональной
  привязки Django User.
- Неактивная binding или неактивный Store ботом не обслуживаются.

## Входящие updates

- `poll_telegram_updates` получает updates официальным API polling-методом.
- `TelegramUpdateLog.update_id` уникален; повторный update не обрабатывается.
- В безопасном payload сохраняются только IDs, тип, текст команды/задачи и
  callback metadata, без токена, session key или CSRF.
- Состояние диалога хранится в `TelegramConversationState` и истекает через
  30 минут.

## Разовые задачи

- `StoreAdHocTask` поддерживает утро/день/вечер, planned/active/completed/
  failed/cancelled, web/telegram source и ссылки на daily snapshot.
- `/task` реализован пошагово: дата, этап, текст, описание, предпросмотр,
  подтверждение/изменение/отмена.
- Закрытость этапа проверяется по timezone, расписанию, deadline,
  `DailyChecklist`, `DailyChecklistStage` и повторно при confirm callback.
- Если daily уже существует, создаётся новый `DailyChecklistItem` snapshot с
  `source_item=NULL`; опубликованная версия шаблона не меняется.
- Если daily ещё нет, задача присоединяется во время его создания.
- В интерфейсе терминала snapshot помечен «Разовая задача».
- Выполнение использует обычный ответ терминала и выбранного `StoreEmployee`;
  failed требует комментарий и ставит сообщение в очередь немедленно.

## Шаблоны сообщений

- Поддерживаются 12 событий из единого каталога `telegram_events.py`.
- Существующие индивидуальные шаблоны Store сохранены; новые Store используют
  defaults из кода без обязательного создания строк в БД.
- Поддержаны HTML, MarkdownV2 и plain; значения экранируются по parse mode.
- Набор переменных ограничен отдельно для каждого события, без `eval`.
- Реализованы create/edit/hard delete с fallback, live preview, test send,
  переключение активности и восстановление стандарта.
- Стандартные task/failure сообщения содержат Store, дату, этап, сотрудника,
  комментарий и обычную auth-защищённую ссылку.

## Напоминания

- `schedule_telegram_notifications` создаёт 30- и 10-минутные напоминания,
  stage closed/overdue и итог incomplete tasks.
- Ключ содержит Store, дату, этап, тип и destination; повторный cron не
  создаёт дублей.
- Итог для следующей смены включает детали разовых задач, сотрудника и
  комментарий, когда они известны.

## Безопасность

- Все web mutations используют POST + CSRF.
- `store_id` директорских объектов берётся из серверного профиля, foreign
  objects возвращают нейтральный 404/403.
- Callback data повторно валидируется, update/callback не создаёт вторую
  задачу.
- Token, session key, CSRF и полный Telegram request URL не сохраняются.
- Существующее безопасное удаление Store учитывает новые Telegram-чаты,
  bindings, очередь и разовые задачи; при истории Store деактивируется.

## Management commands и cron

```text
python manage.py poll_telegram_updates --limit 100 --timeout 0
python manage.py schedule_telegram_notifications
python manage.py process_telegram_queue --limit 100
python manage.py process_telegram_queue --retry-failed --store-code CODE
```

Рекомендуемый cron:

```cron
* * * * * poll_telegram_updates
* * * * * schedule_telegram_notifications
* * * * * process_telegram_queue
```

Полные команды с `cd`, Python из `.venv` и log-файлами приведены в README.
Celery не используется.

## Миграции

- `0011_telegrampendingbinding_telegramupdatelog_and_more.py` — новые модели,
  индексы и Audit actions.
- `0012_create_default_telegram_templates.py` — defaults для существующих
  Store.
- `0013_expand_default_telegram_task_templates.py` — безопасно обновляет
  только нетронутые стандартные task templates.
- Миграции `0001`–`0010` не изменялись.
- Схема использует обычные уникальности, `JSONField`, переносимые индексы и
  `select_for_update` feature detection; partial indexes отсутствуют.
- Длины utf8mb4-индексов укладываются в лимит InnoDB MySQL 8.0.

## Тесты и результаты проверок

- Добавлено 29 Telegram-сценариев.
- Все Telegram HTTP-вызовы в тестах замоканы.
- Итоговый полный прогон: `220 passed, 1 warning in 37.34s`.
- `manage.py check`: `System check identified no issues (0 silenced)`.
- `makemigrations --check --dry-run`: `No changes detected`.
- `migrate`: `No migrations to apply`; ранее `0011`, `0012`, `0013` — `OK`.
- `pip check`: `No broken requirements found`.
- `git diff --check`: успешно, вывода нет.
- Единственное предупреждение — transitional warning Django 5.2 о будущем
  default scheme `URLField` в Django 6.0; на корректность не влияет.

## Итоговый `git diff --stat`

```text
 .env.example                                       |   2 +
 CODEX_REPORT_STAGE_3_9_DIRECTOR_SYSTEM_ADMIN.md    | 473 +++++++++++--
 PLAN.md                                            |   4 +
 README.md                                          |  85 ++-
 checklists/admin.py                                |  47 ++
 checklists/apps.py                                 |   3 +
 checklists/forms.py                                | 112 +++-
 checklists/management_services.py                  | 618 ++++++++++++++++-
 checklists/models.py                               | 732 ++++++++++++++++++++-
 checklists/notifications.py                        |  22 +-
 checklists/portal_forms.py                         | 130 ++++
 checklists/portal_views.py                         | 216 +++++-
 checklists/reporting.py                            |  31 +-
 checklists/services.py                             |  99 ++-
 checklists/test_portals.py                         | 674 ++++++++++++++++++-
 checklists/test_web.py                             |   6 +-
 checklists/urls.py                                 |  69 +-
 checklists/views.py                                |  56 +-
 config/__init__.py                                 |   3 +
 templates/base.html                                |   8 +-
 templates/checklists/_answer_sections.html         |  19 +-
 templates/checklists/daily_checklist.html          |   2 +-
 .../checklists/director/checklist_detail.html      |   2 +-
 templates/checklists/director/questions.html       |  39 +-
 templates/checklists/director/report_daily.html    |   2 +-
 .../checklists/director/report_revisions.html      |   2 +-
 templates/checklists/system_admin/audit.html       |  47 +-
 templates/checklists/system_admin/stores.html      |   8 +-
 28 files changed, 3359 insertions(+), 152 deletions(-)
```

Новые untracked-файлы не входят в обычный `git diff --stat` и перечислены в
status ниже.

## Итоговый `git status --short`

```text
 M .env.example
 M CODEX_REPORT_STAGE_3_9_DIRECTOR_SYSTEM_ADMIN.md
 M PLAN.md
 M README.md
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
 M templates/base.html
 M templates/checklists/_answer_sections.html
 M templates/checklists/daily_checklist.html
 M templates/checklists/director/checklist_detail.html
 M templates/checklists/director/questions.html
 M templates/checklists/director/report_daily.html
 M templates/checklists/director/report_revisions.html
 M templates/checklists/system_admin/audit.html
 M templates/checklists/system_admin/stores.html
?? checklists/ad_hoc_tasks.py
?? checklists/management/commands/poll_telegram_updates.py
?? checklists/management/commands/process_telegram_queue.py
?? checklists/management/commands/schedule_telegram_notifications.py
?? checklists/management/commands/seed_order_count_questions.py
?? checklists/migrations/0008_alter_auditlog_action.py
?? checklists/migrations/0009_alter_auditlog_action.py
?? checklists/migrations/0010_answerrevision_daily_item_and_more.py
?? checklists/migrations/0011_telegrampendingbinding_telegramupdatelog_and_more.py
?? checklists/migrations/0012_create_default_telegram_templates.py
?? checklists/migrations/0013_expand_default_telegram_task_templates.py
?? checklists/signals.py
?? checklists/telegram_bot.py
?? checklists/telegram_client.py
?? checklists/telegram_queue.py
?? checklists/telegram_reminders.py
?? checklists/telegram_services.py
?? checklists/telegram_templates.py
?? checklists/telegram_views.py
?? checklists/test_integer_answers.py
?? checklists/test_telegram_integration.py
?? templates/checklists/director/question_confirm_delete.html
?? templates/checklists/system_admin/audit_confirm_clear.html
?? templates/checklists/system_admin/store_confirm_delete.html
?? templates/checklists/telegram/
```

Git-коммит не создавался.

## Переработка интерфейса Telegram-шаблонов

### Навигация и контекст магазина

- Добавлены reusable partials `_breadcrumbs.html`, `_store_header.html` и
  `_navigation.html`; они используются на всех страницах Telegram-раздела.
- Хлебные крошки различают главную директора и системного администратора,
  предыдущие уровни кликабельны, текущий отмечен `aria-current`.
- Выбранный Store всегда показан с названием, кодом, статусом и краткими
  показателями шаблонов, чатов, очереди и ошибок.
- Selector доступен только system_admin. Store director видит подпись о
  применении настроек только к своему магазину.
- Вкладки имеют активное состояние, Unicode-иконки, клавиатурную доступность
  и горизонтальный scroll на узком экране.

### События, модель и миграции

- Единый расширяемый каталог `checklists/telegram_events.py` содержит 12
  событий, пять категорий, описания и допустимые переменные с примерами.
- Поле модели `template_type` безопасно переименовано в `event_code`; добавлены
  `name` и nullable `created_by`. Уникальность остаётся `store + event_code`.
- Миграция `0014_redesign_telegram_templates.py` сохраняет ID, тексты,
  настройки каналов и активность существующих строк, заполняя `name`.
- Миграция `0015_finalize_telegram_template_redesign.py` обновляет choices,
  AuditLog actions и переносимое обычное уникальное ограничение.
- Миграции 0001–0013 не изменялись. Последовательность совместима с SQLite и
  MySQL 8.0: partial indexes и backend-specific SQL не используются.

### Создание, изменение и удаление

- Реализованы отдельные маршруты create/edit/delete/toggle/reset/test/preview.
- При создании показываются только ещё не занятые события; стандартный текст
  загружается серверно, смена события с заполненными полями требует
  подтверждения.
- Удаление выполняется только POST после отдельной GET-страницы подтверждения,
  внутри `transaction.atomic`. После hard delete очередь использует default из
  кода, поэтому доставка не прекращается.
- Reset сначала показывает стандартный вариант и последствия, затем
  выполняется через POST. Стандартный вариант также доступен без сохранения.
- Включение и отключение вынесено в POST action.

### Список и редактор

- Список сгруппирован по категориям и поддерживает поиск, фильтры категории,
  события, активности и назначения (лично, группа, оба, не выбрано).
- Карточки показывают событие, код, описание, parse mode, каналы, статус,
  время и автора изменения, а также все основные действия.
- Редактор mobile-first: вертикальная форма, увеличенные textarea, событие
  readonly после создания, переменные только текущего события.
- Vanilla JS вставляет переменную в позицию курсора активного поля, не
  заменяя выделенный текст и возвращая фокус.
- Live preview выполняется через CSRF-защищённый POST endpoint, не сохраняет
  модель, использует серверную валидацию и примерные значения. Ответ
  отображается через `textContent`, без `eval` и HTML-инъекции.
- Тестовая отправка валидирует текущие несохранённые значения и ставит их в
  очередь только адресатам разрешённого Store, включая Topic ID.

### Права и аудит

- Все views проходят общий role check; store_account получает 403.
- QuerySet edit/delete/toggle/reset/test/preview сначала ограничен серверно
  выбранным или директорским Store; чужой ID возвращает 404.
- Мутации защищены POST, CSRF и транзакциями.
- Добавлены AuditLog actions created, updated, deleted, enabled, disabled,
  reset и test_sent. Metadata содержит Store ID, template ID, event code,
  title, parse mode, channels и actor ID, но не содержит token, URL API,
  session key или CSRF.

### Тесты и проверки

- Telegram integration suite расширен до 51 сценария, включая UI,
  breadcrumbs, scoping, CSRF, CRUD, фильтры, variables, JS, preview, reset,
  test send, audit, fallback и сохранение строк миграцией.
- `python manage.py makemigrations`: `No changes detected`.
- `python manage.py migrate`: `No migrations to apply`.
- `python manage.py makemigrations --check --dry-run`:
  `No changes detected`.
- `python manage.py check`:
  `System check identified no issues (0 silenced)`.
- `pytest -q checklists/test_telegram_integration.py`:
  `51 passed, 1 warning`.
- `pytest -q`: `242 passed, 1 warning`.
- `pip check`: `No broken requirements found`.
- `git diff --check`: успешно, вывода нет.
- Warning — переходное предупреждение Django 5.2 о будущем default scheme
  `URLField` в Django 6.0; на результат тестов не влияет.

### Git diff --stat

```text
28 files changed, 3632 insertions(+), 152 deletions(-)
```

`git diff --stat` не включает untracked-файлы. К этому этапу добавлены
`telegram_events.py`, миграции 0014–0015, JS редактора и шаблонные partials.

### Git status --short

```text
 M .env.example
 M CODEX_REPORT_STAGE_3_9_DIRECTOR_SYSTEM_ADMIN.md
 M PLAN.md
 M README.md
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
 M templates/base.html
 M templates/checklists/_answer_sections.html
 M templates/checklists/daily_checklist.html
 M templates/checklists/director/checklist_detail.html
 M templates/checklists/director/questions.html
 M templates/checklists/director/report_daily.html
 M templates/checklists/director/report_revisions.html
 M templates/checklists/system_admin/audit.html
 M templates/checklists/system_admin/stores.html
?? checklists/ad_hoc_tasks.py
?? checklists/management/commands/poll_telegram_updates.py
?? checklists/management/commands/process_telegram_queue.py
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
?? checklists/signals.py
?? checklists/static/checklists/telegram_template_editor.js
?? checklists/telegram_bot.py
?? checklists/telegram_client.py
?? checklists/telegram_events.py
?? checklists/telegram_queue.py
?? checklists/telegram_reminders.py
?? checklists/telegram_services.py
?? checklists/telegram_templates.py
?? checklists/telegram_views.py
?? checklists/test_integer_answers.py
?? checklists/test_telegram_integration.py
?? templates/checklists/director/question_confirm_delete.html
?? templates/checklists/system_admin/audit_confirm_clear.html
?? templates/checklists/system_admin/store_confirm_delete.html
?? templates/checklists/telegram/
```

Рабочее дерево содержит накопленные незакоммиченные изменения этапов 3.6–3.9;
они сохранены без перезаписи. Git-коммит не создавался.

## Полный доступ system_admin к функциям директора

- Директорские URL и business services не дублировались. После явного выбора
  активного магазина system_admin использует те же `/director/` views для
  сотрудников, смен, вопросов, расписания, уведомлений, задач, отчётов,
  чек-листов и повторного открытия этапов.
- `store_director_required` разрешает system_admin только при наличии
  серверно выбранного Store. Без выбора операции уровня магазина возвращают
  403; store_account административного доступа не получает.
- Меню system_admin разделено на «Система» и «Выбранный магазин» и содержит
  все магазинные функции, включая задачи и Telegram.

## Единый store context

- `resolve_managed_store(request)` возвращает директору Store только из
  активного `EmployeeProfile`, а system_admin — только ID из server-side
  session.
- GET, URL и произвольный `store_id` из бизнес-форм не участвуют в resolution.
  Выбор администратора выполняется отдельным CSRF-защищённым POST endpoint.
- Добавлены единые permission helpers `can_manage_store`,
  `can_manage_store_questions`, `can_manage_store_employees`,
  `can_manage_store_shifts`, `can_manage_store_tasks`,
  `can_manage_store_telegram` и `can_view_store_reports`.
- Telegram-раздел переведён на тот же Store context; прежний отдельный
  Telegram session key больше не используется.

## Управление задачами

- Добавлены URL списка, создания, detail, edit и cancel для
  `StoreAdHocTask`.
- Список поддерживает диапазон дат, этап, статус, источник, поиск и фильтр
  незавершённых задач; показывает создателя, исполнителя и комментарий.
- Web-задача получает `source=web`, `created_by_user=request.user`, Store
  только из context и AuditLog с реальным actor.
- Общие проверки не позволяют создавать/менять задачу в закрытом этапе,
  менять выполненную или отменённую задачу либо обращаться к чужому Store.
- Изменение синхронизирует snapshot в daily checklist; отмена помечает
  прикреплённый пункт необязательным и сохраняет историю.

## Общие breadcrumbs

- Создан один partial `templates/checklists/_breadcrumbs.html` и единый
  context helper `checklists.portal_context.portal_context`.
- Breadcrumbs автоматически строятся по resolver name/kwargs для системных,
  директорских, Telegram, list/detail/create/edit/delete/confirm страниц.
- Текущий уровень не является ссылкой, содержит `aria-current="page"`;
  предыдущие уровни строятся через `reverse`.
- Названия экранируются Django, переносятся через `text-break`/`flex-wrap`.
  Отдельный Telegram breadcrumb partial больше не используется.

## Telegram webhook

- Добавлен `POST /telegram/webhook/`, единственный `csrf_exempt` endpoint.
- Проверяется `X-Telegram-Bot-Api-Secret-Token` через
  `secrets.compare_digest`, Content-Type, размер body и безопасный JSON.
- Token бота не входит в URL. URL формируется из `SITE_URL`; добавлены proxy,
  forwarded-host и trusted-origin настройки для Passenger/Beget.
- Повторный `update_id` возвращает 200 без повторного job и ack.
- Webhook сохраняет только ограниченную безопасную структуру update и не
  выполняет Telegram bot business logic внутри request.

## Очередь входящих команд

- Модель `TelegramInboundJob` содержит уникальный update ID, ссылку на
  сохранённый `TelegramUpdateLog`, Store/Telegram IDs, command, статусы,
  attempts, available/locked/completed timestamps и безопасную ошибку.
- `process_telegram_inbound_queue` поддерживает limit, retry-failed,
  store-code и max-attempts.
- Захват выполняется короткой транзакцией через `select_for_update` и
  `skip_locked`, когда backend поддерживает; обработка идёт вне длинной
  транзакции.
- Polling и inbound worker используют общий
  `telegram_update_processor.py`; отдельной реализации команд нет.

## Быстрый acknowledgment

- После commit выполняется одна попытка через альтернативный gateway с
  timeout не более двух секунд и без пяти retry.
- При неудаче `Принято` ставится в общую outbound queue с уникальным
  idempotency key; webhook всё равно возвращает HTTP 200.
- Callback дополнительно получает быстрый `answerCallbackQuery`.
- Service/unknown/bot/duplicate updates не получают повторный ack.

## Webhook/polling fallback

- `incoming_mode` поддерживает `webhook` и `polling`, default — webhook.
- Активный webhook блокирует обычный `poll_telegram_updates`; `--force`
  оставлен только для диагностики.
- UI system_admin поддерживает setWebhook, getWebhookInfo, deleteWebhook,
  переключение на polling и возврат failed inbound jobs.
- Все методы идут через единый Telegram client с альтернативным gateway и
  штатным fallback; secret и token не попадают в AuditLog.

## Cron

- README содержит единую Beget cron-команду:
  `process_telegram_inbound_queue`, затем
  `schedule_telegram_notifications`, затем `process_telegram_queue`.
- Polling не включён в штатный webhook cron.

## Безопасность

- Все web mutations используют POST + CSRF; webhook является единственным
  обоснованным исключением и проверяет отдельный secret header.
- Store scope применяется до object lookup; чужие ID дают neutral 404.
- `update_id`, inbound job и outbound idempotency key уникальны.
- Неактивные Store/bindings не связываются с входящим job.
- Callback data повторно валидируется общей bot logic.
- Token, webhook secret, полный API URL, session key, CSRF и полный raw update
  не сохраняются в audit/error metadata.

## Миграции

- Создана `0016_telegram_webhook_and_inbound_queue.py`.
- Она добавляет webhook-настройки, Audit actions и `TelegramInboundJob`.
- Миграции 0001–0015 не изменялись; сетевых операций в миграции нет.
- Использованы переносимые `JSONField`, обычные indexes/unique fields и
  nullable foreign keys, совместимые с SQLite и MySQL 8.0.

## Тесты и результаты проверок

- Telegram integration suite: `59 passed, 1 warning`.
- Portal suite: `63 passed, 1 warning`.
- Полный suite: `265 passed, 1 warning in 45.04s`.
- `python manage.py makemigrations`: `No changes detected`.
- `python manage.py migrate`: `No migrations to apply`.
- `python manage.py makemigrations --check --dry-run`:
  `No changes detected`.
- `python manage.py check`:
  `System check identified no issues (0 silenced)`.
- `pip check`: `No broken requirements found`.
- `git diff --check`: успешно, вывода нет.
- Единственный warning — transitional warning Django 5.2 о будущем default
  scheme `URLField` в Django 6.0.

## Итоговый git diff --stat

```text
31 files changed, 4575 insertions(+), 165 deletions(-)
```

Новые untracked-файлы не входят в этот stat. Основные новые файлы этапа:

```text
?? checklists/migrations/0016_telegram_webhook_and_inbound_queue.py
?? checklists/portal_context.py
?? checklists/telegram_inbound.py
?? checklists/telegram_update_processor.py
?? checklists/telegram_webhook.py
?? checklists/management/commands/process_telegram_inbound_queue.py
?? templates/checklists/_breadcrumbs.html
?? templates/checklists/_portal_navigation.html
?? templates/checklists/director/task_detail.html
?? templates/checklists/director/task_form.html
?? templates/checklists/director/tasks.html
```

## Итоговый git status --short

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
 M templates/checklists/director/report_revisions.html
 M templates/checklists/system_admin/audit.html
 M templates/checklists/system_admin/dashboard.html
 M templates/checklists/system_admin/stores.html
?? checklists/ad_hoc_tasks.py
?? checklists/management/commands/poll_telegram_updates.py
?? checklists/management/commands/process_telegram_inbound_queue.py
?? checklists/management/commands/process_telegram_queue.py
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
?? checklists/portal_context.py
?? checklists/signals.py
?? checklists/static/checklists/telegram_template_editor.js
?? checklists/telegram_bot.py
?? checklists/telegram_client.py
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
?? checklists/test_telegram_integration.py
?? templates/checklists/_breadcrumbs.html
?? templates/checklists/_portal_navigation.html
?? templates/checklists/director/question_confirm_delete.html
?? templates/checklists/director/task_detail.html
?? templates/checklists/director/task_form.html
?? templates/checklists/director/tasks.html
?? templates/checklists/system_admin/audit_confirm_clear.html
?? templates/checklists/system_admin/store_confirm_delete.html
?? templates/checklists/telegram/
```

Рабочее дерево остаётся незакоммиченным; сохранены изменения предыдущих
этапов. Git-коммит не создавался.
