# Store Checklist — отчёт по Stage 3.11.1

Дата завершения: 2026-07-18

## Результат

Реализована единая связь пользователей с магазинами через промежуточную
модель `UserStoreMembership`. Один пользователь по-прежнему имеет не более
одного `TelegramUserProfile`, но теперь может работать в нескольких магазинах
с отдельной ролью в каждом магазине.

Существующие `EmployeeProfile.store` и `TelegramStoreBinding.store` сохранены
для обратной совместимости. Новая миграция переносит их данные в
`UserStoreMembership`, не удаляя исходные связи.

## Резервная копия перед изменениями

Перед изменением моделей создана локальная резервная копия:

`backups/db.sqlite3.stage_3_11_1.pre_change.20260718.bak`

- проверка целостности SQLite: `ok`;
- SHA-256:
  `2030bfc8db704ef6116289bf28963fd5ecd2dcb7514836a08022cd4a696f48e3`;
- каталог `backups/` добавлен в `.gitignore`.

Резервная копия не должна попадать в Git.

## Новые модели

### UserStoreMembership

Промежуточная модель many-to-many `User ↔ Store`:

- `user`;
- `store`;
- `role_in_store`: директор, сотрудник или администратор;
- `is_active`;
- `created_at`.

Дополнительно реализованы:

- уникальность пары `user + store`;
- индекс по `user + is_active`;
- индекс по `store + role_in_store + is_active`;
- запрет активной связи с неактивным магазином;
- обратные связи `user.store_memberships`, `user.stores`,
  `store.user_memberships` и `store.users`.

Другие новые модели на этом этапе не создавались.

## Миграции

Создана и применена:

`checklists/migrations/0020_alter_auditlog_action_userstoremembership_and_more.py`

Миграция:

1. создаёт `UserStoreMembership`;
2. добавляет `Store.users` через эту промежуточную модель;
3. добавляет индексы и уникальное ограничение;
4. дополняет действия `AuditLog`;
5. переносит существующие связи:
   - директор магазина → роль `director`;
   - учётная запись магазина → роль `employee`;
   - остальные профили с магазином → роль `employee`;
   - связь из `TelegramStoreBinding` → соответствующая связь с магазином;
   - Telegram-связь с пользователем → `TelegramUserProfile`, если нет
     конфликта Telegram ID.

Локальный результат:

```text
Applying checklists.0020_... OK
```

После повторной проверки все миграции `checklists` с `0001` по `0020`
отмечены как применённые. Две существующие локальные связи пользователей с
магазинами сохранены как две записи `UserStoreMembership`.

## Реализованные правила

### Пользователи и магазины

- у пользователя может быть несколько активных связей с магазинами;
- роль назначается отдельно для каждого магазина;
- директор и администратор магазина получают доступ к директорским операциям
  соответствующего магазина;
- сотрудник такого доступа не получает;
- создание и удаление связей журналируется в `AuditLog`;
- редактирование прежнего основного магазина не удаляет дополнительные связи.

### Telegram

- `TelegramUserProfile` остаётся связью один-к-одному с пользователем;
- экран Telegram показывает пользователя, username, chat ID и все его
  магазины;
- системный администратор может перепривязать профиль другому пользователю
  или отвязать его;
- карточка пользователя показывает Telegram-статус и список магазинов;
- из карточки можно добавлять и удалять магазинные связи;
- `/task <текст>` при одной активной связи сразу создаёт задачу;
- при нескольких магазинах бот показывает inline-выбор магазина;
- выбранная связь повторно проверяется по пользователю, активности связи и
  активности магазина;
- Telegram-задача сохраняет `created_by`, источник `Telegram` и исходную
  Telegram-привязку.

`TelegramStoreBinding.store` оставлен как совместимое основное значение для
старых команд, уведомлений и существующей очереди. Для выбора магазина при
быстром создании задач используется `UserStoreMembership`.

### Удаление задач

- системный администратор может удалить любую задачу;
- директор или администратор магазина может удалить задачу своего магазина,
  независимо от её автора;
- сотрудник удалять задачи не может;
- доступ к задаче другого магазина не предоставляется;
- перед удалением задача блокируется через `select_for_update`;
- удаление записывается в `AuditLog` вместе с данными удалённой задачи.

### Главный администратор Bud

- Bud остаётся видимым в общем списке пользователей;
- элементы удаления и отключения скрыты в интерфейсе;
- сервисы запрещают удаление и отключение;
- Django-сигналы запрещают прямое отключение, переименование и удаление Bud
  через ORM;
- superuser также защищён от удаления и отключения сервисами управления.

## Изменённые файлы Stage 3.11.1

- `.gitignore`;
- `checklists/models.py`;
- `checklists/migrations/0020_alter_auditlog_action_userstoremembership_and_more.py`;
- `checklists/admin.py`;
- `checklists/access_control.py`;
- `checklists/management_services.py`;
- `checklists/portal_forms.py`;
- `checklists/portal_views.py`;
- `checklists/urls.py`;
- `checklists/signals.py`;
- `checklists/ad_hoc_tasks.py`;
- `checklists/telegram_services.py`;
- `checklists/telegram_views.py`;
- `checklists/telegram_bot.py`;
- `templates/checklists/system_admin/user_detail.html`;
- `templates/checklists/telegram/users.html`;
- `checklists/test_stage_3_10.py`;
- `checklists/test_stage_3_11_1.py`;
- `CODEX_REPORT_STAGE_3_11_1_COMPLETED.md`.

Рабочее дерево до начала Stage 3.11.1 уже содержало незакоммиченные изменения
предыдущих этапов. Они не откатывались и не перезаписывались.

## Тесты и проверки

Добавлено 9 тестов Stage 3.11.1. Они проверяют:

- немедленное создание Telegram-задачи для одного магазина;
- выбор магазина для пользователя с двумя магазинами;
- удаление задач директором в своём магазине;
- запрет директору доступа к задаче чужого магазина;
- удаление любой задачи системным администратором;
- запрет удаления сотрудником;
- защиту Bud на уровне сервиса и ORM;
- добавление и удаление нескольких связей в карточке пользователя;
- перепривязку и отключение Telegram-профиля.

Итоговые результаты:

```text
python manage.py makemigrations --check --dry-run
No changes detected

python manage.py check
System check identified no issues (0 silenced).

pytest -q
328 passed, 1 warning in 60.55s

pip check
No broken requirements found.

git diff --check
Ошибок нет.
```

Предупреждение pytest относится к будущему изменению схемы URL по умолчанию
для `forms.URLField` в Django 6.0 и не влияет на Django 5.2.

Проверка production-настроек с `DATABASE_ENGINE=mysql` пройдена. Django также
показывает четыре рекомендации безопасности: включить HTTPS redirect, secure
cookies для session/CSRF и HSTS. Они не являются ошибками миграции, но должны
быть настроены после подтверждения корректной HTTPS-терминации на Beget.

## Инструкция по выкладке на Beget

1. Остановить операции изменения данных на время миграции или выбрать окно с
   минимальной активностью.
2. Сделать отдельный backup production MySQL через панель Beget либо
   `mysqldump` с параметрами из `.env`. Проверить, что файл backup не пустой и
   доступен для восстановления.
3. Сохранить текущие `.env`, каталог `media/` и конфигурацию Passenger.
4. Загрузить текущие файлы проекта без локальных `.env`, `db.sqlite3`,
   `.venv`, `backups/` и `.mysql-data/`.
5. Активировать production virtualenv и установить зафиксированные
   зависимости:

   ```bash
   cd /home/a/autobud/checklist/public_html/django_app
   source /home/a/autobud/checklist/public_html/django_venv/bin/activate
   pip install -r requirements.txt
   ```

6. До миграции проверить production-окружение:

   ```bash
   python manage.py deployment_check
   python manage.py showmigrations checklists
   python manage.py makemigrations --check --dry-run
   ```

   `deployment_check` должен подтвердить `DATABASE_ENGINE=mysql`.

7. Применить только Django migrations:

   ```bash
   python manage.py migrate
   ```

   Ожидаемая новая миграция:
   `0020_alter_auditlog_action_userstoremembership_and_more`.

8. Выполнить проверки:

   ```bash
   python manage.py check
   python manage.py showmigrations checklists
   python manage.py collectstatic --noinput
   ```

9. Проверить количество перенесённых связей через Django shell, не меняя
   данные:

   ```bash
   python manage.py shell -c "from checklists.models import UserStoreMembership; print(UserStoreMembership.objects.count())"
   ```

10. Перезапустить Passenger:

    ```bash
    touch /home/a/autobud/checklist/public_html/tmp/restart.txt
    ```

11. Провести smoke-проверку:

    - вход Bud и доступность списка пользователей;
    - карточка пользователя с несколькими магазинами;
    - список Telegram-пользователей;
    - `/task Проверить склад` для одного магазина;
    - выбор магазина для пользователя с несколькими связями;
    - удаление задачи директором своего магазина;
    - отсутствие удаления у сотрудника.

При любой ошибке миграции не выполнять ручные изменения таблиц. Сохранить
текст ошибки, остановить выкладку и восстанавливать данные только из
проверенного backup.

## Ограничения и дальнейшие действия

- Старые поля `EmployeeProfile.store` и `TelegramStoreBinding.store` пока
  намеренно сохранены. Их удаление требует отдельного этапа после перевода
  всех старых сценариев на `UserStoreMembership`.
- У одного Telegram ID и одного пользователя допускается только один
  `TelegramUserProfile`.
- Автоматическая очистка старых legacy-связей в этот этап не входит.
- Следующий этап не начинался и требует отдельного подтверждения.
