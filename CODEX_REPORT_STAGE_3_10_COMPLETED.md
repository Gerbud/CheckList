# CODEX REPORT — Stage 3.10 Completed

Дата: 2026-07-18

## Итог

Реализованы управление авторами и удалением задач, полный список
пользователей с защитой главного администратора, связь web-пользователя с
Telegram, логотип магазина и журнал Telegram-сообщений с удалением через Bot
API.

Git-коммит не создавался.

## Preflight и база данных

В исходном состоянии management-команда `deployment_check` отсутствовала, а
локальный `.env` не содержал `DATABASE_ENGINE`. Добавлена read-only команда
`python manage.py deployment_check`, которая проверяет:

- backend `django.db.backends.mysql`;
- наличие `MYSQL_DATABASE`;
- наличие `MYSQL_USER`;
- наличие `MYSQL_PASSWORD`;
- наличие `MYSQL_HOST`;
- наличие `MYSQL_PORT`;
- системные deployment checks Django.

Проверка выполнена с временными несекретными переменными окружения и
`DATABASE_ENGINE=mysql`. Production-секреты в `.env` не записывались. Локальная
разработка и тесты продолжают использовать SQLite.

Структура базы менялась только через Django migration.

## Миграция

Создана и применена:

`checklists/migrations/0019_rename_created_by_user_storeadhoctask_created_by_and_more.py`

Операции:

- `StoreAdHocTask.created_by_user` переименовано в `created_by` с сохранением
  существующих данных;
- добавлено `Store.logo`;
- добавлены `TelegramOutboundMessage.deleted_at` и `deleted_by`;
- добавлен статус исходящего сообщения `deleted`;
- создана модель `TelegramUserProfile`;
- обновлены choices `AuditLog.action`.

Поле `TelegramOutboundMessage.telegram_message_id` уже существовало и уже
сохранялось после успешного ответа Telegram, поэтому повторное поле не
создавалось.

## Задачи

- При создании задачи через web `created_by` всегда равен `request.user`.
- При создании задачи через Telegram `created_by` равен связанному
  web-пользователю.
- Автор отображается в списке и карточке задачи.
- Директор видит задачи своего магазина.
- Директор может редактировать и удалять только созданные им задачи.
- Попытка редактирования или удаления чужой задачи возвращает HTTP 403.
- Superuser получил раздел «Все задачи» и может редактировать или удалять
  задачу любого магазина.
- Удаление выполняется через POST после страницы подтверждения и записывается
  в `AuditLog`.

## Пользователи

- Список строится по `User`, а не только по `EmployeeProfile`, поэтому
  показывает superuser, включая `Bud`.
- Добавлена явная роль «Главный администратор».
- Пользователи с `EmployeeProfile` показываются как системные администраторы,
  директора или сотрудники.
- Для superuser и пользователя с именем `Bud` кнопка удаления отсутствует.
- Backend возвращает HTTP 403 при попытке удаления защищённого пользователя.
- Остальные пользователи, включая обычного системного администратора,
  удаляются через страницу подтверждения.
- Удаление пользователя фиксируется в `AuditLog`.

## TelegramUserProfile и создание задач

Создана модель `TelegramUserProfile`:

- `user` — OneToOneField;
- `telegram_user_id` — уникальный BigIntegerField;
- `telegram_chat_id`;
- `telegram_username`;
- `first_name`;
- `last_name`;
- `is_verified`;
- `created_at`;
- `updated_at`.

При подтверждении Telegram-привязки администратор может выбрать web-пользователя.
Связь сохраняется одновременно в `TelegramStoreBinding.user` и
`TelegramUserProfile`.

Команда `/start` синхронизирует подтверждённый Telegram-профиль. Команды
`/task`, `/newtask` и task callbacks не запускают создание задачи без
web-пользователя и возвращают:

`Ваш Telegram не привязан к аккаунту.`

## Логотип магазина

- В `Store` добавлен `ImageField`.
- `upload_to`: `stores/logo/`.
- Полный путь относительно проекта: `media/stores/logo/`.
- В формы создания и изменения магазина добавлена загрузка файла.
- Формы используют `multipart/form-data`.
- Добавлены `MEDIA_ROOT` и `MEDIA_URL`.
- При наличии логотипа он показывается в шапке; иначе остаётся текст
  «Чек-лист».
- `media/` добавлена в `.gitignore`.
- Добавлена зависимость `Pillow==11.3.0`.

На production каталог `media/` должен быть постоянным и обслуживаться
web-сервером.

## Telegram сообщения

- В Telegram client добавлен метод `deleteMessage`.
- После успешной отправки сохраняется `telegram_message_id`.
- Для отправленного сообщения с `telegram_message_id` доступна кнопка
  «Удалить в Telegram».
- Удаление передаёт `chat_id` и `message_id` в Bot API.
- После успеха сохраняются:
  - `status=deleted`;
  - `deleted_at`;
  - `deleted_by`.
- Журнал исходящих сообщений получил фильтры:
  - пользователь;
  - магазин;
  - дата от/до;
  - статус.
- В журнале показываются магазин, получатель, тип, статус, message ID и ошибка.

## Основные изменённые файлы этапа

- `requirements.txt`
- `.gitignore`
- `config/settings.py`
- `config/urls.py`
- `checklists/models.py`
- `checklists/ad_hoc_tasks.py`
- `checklists/access_control.py`
- `checklists/management_services.py`
- `checklists/portal_forms.py`
- `checklists/portal_views.py`
- `checklists/portal_context.py`
- `checklists/telegram_bot.py`
- `checklists/telegram_client.py`
- `checklists/telegram_queue.py`
- `checklists/telegram_services.py`
- `checklists/telegram_views.py`
- `checklists/urls.py`
- `checklists/management/commands/deployment_check.py`
- `checklists/migrations/0019_rename_created_by_user_storeadhoctask_created_by_and_more.py`
- `checklists/test_stage_3_10.py`
- `checklists/test_telegram_integration.py`
- шаблоны кабинетов, задач, пользователей и Telegram.

## Тесты

Добавлены проверки:

- superuser удаляет любую задачу;
- директор удаляет свою задачу;
- директор получает HTTP 403 при удалении чужой задачи;
- `Bud` отображается как главный администратор;
- `Bud`/superuser нельзя удалить;
- обычного системного администратора можно удалить;
- связанный Telegram-пользователь создаёт задачу;
- `created_by` Telegram-задачи заполнен;
- неподтверждённый пользователь не может начать создание задачи;
- `TelegramUserProfile` создаётся и подтверждается;
- удаление сообщения вызывает `deleteMessage`;
- сохраняются `deleted`, `deleted_at`, `deleted_by`;
- логотип магазина загружается и отображается.

Результаты:

- `python manage.py makemigrations --check --dry-run` — `No changes detected`;
- `python manage.py migrate` — migration `0019` применена успешно;
- `python manage.py check` — ошибок нет;
- `pytest -q` — `319 passed`;
- `pip check` — `No broken requirements found`;
- `git diff --check` — ошибок нет.

Остаётся одно предупреждение совместимости Django 6.0 о будущем изменении
схемы по умолчанию для `forms.URLField`; для Django 5.2 оно не является
ошибкой.

`deployment_check` также выводит рекомендации Django включить
`SECURE_SSL_REDIRECT`, secure cookies и HSTS. Команда проходит, поскольку это
deployment warnings, но перед production-запуском настройки HTTPS нужно
согласовать с конфигурацией Passenger/Beget.

## Deployment

После доставки файлов на Beget:

```bash
source /home/a/autobud/checklist/public_html/django_venv/bin/activate
cd /home/a/autobud/checklist/public_html/django_app
python manage.py deployment_check
python manage.py migrate
python manage.py check
python manage.py register_telegram_commands
touch /home/a/autobud/checklist/public_html/tmp/restart.txt
```

В production `.env` обязательно должно быть:

```text
DATABASE_ENGINE=mysql
MYSQL_DATABASE=...
MYSQL_USER=...
MYSQL_PASSWORD=...
MYSQL_HOST=...
MYSQL_PORT=3306
```

Также нужно настроить постоянный каталог `media/` и его раздачу web-сервером.
