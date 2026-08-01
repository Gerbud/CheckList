# Store Checklist — календарь, график сотрудников и UX вопросов

Дата: 2026-07-18

## Результат

Реализованы настройки логотипа и рабочих дней магазина, календарные статусы
чек-листа, помесячный график сотрудников, личный просмотр смен, контроль
заполнения следующего месяца и Drag & Drop вопросов.

## Backup

Перед изменениями создана проверенная копия локальной SQLite:

`backups/db.sqlite3.stage_employee_schedule.pre_change.20260718-222350.bak`

- `PRAGMA integrity_check`: `ok`;
- SHA-256:
  `e80b4287477b627df08da47903e19633debb0c4f296f130a2c6bc2debf0e6230`.

Каталог `backups/` уже исключён из Git.

## Модели

### Новая модель StoreDayStatus

- магазин;
- дата;
- статус: обычный день, тестирование, выходной или чрезвычайная ситуация;
- комментарий;
- пользователь, изменивший статус;
- дата создания и изменения;
- уникальность `store + date`;
- индекс `store + date + status`.

### Изменённые модели

`DailyChecklist`:

- добавлен снимок `day_status`.

`StoreChecklistSchedule`:

- добавлен JSON-список `working_weekdays`;
- существующим магазинам миграция устанавливает все семь дней, сохраняя
  прежнее поведение до ручной настройки.

`StoreEmployee`:

- добавлена nullable-связь `user`;
- пара `store + user` уникальна;
- учётная запись должна быть активна и иметь активную связь с тем же
  магазином через `UserStoreMembership`.

`Store.logo` уже существовал после предыдущего этапа. В этом этапе добавлено
управление логотипом директором из настроек магазина.

## Миграция

Создана и применена:

`checklists/migrations/0021_storedaystatus_dailychecklist_day_status_and_more.py`

Результат:

```text
Applying checklists.0021_storedaystatus_dailychecklist_day_status_and_more... OK
```

Миграция создаёт только Django-схему и data migration. Ручные изменения базы
не выполнялись.

## Бизнес-правила

- Обычный день участвует в рейтинге.
- Тестирование, выходной и чрезвычайная ситуация остаются в истории, но не
  участвуют в рейтинговых метриках.
- В выходной новый чек-лист не создаётся.
- При назначении существующему дню статуса «Выходной» pending/failed
  уведомления этапов удаляются; уже отправленная история сохраняется.
- Недельное расписание определяет выходной, если для даты нет явного
  календарного переопределения.
- Прошлый месяц графика доступен только для чтения.
- Текущий и будущие месяцы можно изменять.
- Ограничение прошлого месяца действует в сервисах создания, изменения,
  удаления и массового заполнения, а не только в интерфейсе.
- Сотрудник видит только смены карточек `StoreEmployee`, связанных с его
  Django User.
- За три дня до следующего месяца cron проверяет каждый рабочий день. Если
  есть даты без назначений, связанным директорам и администраторам магазина
  ставится идемпотентное Telegram-сообщение.

## Интерфейс

- В настройках магазина директор может загрузить или заменить логотип.
- Шапка показывает логотип текущего магазина либо прежний текст «Чек-лист».
- Настройки расписания содержат дни недели.
- На той же странице доступно назначение особого статуса дате и календарь
  статусов месяца.
- График директора отображается помесячными карточками с переходом к дневному
  редактированию и массовому заполнению.
- `/employee/schedule/` показывает сотруднику личные смены.
- `/director/questions/` переведена на читаемые карточки и визуальные группы.
- SortableJS сохраняет порядок через POST с CSRF без перезагрузки.
- Старые стрелки сортировки сохранены как резервный способ.

## Telegram

Существующая команда:

```bash
python manage.py schedule_telegram_notifications
```

теперь запускает и проверку графика сотрудников. Она создаёт сообщение типа
`employee_schedule_missing`, после чего штатный
`process_telegram_queue` доставляет его через текущий gateway/fallback.

Идемпотентный ключ включает магазин, месяц и Telegram-профиль директора,
поэтому повторный cron не создаёт дубль.

## Основные изменённые файлы

- `checklists/models.py`;
- `checklists/migrations/0021_storedaystatus_dailychecklist_day_status_and_more.py`;
- `checklists/calendar_services.py`;
- `checklists/services.py`;
- `checklists/management_services.py`;
- `checklists/notifications.py`;
- `checklists/telegram_reminders.py`;
- `checklists/management/commands/schedule_telegram_notifications.py`;
- `checklists/reporting.py`;
- `checklists/reporting_v2.py`;
- `checklists/portal_forms.py`;
- `checklists/portal_views.py`;
- `checklists/portal_context.py`;
- `checklists/urls.py`;
- `checklists/admin.py`;
- `templates/base.html`;
- `templates/checklists/director/schedule.html`;
- `templates/checklists/director/shift_month.html`;
- `templates/checklists/director/shifts.html`;
- `templates/checklists/director/questions.html`;
- `templates/checklists/director/report_daily.html`;
- `templates/checklists/director/checklist_detail.html`;
- `templates/checklists/daily_checklist.html`;
- `templates/checklists/employee/schedule.html`;
- `checklists/test_store_calendar_and_employee_schedule.py`;
- `checklists/test_portals.py`;
- `checklists/test_store_terminal.py`;
- `README.md`;
- `PLAN.md`.

## Тесты

Добавлено восемь сценариев:

- тестовый день сохраняется в истории и не влияет на рейтинг;
- выходной не требует чек-листа и не портит статистику;
- выходной отменяет ожидающие уведомления;
- директор не изменяет прошлый месяц;
- сотрудник видит только личный график;
- напоминание создаётся за три дня до месяца и не дублируется;
- невыбранный рабочий день недели становится выходным;
- SortableJS и AJAX-сохранение порядка доступны на странице вопросов.

Итог:

```text
pytest -q
336 passed, 1 warning in 59.67s

python manage.py check
System check identified no issues (0 silenced).

python manage.py makemigrations --check --dry-run
No changes detected

pip check
No broken requirements found.

git diff --check
Ошибок нет.
```

Предупреждение pytest относится к переходному поведению `URLField` перед
Django 6.0 и не влияет на Django 5.2.

`deployment_check` с `DATABASE_ENGINE=mysql` проходит. Сохраняются четыре
известные рекомендации Django по HTTPS redirect, secure session/CSRF cookies
и HSTS.

## Выкладка на Beget

1. Создать и проверить backup production MySQL через панель Beget или
   `mysqldump`.
2. Отдельно сохранить `.env` и весь каталог `media/`, содержащий логотипы.
3. Загрузить файлы проекта, не заменяя `.env`, `media/`, virtualenv и
   production-данные.
4. Активировать окружение:

   ```bash
   cd /home/a/autobud/checklist/public_html/django_app
   source /home/a/autobud/checklist/public_html/django_venv/bin/activate
   ```

5. Проверить окружение и миграции:

   ```bash
   python manage.py deployment_check
   python manage.py showmigrations checklists
   python manage.py makemigrations --check --dry-run
   ```

6. Применить миграцию:

   ```bash
   python manage.py migrate
   ```

   Ожидается `checklists.0021_... OK`.

7. Выполнить проверки и статику:

   ```bash
   python manage.py check
   python manage.py collectstatic --noinput
   ```

8. Перезапустить Passenger:

   ```bash
   touch /home/a/autobud/checklist/public_html/tmp/restart.txt
   ```

9. Оставить cron-команды:

   ```text
   process_telegram_inbound_queue
   schedule_telegram_notifications
   process_telegram_queue
   ```

10. Провести smoke-проверку:

    - логотипы двух разных магазинов;
    - настройка Пн–Пт и выходных;
    - тестовый и выходной день в ежедневном отчёте;
    - текущий и прошлый месяцы графика;
    - личный график связанного сотрудника;
    - Drag & Drop двух вопросов;
    - preview cron за три дня до нового месяца.

При ошибке не изменять таблицы вручную. Остановить выкладку, сохранить вывод
команды и восстанавливать базу только из проверенного backup.
