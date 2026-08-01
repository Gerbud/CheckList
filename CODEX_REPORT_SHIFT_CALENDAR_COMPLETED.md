# Отчёт: календарь графика сотрудников

Дата: 2026-07-18

## Результат

Страница `/director/shifts/bulk-create/` переработана в календарь массового
планирования. Существующая модель `DailyShiftAssignment`, старые страницы
создания/редактирования смен и прежний POST массового создания сохранены.
Новый интерфейс записывает изменения через те же сервисы управления сменами.

## Реализовано

- помесячная таблица «сотрудники × дни» с фиксированными заголовками;
- навигация по месяцам, процент заполнения и число сотрудников с пропусками;
- ФИО, должность и подразделение сотрудника;
- фильтры по подразделению и состоянию заполнения;
- типы смен: день, ночь, выходной, отпуск, больничный, сервис и личное
  отсутствие;
- одиночное редактирование и выделение диапазона мышью;
- сохранение одной или нескольких ячеек через AJAX без перезагрузки;
- очистка назначения;
- копирование недельного шаблона на выбранный месяц;
- пользовательские шаблоны смен и исходные шаблоны «День», «Сервис», «КЦ»;
- запрет изменения прошлого месяца и прошедших дней текущего месяца;
- полный доступ к редактированию будущих месяцев;
- личный read-only график `/employee/schedule/`;
- Telegram-напоминание за три дня до следующего месяца со списком сотрудников,
  чей график заполнен не полностью, названием магазина и ссылкой на календарь;
- аудит создания, изменения и удаления назначений и шаблонов.

## Изменения модели и миграция

Добавлены миграции:

- `checklists/migrations/0022_dailyshiftassignment_shift_type_and_more.py`.
- `checklists/migrations/0023_alter_dailyshiftassignment_shift_type_and_more.py`.

Изменения:

- `DailyShiftAssignment.shift_type`;
- `StoreEmployee.position`;
- `StoreEmployee.department`;
- новая модель `ShiftTemplate`;
- новые действия аудита;
- data migration создаёт шаблоны «День», «Сервис», «КЦ» для существующих
  магазинов.

Для новых магазинов эти три шаблона создаются сервисом
`create_store_with_defaults`.

Миграция `0023` добавляет сохраняемый тип «Ночная смена» в существующие поля
`DailyShiftAssignment.shift_type` и `ShiftTemplate.shift_type`.

Миграция применена локально. Повторный `migrate` сообщает:
`No migrations to apply`. `makemigrations --check --dry-run` сообщает:
`No changes detected`.

## Изменённые и созданные файлы этапа

- `checklists/models.py`;
- `checklists/migrations/0022_dailyshiftassignment_shift_type_and_more.py`;
- `checklists/migrations/0023_alter_dailyshiftassignment_shift_type_and_more.py`;
- `checklists/admin.py`;
- `checklists/portal_forms.py`;
- `checklists/management_services.py`;
- `checklists/shift_calendar.py`;
- `checklists/portal_views.py`;
- `checklists/urls.py`;
- `checklists/telegram_reminders.py`;
- `checklists/static/checklists/shift_calendar.js`;
- `templates/checklists/director/bulk_shifts.html`;
- `templates/checklists/director/shifts.html`;
- `templates/checklists/director/shift_month.html`;
- `templates/checklists/employee/schedule.html`;
- `checklists/test_shift_calendar.py`;
- `checklists/test_store_calendar_and_employee_schedule.py`;
- `checklists/test_store_terminal.py`;
- `README.md`;
- `PLAN.md`.

В рабочем дереве до этого этапа уже находились незакоммиченные изменения
предыдущих этапов. Они не откатывались и не удалялись.

## Безопасность и совместимость

- директор получает магазин только через существующий
  `store_director_required`;
- каждый `employee_id` повторно проверяется в БД на активность и
  принадлежность текущему магазину;
- пакетное изменение выполняется в `transaction.atomic`;
- лимит одного AJAX-запроса — 1000 ячеек;
- некорректные идентификаторы и JSON возвращают контролируемую ошибку;
- уникальное ограничение `store + employee + work_date` сохранено;
- старые записи получают тип `work`;
- старый HTML POST без `shift_type` автоматически использует `work`;
- старые маршруты одиночного создания, изменения, удаления и копирования
  смен сохранены.

## Компактный UX

- высота строки и ячейки уменьшена до 35–36 px, ширина дня — 34 px;
- имя, подразделение, должность и статус помещены в две компактные строки;
- горизонтальная и вертикальная прокрутка ограничены областью календаря;
- колонка сотрудников и строка дат закреплены через `sticky`;
- статистика, фильтры, добавление, диапазон, копирование и шаблоны собраны в
  одну верхнюю панель;
- нижние крупные блоки перенесены в модальные окна;
- клик по пустой ячейке открывает создание, по заполненной — редактирование;
- комментарий передаётся через существующий пакетный AJAX endpoint;
- сохранение и удаление перерисовывают только затронутую ячейку.

## Тесты и проверки

- календарь и связанный личный график: `16 passed`;
- полный набор: `344 passed`, одно предупреждение о планируемом изменении
  значения `URLField.assume_scheme` в Django 6.0;
- `python manage.py check`: ошибок нет;
- `python manage.py makemigrations --check --dry-run`: изменений нет;
- `python -m pip check`: `No broken requirements found`;
- `deno check checklists/static/checklists/shift_calendar.js`: ошибок нет;
- `git diff --check`: ошибок форматирования нет.

Дополнительно выполнена локальная браузерная проверка: компактная таблица,
sticky-колонка при горизонтальной прокрутке, открытие редактора по клику,
создание ночной смены с комментарием, сохранение после перезагрузки,
редактирование и удаление. Созданные для этой проверки данные удалены.

Локальный `deployment_check` ожидаемо останавливается, потому что `.env`
настроен на SQLite. В production он должен запускаться с
`DATABASE_ENGINE=mysql`.

Покрыты сценарии:

- директор видит только сотрудников своего магазина;
- подстановка сотрудника другого магазина возвращает 403;
- прошлый месяц и прошедший день текущего месяца недоступны для изменения;
- массовое заполнение диапазона;
- применение шаблона времени;
- копирование недельного паттерна;
- совместимость старой формы создания смен;
- сотрудник видит только собственный график;
- выходной и отпуск отображаются в личном графике;
- Telegram-напоминание создаётся один раз.

## Локальная проверка

```bash
source .venv/bin/activate
python manage.py migrate
python manage.py makemigrations --check --dry-run
python manage.py check
pytest -q checklists/test_shift_calendar.py
pytest -q
python -m pip check
git diff --check
```

После запуска сервера проверить:

1. `/director/shifts/bulk-create/`;
2. выделение одного дня и диапазона;
3. применение типа смены и шаблона;
4. копирование недели;
5. фильтры;
6. `/employee/schedule/` под учётной записью связанного сотрудника.

## Выкладка

Перед production-миграцией создать резервную копию MySQL. Затем:

```bash
cd /home/a/autobud/checklist/public_html/django_app
source /home/a/autobud/checklist/public_html/django_venv/bin/activate
python manage.py deployment_check
python manage.py migrate
python manage.py makemigrations --check --dry-run
python manage.py check
python manage.py collectstatic --noinput
touch /home/a/autobud/checklist/public_html/tmp/restart.txt
```

Cron-команда `schedule_telegram_notifications` уже вызывает проверку
заполненности графика. Отдельную cron-команду добавлять не требуется.

Git-коммит не создавался.
