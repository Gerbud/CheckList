# Отчёт: минутные окна, черновики и прогресс этапов

Дата: 20.07.2026.

## 1. Что изменено

Рабочая механика раннего просмотра и черновиков сохранена и расширена:

- окна завершения переведены с целых часов на минуты;
- значение `0` теперь отключает ограничение и разрешает завершение с момента
  открытия этапа;
- на dashboard сотрудника добавлены отвеченные вопросы, общее количество,
  процент и вычисляемые статусы;
- будущие этапы выглядят доступными для заполнения, показывают точную границу и
  обратный отсчёт;
- завершённый этап гарантированно доступен только для чтения;
- на dashboard директора добавлена взаимоисключающая сводка этапов текущего
  дня;
- таймер различает время до открытия, до окна, внутри окна, просрочку и
  завершение.

Разделение backend-действий «Сохранить ответы» и «Завершить этап»,
timezone-aware расчёты, защита магазина и снимок `completion_available_at`
сохранены.

## 2. Решение по архитектуре этапов

Масштабная универсализация не выполнялась. В проекте нет отдельной
конфигурационной сущности `Stage` или `ScheduleStage`: расписание содержит три
фиксированные границы, а `DailyChecklistStage` является историческим экземпляром
этапа конкретного дня. Перенос конфигурации потребовал бы менять модель
шаблона, URL, формы, уведомления и сервис создания чек-листа.

Безопасный локальный вариант — сохранить три поля настройки и существующий
`DailyChecklistStage`. Технический долг: в будущем можно добавить
`StoreScheduleStage` с кодом, названием, порядком, временем открытия,
дедлайном и окном завершения, затем мигрировать три текущие записи каждого
магазина и только после этого переводить URL и шаблоны на динамический список.

## 3. Поля моделей

В `StoreChecklistSchedule` старые поля:

- `morning_completion_window_hours`;
- `day_completion_window_hours`;
- `evening_completion_window_hours`

заменены на:

- `morning_completion_window_minutes`;
- `day_completion_window_minutes`;
- `evening_completion_window_minutes`.

Значение по умолчанию — 120 минут. Допустимы значения 0–720 с шагом 15 минут.
Диапазон и шаг проверяются формой, `clean()` модели и переносимыми
`CheckConstraint` с допустимым набором значений в БД.

Поле `DailyChecklistStage.completion_available_at` сохранено без изменений.

## 4. Миграция часов в минуты

Миграция `0025_completion_windows_in_minutes.py`:

1. удаляет ограничения старых полей;
2. переименовывает столбцы, сохраняя данные;
3. умножает каждое старое значение на 60 одним SQL-обновлением;
4. меняет default и валидаторы;
5. добавляет ограничения минутных значений.

Проверены преобразования `2 → 120`, `1 → 60`, `0 → 0` и отсутствие старых
полей в новом состоянии миграций. Обратное преобразование допускается только
для значений, кратных 60, чтобы rollback не выполнял скрытую потерю данных.
Миграция успешно применена на локальной SQLite. Схема и ограничения включены в
MySQL-совместимые тесты.

## 5. Значение 0 и расчёт снимка

Формула:

```python
if completion_window_minutes == 0:
    completion_available_at = opens_at
else:
    completion_available_at = max(
        opens_at,
        deadline_at - timedelta(minutes=completion_window_minutes),
    )
```

Поэтому `0` разрешает завершение сразу после открытия, но не до него. Окно,
которое длиннее этапа, также ограничивается `opens_at`. Переход вечернего
дедлайна через полночь поддерживается.

## 6. Использование `completion_available_at`

Граница вычисляется только при создании `DailyChecklistStage` и сохраняется в
нём. После этого:

- backend-проверка завершения;
- dashboard и страница этапа;
- disabled-состояние кнопки;
- JavaScript-таймер

используют снимок этапа, а не актуальное значение расписания магазина.
Изменение 120 минут на 30 не меняет старый этап; этап следующего дня получает
новую границу. Уведомления продолжают планироваться от `deadline_at` и
останавливаются только после фактического завершения.

## 7. Форма директора и аудит

Блок называется «За сколько времени до окончания разрешать завершение этапа».
Для утра, дня и вечера используются select-поля со всеми значениями 0–720
через 15 минут и удобными подписями: «Сразу после открытия», «15 минут»,
«1 час», «1 час 30 минут», «2 часа» и далее.

`STORE_SCHEDULE_UPDATED` хранит полные старые и новые значения. Для изменённых
окон дополнительно записывается `completion_window_changes` с этапом, именем
поля, `old_minutes` и `new_minutes`. Пользователь, магазин и время уже являются
полями `AuditLog`.

## 8. Прогресс сотрудника

Для каждого этапа считаются только снимки вопросов, включённые в текущий
`DailyChecklist`. Неактивные и не действующие на дату вопросы не попадают в
снимок и не учитываются.

Отвеченным считается:

- статусный вопрос со статусом, отличным от `pending`;
- числовой вопрос с непустым `integer_value`.

Повторное сохранение обновляет `OneToOne`-ответ и не увеличивает счётчик.
Процент — округлённое отношение отвеченных вопросов к общему количеству.
Заполнение 100% не завершает этап автоматически.

Вычисляемые UI-статусы не добавлены в БД:

1. `Завершён`;
2. `Просрочен`;
3. `Готов к завершению` при 100%;
4. `Заполнено частично`;
5. `Есть черновик` при сохранённой активности без выбранного ответа;
6. `Не начат`.

## 9. Сводка директора

Для утра, дня и вечера показываются:

- завершили;
- есть черновик;
- не приступили;
- просрочили.

Классификация взаимоисключающая: завершение определяется по `completed_at`,
затем незавершённый этап после `deadline_at` считается просроченным, затем
учитывается `Exists` сохранённого ответа, остаток относится к не приступившим.

Сводка ограничена магазином и датой. Директор видит свой магазин, системный
администратор — выбранный магазин через существующий access-control. Агрегация
выполняется одним запросом с `Exists` и условными `Count`; тест фиксирует ровно
один запрос и отсутствие N+1.

## 10. Backend и read-only

- До `completion_available_at` завершение отклоняется независимо от frontend.
- До `opens_at` завершение отклоняется и при окне 0.
- Валидные ответы преждевременного POST сохраняются отдельно от попытки
  завершения.
- Сохраняются проверки пользователя, магазина, сотрудника и повторного
  завершения.
- `update_answer` блокирует завершённый этап на сервисном уровне.
- View возвращает 403 на прямой POST завершённого этапа.
- В read-only шаблоне нет полей ответа и кнопок сохранения/завершения.
- Аудит и revisions ответов не удаляются.

## 11. Шаблоны и JavaScript

Обновлены:

- `templates/checklists/dashboard.html`;
- `templates/checklists/daily_checklist.html`;
- `templates/checklists/director/schedule.html`;
- `templates/checklists/director/dashboard.html`;
- `checklists/static/checklists/deadline_timer.js`.

Таймер сначала показывает время до начала этапа, затем при необходимости время
до окна, внутри окна — время до дедлайна, после дедлайна — просрочку. Для окна
0 отдельного отсчёта до окна нет. При пересечении границы кнопка автоматически
активируется; backend остаётся источником истины.

## 12. Тесты

Добавлен migration-test и расширен
`checklists/test_stage_completion_windows.py`. Проверены:

- миграция 2/1/0 часов в 120/60/0 минут и удаление старых полей;
- модель, форма и БД для диапазона и шага 15;
- окна 0, 15, 30, 45, 90 и 720 минут;
- точная граница, запрет до открытия и прямой POST;
- ограничение длинного окна временем открытия;
- ночной дедлайн;
- неизменность старого снимка и новая граница следующего дня;
- прогресс 0%, 50%, 100%, разные типы ответов и неактивный вопрос;
- черновик без фактического ответа;
- отсутствие дублей;
- read-only UI, прямой POST и сервис;
- уведомления от дедлайна и завершения;
- аудит;
- директорская классификация, store scoping и один SQL-запрос;
- MySQL-совместимая схема.

Полный результат:

```text
382 passed, 1 warning in 72.02s
```

Единственное предупреждение — переходное изменение default-схемы `URLField` в
Django 6.0; к этой доработке оно не относится.

## 13. Браузерная проверка

На локальном Django-сервере вручную проверены:

- выбранные настройки 0, 30 и 90 минут с удобными подписями;
- доступность будущего вечернего этапа;
- сохранение неполного черновика;
- прогресс `1 из 2 — 50%`;
- точное время начала завершения и оба состояния таймера;
- автоматическая активация кнопки на границе без перезагрузки;
- завершение при окне 0;
- отсутствие кнопок и полей у завершённого этапа;
- сводка директора;
- отсутствие ошибок и предупреждений в console.

Временный магазин и учётные записи браузерной проверки после проверки удалены.

## 14. Результаты обязательных проверок

```text
python manage.py makemigrations --check --dry-run
No changes detected

python manage.py check
System check identified no issues (0 silenced).

pytest -q
382 passed, 1 warning in 72.02s

pip check
No broken requirements found.

git diff --check
успешно, вывод отсутствует
```

## 15. Выкладка

```bash
source .venv/bin/activate
python manage.py migrate
python manage.py collectstatic --noinput
python manage.py check
pytest -q
```

- `migrate` — требуется для миграций 0024 и 0025;
- `collectstatic` — требуется из-за изменения `deadline_timer.js`;
- restart — требуется для загрузки обновлённого Python-кода.

Перед миграцией production нужна штатная резервная копия БД.

## 16. Полный Git-статус

`git diff --stat`:

```text
 CODEX_REPORT_INDEX.md                          |   1 +
 checklists/forms.py                            |   2 +-
 checklists/management_services.py              |  26 ++++-
 checklists/models.py                           | 102 +++++++++++++++++--
 checklists/portal_forms.py                     |  65 ++++++++++++
 checklists/reporting.py                        |  81 ++++++++++++++-
 checklists/services.py                         |  59 +++++++++--
 checklists/static/checklists/deadline_timer.js |  28 +++++-
 checklists/test_integer_answers.py             |   5 +-
 checklists/test_mysql_compat.py                |  20 +++-
 checklists/test_time_stages.py                 |  37 ++++---
 checklists/test_web.py                         |   2 +-
 checklists/tests.py                            |   6 +-
 checklists/views.py                            | 132 +++++++++++++++++--------
 templates/checklists/daily_checklist.html      |  24 +++--
 templates/checklists/dashboard.html            |  23 +++--
 templates/checklists/director/dashboard.html   |  18 ++++
 templates/checklists/director/schedule.html    |  21 +++-
 18 files changed, 558 insertions(+), 94 deletions(-)
```

Обычный stat не включает новые неотслеживаемые файлы.

`git status --short`:

```text
 M CODEX_REPORT_INDEX.md
 M checklists/forms.py
 M checklists/management_services.py
 M checklists/models.py
 M checklists/portal_forms.py
 M checklists/reporting.py
 M checklists/services.py
 M checklists/static/checklists/deadline_timer.js
 M checklists/test_integer_answers.py
 M checklists/test_mysql_compat.py
 M checklists/test_time_stages.py
 M checklists/test_web.py
 M checklists/tests.py
 M checklists/views.py
 M templates/checklists/daily_checklist.html
 M templates/checklists/dashboard.html
 M templates/checklists/director/dashboard.html
 M templates/checklists/director/schedule.html
?? CODEX_REPORT_STAGE_COMPLETION_WINDOWS.md
?? checklists/migrations/0024_dailycheckliststage_completion_available_at_and_more.py
?? checklists/migrations/0025_completion_windows_in_minutes.py
?? checklists/test_completion_window_migration.py
?? checklists/test_stage_completion_windows.py
```

Git-коммит не создавался.
