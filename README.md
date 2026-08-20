# Store Checklist

Внутренний веб-сервис ежедневных чек-листов магазина.

В проекте реализованы предметные модели, версионируемые шаблоны, исторические
снимки ежедневных чек-листов, сервисный слой бизнес-операций, явный журнал
изменений и мобильный интерфейс сотрудника на Django Templates и Bootstrap 5.

## Стек

- Python 3.14
- Django 5.2 LTS
- SQLite для первоначальной локальной разработки
- MySQL 8.0 для production на Beget
- Bootstrap 5 для будущего интерфейса
- pytest и pytest-django
- openpyxl для будущего импорта и экспорта таблиц

## Локальный запуск

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python manage.py migrate
python manage.py seed_checklist
DEMO_MANAGER_PASSWORD='strong-password' \
DEMO_EMPLOYEE_PASSWORD='another-strong-password' \
python manage.py seed_demo_users
STORE_TERMINAL_USERNAME='store-terminal' \
STORE_TERMINAL_PASSWORD='another-strong-password' \
python manage.py seed_store_terminal --with-demo-employees
python manage.py runserver
```

Перед использованием измените `SECRET_KEY` в локальном `.env`. Файл `.env`
не отслеживается Git.

## Проверки

```bash
python manage.py check
pytest
python manage.py makemigrations --check --dry-run
```

## Предметная модель

- `Store` и `EmployeeProfile` обеспечивают работу с несколькими магазинами и
  ролями `employee`, `manager`, `administrator` поверх стандартного Django User.
- `StoreTerminalAccount` связывает один общий Django User с магазином,
  а `StoreEmployee` хранит физических сотрудников без отдельных учётных
  записей. Существующие индивидуальные аккаунты продолжают работать.
- `DailyShiftAssignment` фиксирует назначенных на дату сотрудников, а
  `AnswerRevision` хранит неизменяемую безопасную историю исправлений
  ответов в дополнение к `AuditLog`.
- `ChecklistTemplate`, `ChecklistTemplateVersion`, `ChecklistSection` и
  `ChecklistItem` описывают версионируемый шаблон.
- `DailyChecklist` копирует пункты опубликованной версии в неизменяемые
  `DailyChecklistItem`; ответы хранятся в `ChecklistAnswer`.
- `DailyChecklistStage` делит рабочий день магазина на этапы `opening`
  (09:00–11:00), `during_day` (11:00–20:00) и `closing`
  (20:00–22:00). Границы рассчитываются в часовом поясе `Store.timezone` из
  `StoreChecklistSchedule` и сохраняются в самом этапе.
- Изменение расписания действует только на новые дневные чек-листы и не
  переписывает исторические `opens_at`/`deadline_at`. Время жёлтого
  предупреждения задаётся `warning_minutes_before`.
- Все изменяющие бизнес-операции выполняются функциями из
  `checklists.services` с транзакциями, блокировками и записями `AuditLog`.

Порядок пунктов внутри этапа рассчитывается один раз при создании дневного
чек-листа через SHA-256 и сохраняется в `DailyChecklistItem.display_order`.
Он устойчив при повторном открытии страницы, но зависит от сотрудника, даты и
этапа. Переменную `RANDOMIZATION_SECRET` после начала эксплуатации необходимо
хранить стабильно и не менять без плана миграции исторических данных.

Начальные данные магазина «5 Планет» создаются идемпотентной командой:

```bash
python manage.py seed_checklist
```

Демонстрационные пользователи `manager` и `employee` создаются отдельной
командой `seed_demo_users`. Она требует переменные `DEMO_MANAGER_PASSWORD` и
`DEMO_EMPLOYEE_PASSWORD` и не создаёт пользователей со слабым паролем.

## Интерфейс сотрудника

- `/login/` — вход;
- `/logout/` — выход через POST;
- `/` — сводка чек-листа текущего дня;
- `/checklist/today/` — переход к актуальному этапу;
- `/checklist/today/opening/` — этап открытия;
- `/checklist/today/during-day/` — этап в течение дня;
- `/checklist/today/closing/` — этап закрытия.

### Общий терминал магазина

Основной сценарий магазина использует один общий логин и пароль. Перед
редактированием сотрудник выбирает себя из активных сотрудников своего
магазина. Выбор выполняется только POST-запросом с CSRF-защитой, проверяется
по базе и сохраняется в Django session до ручной смены, выхода или завершения
текущего этапа. Дополнительное подтверждение личности не используется.

Технический Django User сохраняется как `actor`, а выбранный `StoreEmployee` — как
фактический исполнитель в ответе, этапе, ревизии и аудите. При первом ответе
причина не нужна; любое изменение ранее сохранённого статуса или комментария
требует отдельную причину длиной не менее 5 символов.

Терминал и демонстрационных сотрудников можно идемпотентно создать так:

```bash
STORE_TERMINAL_USERNAME='store-terminal' \
STORE_TERMINAL_PASSWORD='replace-with-a-strong-password' \
python manage.py seed_store_terminal --with-demo-employees
```

Ключ `--with-demo-employees` работает только при `DEBUG=True`; в production сотрудников
создают и деактивируют через Django admin. Назначения на смену также ведутся в admin;
состав можно скопировать на следующий день admin action.

Будущий этап недоступен даже по прямому URL и через POST, просроченный этап
остаётся редактируемым, а завершённый доступен только для чтения. После
завершения всех трёх этапов дневной чек-лист закрывается автоматически.
Интерфейс показывает серверные сроки и секундный таймер на чистом JavaScript;
без JavaScript остаются видимыми абсолютные локальные даты и время.

Все изменения ответов и завершение этапов выполняются через
`checklists.services`; views не сохраняют ответы напрямую.

## Telegram-бот, очередь и разовые задачи

Для всей системы используется один бот. Его токен и параметры шлюза настраивает
только `system_admin` на странице `/settings/telegram/`. Токен хранится в
`TelegramSystemSettings`, после сохранения показывается только маска и никогда
не попадает в HTML, audit payload или текст технической ошибки. Пустое поле
«Новый токен» сохраняет прежнее значение; удаление выполняется отдельным
флажком.

По умолчанию клиент делает до пяти попыток через
`https://tauto.gerbud.ru/bot<TOKEN>/<method>`, затем до пяти попыток через
официальный `https://api.telegram.org`. Поддержаны `sendMessage`,
`editMessageText`, `answerCallbackQuery`, `getMe`, `getChat` и `getUpdates`.
Сетевые вызовы исходящих сообщений выполняются после DB-транзакции захвата
очереди.

Единый раздел настроек:

- `/settings/telegram/` — подключение, gateway, статистика и тест;
- `/settings/telegram/templates/` — шаблоны конкретного магазина;
- `/settings/telegram/users/` — pending/active привязки (`system_admin`);
- `/settings/telegram/chats/` — группы, каналы и Telegram Topics;
- `/settings/telegram/queue/` — pending/sent/failed и повтор failed.

Директор ограничен своим магазином и не видит токен или привязки пользователей.
Аккаунт магазина в раздел настроек не допускается. Все изменения выполняются
POST-запросами с CSRF. Для Topic в `TelegramStoreChat` укажите
`message_thread_id`; он будет передан в `sendMessage`.

Пользователь пишет боту `/start`, получает одноразовый код, а системный
администратор подтверждает Store. После подтверждения доступны `/start`,
`/menu`, `/task`, `/tasks`, `/cancel` и `/help`, а также главное InlineKeyboard
меню. Диалог `/task` хранится 30 минут, повторно
проверяет дату и закрытость этапа перед подтверждением и не позволяет подменой
callback добавить задачу в закрытый этап. Разовая задача добавляется как
`DailyChecklistItem` snapshot и не меняет опубликованный шаблон.

Обычные команды и шаги диалога обрабатываются синхронно внутри webhook:
отдельное сообщение «Принято» не отправляется. Бот формирует реальный экран,
делает одну короткую попытку доставки через `tauto.gerbud.ru`, а при временной
сетевой ошибке ставит именно этот экран в `TelegramOutboundMessage`.
`answerCallbackQuery` выполняется до отправки следующего экрана. Дубликаты
`update_id`, callback подтверждения и fallback-сообщения не создают повторную
задачу или второй ответ.

`TelegramInboundJob` используется только для фоновых update: тяжёлых отчётов,
массовых и будущих внешних операций. Cron не требуется для `/start`, `/menu`,
`/help`, `/task`, `/tasks`, выбора даты/этапа, ввода текста и подтверждения
задачи.

Системное меню Telegram регистрируется командой:

```bash
python manage.py register_telegram_commands
```

То же действие и проверка `getMyCommands` доступны system admin в
`/settings/telegram/`. Токен не попадает в URL, журналы и безопасные ошибки.

Ручные запуски:

```bash
python manage.py process_telegram_inbound_queue --limit 50
python manage.py schedule_telegram_notifications
python manage.py process_telegram_queue --limit 50
python manage.py poll_telegram_updates --limit 100 --timeout 0  # fallback
python manage.py process_telegram_queue --retry-failed --store-code store-1
```

Основной приём команд выполняется через webhook
`https://checklist.es-helper.ru/telegram/webhook/`; URL формируется из
`SITE_URL` и не содержит bot token. Polling автоматически отключён при
активном webhook и доступен в режиме `polling` либо с диагностическим
параметром `--force`.

Единая cron-команда для Beget. Все периодические задачи проекта находятся в
`run_beget_cron.sh`; новые задачи следует добавлять в этот файл, не создавая
отдельные записи в панели хостинга:

```cron
* * * * * /home/a/autobud/checklist/public_html/django_app/run_beget_cron.sh >> /home/a/autobud/checklist/cron.log 2>&1
```

Команды безопасны при конкурентном запуске: `update_id` и
`idempotency_key` уникальны, очередь захватывается через `select_for_update`
(`skip_locked`, когда backend поддерживает), зависший `processing` старше
10 минут разрешено захватить повторно. Celery на этом этапе не используется.

`schedule_telegram_notifications` также проверяет график сотрудников за три
дня до начала следующего месяца. Если хотя бы один рабочий день остался без
назначений, связанным директорам магазина ставится в Telegram-очередь
идемпотентное напоминание.

Переменные окружения `TELEGRAM_BOT_TOKEN`,
`TELEGRAM_REQUEST_TIMEOUT` и `TELEGRAM_NOTIFICATIONS_ENABLED` оставлены только
для совместимости со старой командой `send_checklist_notifications`. Новая
интеграция берёт токен и параметры из системной модели.

## Календарь магазина и график сотрудников

В разделе директора **Расписание** настраиваются:

- логотип конкретного магазина;
- рабочие дни недели;
- время этапов чек-листа;
- особые статусы дат: обычный день, тестирование, выходной и чрезвычайная
  ситуация.

Тестовые, выходные и чрезвычайные дни сохраняются в ежедневной истории, но не
участвуют в рейтинге. В выходной чек-лист не создаётся и уведомления по его
этапам не отправляются.

Раздел **Смены** открывает помесячный график. Прошлые месяцы доступны только
для чтения, текущий и будущие месяцы можно редактировать. Чтобы сотрудник видел
свой график по адресу `/employee/schedule/`, свяжите его карточку
`StoreEmployee` с учётной записью, имеющей активную связь с тем же магазином.

Логотипы загружаются в `media/stores/logo/`. Каталог `media/` нельзя заменять
или удалять при выкладке приложения.

## Аналитика и отчёты

Директор своего магазина и system admin после серверного выбора Store получают
единый аналитический раздел `/director/reports/`. Вверху показываются
вычисляемый Store Health, объяснение причин и блок «Что требует внимания»;
исходные факты другого магазина никогда не смешиваются.

Доступны:

- `/director/reports/daily/` — этапы, дедлайны, обязательные ответы, задачи,
  участники смены и Telegram-уведомления;
- `/director/reports/employees/` и `/director/reports/employees/<id>/` —
  участие, failed, пропуски, задачи, revisions и карточка сотрудника;
- `/director/reports/tasks/` — аналитика разовых задач web/Telegram;
- `/director/reports/revisions/` — изменения значений и действия после
  дедлайна;
- `/director/reports/problems/` — полный drill-down проблем;
- `/director/reports/recurring/` — повторяющиеся failed, пропуски, задачи и
  поздние этапы;
- `/system-admin/reports/` — системная сводка активных магазинов.

Store Health не хранится в базе. Статус «Критично» формируется при
незавершённых просроченных этапах и обязательных вопросах без ответа;
«Требует внимания» — при failed, позднем закрытии, revisions, просроченных
задачах и неучастии сотрудника смены; иначе показывается «Нормально».
Все фильтры работают через GET, период ограничен 366 днями. Ежедневный,
сотрудники, задачи, revisions и проблемы экспортируются в CSV с UTF-8 BOM,
учитывая текущий Store и фильтры.

## Локальная проверка с MySQL 8.0

MySQL-сервер не устанавливается автоматически. Для проверки можно использовать
Docker Compose:

```bash
docker compose -f docker-compose.mysql.yml up -d
docker compose -f docker-compose.mysql.yml ps
```

Compose запускает MySQL 8.0 с `utf8mb4`, healthcheck, основной базой
`store_checklist` и отдельной тестовой базой `store_checklist_test`. Данные
хранятся в именованном Docker volume `mysql_data` и не попадают в Git.

Настройте локальный `.env`:

```dotenv
DATABASE_ENGINE=mysql
MYSQL_DATABASE=store_checklist
MYSQL_USER=store_checklist
MYSQL_PASSWORD=local-dev-only-change-me
MYSQL_HOST=127.0.0.1
MYSQL_PORT=3306
MYSQL_TEST_DATABASE=store_checklist_test
```

Примените миграции и запустите тесты:

```bash
python manage.py migrate
pytest
```

Для остановки контейнера:

```bash
docker compose -f docker-compose.mysql.yml down
```

Команда `down -v` дополнительно удалит локальные данные MySQL; используйте её
только когда база больше не нужна.

## MySQL на Beget

В `.env` на сервере установите `DATABASE_ENGINE=mysql` и заполните обязательные
`MYSQL_DATABASE`, `MYSQL_USER`, `MYSQL_PASSWORD`, `MYSQL_HOST`, `MYSQL_PORT`.

Из панели Beget в разделе **MySQL** возьмите:

- полное имя базы данных;
- имя пользователя/доступа;
- пароль созданного доступа;
- hostname из параметров подключения;
- порт MySQL — обычно `3306`.

Для приложения, работающего на том же хостинге Beget, штатный доступ обычно
использует `localhost`. Для внешнего подключения используйте hostname, который
показывает панель в параметрах подключения. Версию сервера проверьте в панели;
проект рассчитан на MySQL 8.0.

Настройки Django используют backend `django.db.backends.mysql`, драйвер
`mysqlclient`, `utf8mb4`, strict mode и соединения с `CONN_MAX_AGE=60`.
Полезные первичные источники:

- [Django 5.2: MySQL notes](https://docs.djangoproject.com/en/5.2/ref/databases/#mysql-notes)
- [Beget: управление базами MySQL](https://beget.com/ru/kb/manual/mysql)

Перед развёртыванием убедитесь, что среда Beget содержит системные клиентские
библиотеки MySQL, необходимые нативному пакету `mysqlclient`.

Запуск WSGI предусмотрен через Gunicorn:

```bash
gunicorn config.wsgi:application
```

## Календарь графика сотрудников

Календарное массовое планирование доступно директору по адресу
`/director/shifts/bulk-create/`. Таблица показывает сотрудников по строкам и
дни выбранного месяца по столбцам. Ячейки сохраняются через AJAX поверх
существующей модели `DailyShiftAssignment`; прежние страницы создания и
редактирования смен продолжают работать.

Поддерживаются типы «День», «Ночь», «Выходной», «Отпуск», «Больничный»,
«Сервис» и «Личное отсутствие», выделение диапазона в строке сотрудника,
копирование недельного шаблона на месяц, фильтры по подразделению и
заполненности, а также шаблоны времени смен. Клик по ячейке открывает
компактный редактор с комментарием и удалением назначения. Прошлый месяц
доступен только для просмотра, в текущем месяце нельзя менять прошедшие даты.

Сотрудник с привязанной учётной записью видит собственный график без права
редактирования по адресу `/employee/schedule/`.

Локальная проверка календаря:

```bash
python manage.py migrate
python manage.py check
pytest -q checklists/test_shift_calendar.py
pytest -q
```
