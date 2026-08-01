# Технический отчёт: этап 3.5 — подготовка MySQL 8.0 / Beget

Дата проверки: 16 июля 2026 года  
Проект: `/Users/bud/Projects/store-checklist`  
Ветка: `main`

## 1. Изменённые зависимости

В `requirements.txt` выполнена замена:

```text
- psycopg[binary]==3.3.4
+ mysqlclient==2.2.8
```

В локальном `.venv`:

- установлен `mysqlclient 2.2.8`;
- удалён `psycopg 3.3.4`;
- удалён `psycopg-binary 3.3.4`;
- импорт `MySQLdb` проверен успешно: версия `(2, 2, 8, 'final', 0)`;
- `pip check`: `No broken requirements found`.

Для сборки `mysqlclient` под Python 3.14/macOS потребовались только клиентские
библиотеки `mysql-client` и `pkgconf`. MySQL-сервер не устанавливался.

## 2. Настройки DATABASES

Логика вынесена в `config/database.py`. Поддерживаются два режима:

- `DATABASE_ENGINE=sqlite` — локальный режим по умолчанию;
- `DATABASE_ENGINE=mysql` — MySQL 8.0 для Beget и локального Docker.

MySQL использует:

```python
{
    'ENGINE': 'django.db.backends.mysql',
    'NAME': MYSQL_DATABASE,
    'USER': MYSQL_USER,
    'PASSWORD': MYSQL_PASSWORD,
    'HOST': MYSQL_HOST,
    'PORT': MYSQL_PORT,
    'CONN_MAX_AGE': 60,
    'OPTIONS': {
        'charset': 'utf8mb4',
        'init_command': "SET sql_mode='STRICT_TRANS_TABLES'",
    },
}
```

При `DATABASE_ENGINE=mysql` обязательны:

- `MYSQL_DATABASE`;
- `MYSQL_USER`;
- `MYSQL_PASSWORD`;
- `MYSQL_HOST`;
- `MYSQL_PORT`.

Если хотя бы одна переменная отсутствует или пуста, Django останавливает запуск
с `ImproperlyConfigured` и перечисляет отсутствующие переменные. Неизвестное
значение `DATABASE_ENGINE` также отклоняется.

Опциональная `MYSQL_TEST_DATABASE` задаёт отдельную базу pytest через
`DATABASES['default']['TEST']['NAME']`.

## 3. Изменения моделей, сервиса и миграций

Из `ChecklistTemplateVersion.Meta.constraints` удалён условный
`UniqueConstraint(condition=Q(status='published'))`, поскольку MySQL не
предоставляет эквивалент частичного уникального индекса Django.

Создана миграция:

```text
checklists.0002_remove_checklisttemplateversion_one_published_version_per_template
```

Она удаляет constraint `one_published_version_per_template`. Миграция применена
локально с результатом `OK`.

Обычные переносимые ограничения сохранены:

- уникальность `(template, version_number)`;
- уникальность `(version, code)` для разделов;
- уникальность `(store, employee, checklist_date)` для `DailyChecklist`;
- проверка наличия `published_at` у опубликованной/архивной версии.

### Публикация версии

`publish_template_version` по-прежнему выполняется в `transaction.atomic` и
теперь явно:

1. блокирует строку шаблона через `select_for_update`;
2. блокирует все версии шаблона через `select_for_update` по индексированному
   внешнему ключу;
3. проверяет, что целевая версия является черновиком;
4. архивирует ранее опубликованные версии;
5. только после этого публикует целевую версию;
6. записывает AuditLog в той же транзакции.

Обычный `version.save()` не может перевести версию в `published`: модель требует
вызов сервисного слоя. Это уменьшает риск нарушения правила без частичного
индекса.

## 4. Проверка совместимости с MySQL 8.0

### JSONField

`AuditLog.old_value` и `new_value` используют стандартный Django `JSONField`,
поддерживаемый MySQL. Добавлен тест round-trip для Unicode JSON, boolean,
integer и `NULL`.

### UniqueConstraint

- условный unique constraint удалён;
- оставшиеся unique constraints не имеют `condition`, `include`, `opclasses`,
  `deferrable` или PostgreSQL-специфичных параметров;
- составной unique ежедневного чек-листа сохранён без nullable-полей.

### DateTimeField

Используются стандартные timezone-aware `DateTimeField` и `USE_TZ=True`.
Приложение сохраняет UTC, а локальную дату магазина рассчитывает через Python
`zoneinfo`. Свежая схема Django/MySQL использует поддержку дробных секунд.

### Транзакции и блокировки

- сервисы используют `transaction.atomic`;
- блокировки выполняются по первичным ключам, внешним ключам или составному
  уникальному набору, то есть по индексируемым полям;
- для production требуется InnoDB; Docker image MySQL 8.0 использует InnoDB по
  умолчанию.

### utf8mb4, сортировка и индексы

- соединение явно использует `utf8mb4`;
- Docker создаёт базы с `utf8mb4_unicode_ci`;
- индексируемые прикладные строковые поля имеют длину 32 и 50 символов, что
  укладывается в ограничения MySQL 8/InnoDB при `utf8mb4`;
- case-insensitive collation означает, что уникальные строковые значения,
  отличающиеся только регистром, считаются одинаковыми.

### Nullable-поля

Уникальность ежедневного чек-листа не содержит nullable-полей. Nullable JSON,
IP, User-Agent, actor, source_item и временные поля не участвуют в уникальных
индексах. Проверка опубликованной версии дополнительно обеспечивается сервисом,
даже если конкретная минорная версия MySQL ограниченно обрабатывает CHECK.

## 5. MySQL-совместимые тесты

Добавлен `checklists/test_mysql_compat.py` с 7 тестами:

1. обязательность всех MySQL env-переменных;
2. backend, `utf8mb4`, strict mode, `CONN_MAX_AGE` и тестовая база;
3. отсутствие conditional UniqueConstraint и сохранение обычного daily unique;
4. блокировка шаблона/версий и архивирование предыдущей публикации;
5. запрет прямой публикации через model save;
6. JSON/Unicode/NULL round-trip;
7. безопасные длины индексируемых строковых полей.

Существующий тест атомарного отката публикации также продолжает проходить.

Итог всего проекта:

```text
collected 41 items
checklists/test_mysql_compat.py .......  [17%]
checklists/test_web.py ................... [63%]
checklists/tests.py ............... [100%]
41 passed in 4.99s
```

## 6. Docker Compose

Добавлены:

- `docker-compose.mysql.yml`;
- `docker/mysql/init/01-create-test-database.sh`.

Конфигурация содержит:

- image `mysql:8.0`;
- `utf8mb4` и `utf8mb4_unicode_ci`;
- healthcheck через `mysqladmin ping`;
- основную базу;
- отдельную тестовую базу;
- именованный volume `mysql_data`;
- права пользователя на тестовую базу.

Данные контейнера не находятся в рабочем дереве Git. Дополнительно
`.mysql-data/` внесена в `.gitignore` как защита от возможного bind mount.

Docker CLI на текущей машине отсутствует, поэтому контейнер и `docker compose
config` не запускались. Shell-синтаксис init-скрипта проверен через `sh -n`.

## 7. Инструкция Beget

README дополнен инструкциями запуска Docker, применения миграций и pytest на
MySQL. Для Beget из раздела панели **MySQL** нужно перенести в `.env`:

- полное имя базы;
- имя пользователя/доступа;
- пароль;
- hostname из параметров подключения;
- порт, обычно `3306`.

Для приложения на том же виртуальном хостинге штатный hostname обычно
`localhost`; для внешнего подключения следует использовать точное имя сервера
из панели. Версию MySQL необходимо подтвердить в панели Beget.

## 8. Результаты обязательных проверок

```text
python manage.py makemigrations
No changes detected

python manage.py migrate
No migrations to apply.

python manage.py makemigrations --check --dry-run
No changes detected

python manage.py check
System check identified no issues (0 silenced).

pytest
41 passed in 4.99s

pip check
No broken requirements found.
```

Перед финальным повторным прогоном миграция `0002` была применена с `OK`.

## 9. Ограничения

1. Реальный MySQL 8.0 не был доступен, а Docker CLI отсутствует. Миграции и
   конкурентные блокировки ещё нужно выполнить на настоящем MySQL.
2. Миграция `0001` исторически содержит conditional constraint, который Django
   игнорирует на MySQL; миграция `0002` удаляет его из итогового состояния.
   Fresh-install на MySQL нужно обязательно проверить перед Beget deployment.
3. Без database-level partial index правило единственной публикации можно
   обойти через raw SQL или прямой `QuerySet.update`. Прикладной код обязан
   использовать `publish_template_version`; обычный `save()` защищён.
4. MySQL collation по умолчанию нечувствителен к регистру, что влияет на
   сравнение уникальных строковых кодов.
5. Для MySQL 8.0 до 8.0.16 CHECK constraints не обеспечивают полное серверное
   принуждение. Критическое правило публикации продублировано в модели и
   сервисе; точную минорную версию Beget нужно проверить.
6. `mysqlclient` является нативным пакетом. На Beget должны быть доступны
   совместимые клиентские библиотеки или готовый wheel для используемой версии
   Python.
7. Init-скрипты Docker выполняются только при первом создании пустого volume.
   Для изменения схемы инициализации существующий volume нужно пересоздать
   осознанно.

## 10. Git diff --stat

Обычный `git diff --stat` не включает новые untracked-файлы:

```text
 .env.example           | 17 ++++++-----
 .gitignore             |  1 +
 PLAN.md                |  4 ++-
 README.md              | 76 ++++++++++++++++++++++++++++++++++++++++++++++----
 checklists/models.py   | 20 +++++++++----
 checklists/services.py | 38 +++++++++++++++++--------
 config/settings.py     | 21 ++------------
 requirements.txt       |  2 +-
 8 files changed, 131 insertions(+), 48 deletions(-)
```

## 11. Git status

После создания отчёта:

```text
## main
 M .env.example
 M .gitignore
 M PLAN.md
 M README.md
 M checklists/models.py
 M checklists/services.py
 M config/settings.py
 M requirements.txt
?? CODEX_REPORT_STAGE_3_5_MYSQL.md
?? checklists/migrations/0002_remove_checklisttemplateversion_one_published_version_per_template.py
?? checklists/test_mysql_compat.py
?? config/database.py
?? docker-compose.mysql.yml
?? docker/
```

Staging и Git-коммит не выполнялись.
