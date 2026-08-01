# Технический отчёт: этап 3 — авторизация и интерфейс сотрудника

Дата проверки: 16 июля 2026 года  
Проект: `/Users/bud/Projects/store-checklist`  
Ветка: `main`

## 1. Созданные страницы

- `templates/base.html` — общий mobile-first каркас, навигация, сообщения,
  Bootstrap 5 и ограничение ширины контента до 800 px.
- `templates/registration/login.html` — русскоязычная страница входа.
- `templates/checklists/dashboard.html` — домашняя страница сотрудника со
  статусом и сводкой сегодняшнего чек-листа.
- `templates/checklists/daily_checklist.html` — заполнение и read-only просмотр
  результата ежедневного чек-листа.
- `templates/checklists/_answer_sections.html` — переиспользуемое отображение
  разделов и пунктов.
- `templates/checklists/profile_missing.html` — сообщение об отсутствующем или
  неактивном профиле сотрудника.
- `templates/403.html` — базовая страница запрета доступа.
- `templates/404.html` — базовая страница отсутствующего ресурса.

Интерфейс использует крупные Bootstrap button-toggle элементы вместо маленьких
radio button, фиксированную нижнюю панель действий на смартфоне и адаптивную
сетку. Кнопка «Не применимо» выводится только для разрешённых пунктов.

## 2. URL

| URL | Имя | Назначение | Доступ |
| --- | --- | --- | --- |
| `/login/` | `login` | Вход стандартным Django LoginView | Публичный |
| `/logout/` | `logout` | Выход стандартным Django LogoutView через POST | Авторизованный |
| `/` | `checklists:dashboard` | Домашняя страница сотрудника | Авторизованный с активным профилем |
| `/checklist/today/` | `checklists:today` | Создание, заполнение и результат текущего дня | Авторизованный с активным профилем |
| `/admin/` | `admin:index` | Существующий Django admin | По стандартным admin permissions |

После входа пользователь перенаправляется на `/`. Неавторизованный запрос к
защищённым страницам перенаправляется на `/login/?next=...`.

## 3. Формы и views

### Форма

В `checklists/forms.py` создана `DailyChecklistAnswersForm`:

- динамически создаёт поля только для ответов текущего чек-листа;
- принимает только значения из `ChecklistAnswer.Status`;
- проверяет обязательный комментарий для `failed`;
- запрещает `not_applicable`, если снимок пункта его не разрешает;
- при завершении отклоняет оставшиеся `pending`;
- анализирует идентификаторы в POST и отклоняет пункты чужого чек-листа;
- возвращает только действительно изменившиеся ответы.

### Views

В `checklists/views.py` реализованы function-based views:

- `dashboard` — профиль, локальная дата магазина, текущий чек-лист и сводка;
- `today_checklist` — получение или создание чек-листа, отображение формы,
  промежуточное сохранение и завершение;
- `permission_denied` и `page_not_found` — обработчики 403 и 404.

Views не вызывают `ChecklistAnswer.save()`. Все изменения выполняются через:

- `create_daily_checklist`;
- `update_answer`;
- `complete_daily_checklist`.

Один POST обёрнут во внешнюю транзакцию, а каждый бизнес-вызов сохраняет
собственную атомарность сервисного слоя. В аудит передаются `REMOTE_ADDR` и
`HTTP_USER_AGENT`.

## 4. Механизм проверки прав

1. `login_required` защищает `/` и `/checklist/today/`.
2. Для пользователя загружается только активный `EmployeeProfile` активного
   магазина.
3. При отсутствии такого профиля возвращается русскоязычная страница с HTTP
   403 и сообщением обратиться к руководителю.
4. Чек-лист выбирается только по текущему профилю, его магазину и локальной
   дате магазина; публичного параметра чужого сотрудника нет.
5. Допустимые ответы формы формируются из собственного ежедневного чек-листа.
   Подмена ID ответа в POST приводит к ошибке формы без сохранения данных.
6. POST к завершённому чек-листу возвращает 403; результат отображается только
   для чтения.
7. Менеджерский интерфейс не создавался. Чужие чек-листы менеджер и
   администратор по-прежнему просматривают только через Django admin.
8. Сервисный слой повторно проверяет принадлежность и роль независимо от view.

## 5. Начальные пользователи

Добавлена идемпотентная команда `seed_demo_users`, создающая:

- `manager` с ролью `manager`;
- `employee` с ролью `employee`.

Оба профиля относятся к магазину «5 Планет». Пароли читаются из
`DEMO_MANAGER_PASSWORD` и `DEMO_EMPLOYEE_PASSWORD`, проходят стандартные Django
password validators и не хранятся в исходном коде. При отсутствии или слабости
переменных команда завершается с `CommandError` до создания пользователей.

Результаты ручных запусков:

```text
Созданы демонстрационные пользователи: 2.
Демонстрационные пользователи уже существуют; изменений нет.
```

## 6. Тесты

Всего собрано 34 теста:

- 15 существующих тестов предметной модели и сервисного слоя;
- 19 новых web-тестов авторизации, доступа, форм, аудита, seed-команды и
  мобильной разметки.

Покрыты все обязательные сценарии задания, включая успешное завершение через
view, запрет завершения с pending, read-only completed, чужой ID ответа,
передачу IP/User-Agent и три начальных раздела.

Последний результат:

```text
collected 34 items
checklists/test_web.py ................... [55%]
checklists/tests.py ...............        [100%]
34 passed in 4.78s
```

## 7. Результаты проверок

### Миграции

Модели на этапе 3 не менялись, поэтому новая миграция не создана:

```text
python manage.py makemigrations
No changes detected

python manage.py migrate
No migrations to apply.

python manage.py makemigrations --check --dry-run
No changes detected
```

### Django check

```text
System check identified no issues (0 silenced).
```

### pytest

```text
34 passed in 4.78s
```

### pip check

```text
No broken requirements found.
```

Pip выводит не влияющее на проект предупреждение о недоступности каталога
`/Users/bud/Library/Caches/pip`; кэш автоматически отключается.

## 8. Smoke-проверка страниц

По разрешённому заданием fallback-сценарию использован Django test client.

```text
anonymous {'/login/': 200, '/': 302, '/checklist/today/': 302}
login True
authenticated {'/': 200, '/checklist/today/': 200}
```

Таким образом, login доступен публично, защищённые страницы перенаправляют
анонимного пользователя, а после входа обе основные страницы отдаются успешно.

## 9. Созданные и изменённые файлы

Созданы:

- `checklists/forms.py`
- `checklists/urls.py`
- `checklists/test_web.py`
- `checklists/management/commands/seed_demo_users.py`
- `templates/base.html`
- `templates/registration/login.html`
- `templates/checklists/dashboard.html`
- `templates/checklists/daily_checklist.html`
- `templates/checklists/_answer_sections.html`
- `templates/checklists/profile_missing.html`
- `templates/403.html`
- `templates/404.html`
- `CODEX_REPORT_STAGE_3.md`

Изменены:

- `.env.example`
- `README.md`
- `PLAN.md`
- `checklists/views.py`
- `config/settings.py`
- `config/urls.py`

## 10. Найденные проблемы и ограничения

1. Первый прогон новых web-тестов выявил ошибочное обращение теста к обратной
   OneToOne-связи как к полю `answer_id`. Тест исправлен на `item.answer.pk`;
   прикладной код затронут не был.
2. SQLite не реализует реальные блокировки `select_for_update`. Конкурентные
   web-сценарии нужно дополнительно проверить на PostgreSQL.
3. Первый переход на `/checklist/today/` создаёт чек-лист через GET. Операция
   идемпотентна и защищена сервисом от дубликата, но в будущем лучше выделить
   отдельный POST запуска для строгой HTTP-семантики.
4. Bootstrap загружается через jsDelivr CDN. Для офлайн-работы магазина или
   строгой Content Security Policy потребуется локальная сборка статических
   файлов.
5. Промежуточное сохранение выполняется вручную кнопкой; фонового автосохранения
   и защиты от закрытия вкладки пока нет.
6. Повторный `seed_demo_users` не меняет пароль уже существующего пользователя.
   Для ротации паролей нужна отдельная административная процедура.
7. Созданный `manager` имеет `is_staff`, но model permissions Django admin не
   назначаются автоматически.
8. Полноценный кабинет руководителя, XLSX, фотографии, уведомления и production
   deployment намеренно не реализованы.
9. Визуальный smoke выполнен test client, без проверки в реальном мобильном
   браузере и без загрузки CDN-ресурсов.

## 11. Git diff --stat

Обычный `git diff --stat` показывает только изменённые tracked-файлы и не
включает новые untracked-файлы:

```text
 .env.example        |   4 +
 PLAN.md             |   4 +-
 README.md           |  21 ++++-
 checklists/views.py | 245 +++++++++++++++++++++++++++++++++++++++++++++++++++-
 config/settings.py  |   8 +-
 config/urls.py      |  30 +++----
 6 files changed, 287 insertions(+), 25 deletions(-)
```

## 12. Git status

После создания отчёта:

```text
## main
 M .env.example
 M PLAN.md
 M README.md
 M checklists/views.py
 M config/settings.py
 M config/urls.py
?? CODEX_REPORT_STAGE_3.md
?? checklists/forms.py
?? checklists/management/commands/seed_demo_users.py
?? checklists/test_web.py
?? checklists/urls.py
?? templates/
```

Staging и Git-коммит не выполнялись.
