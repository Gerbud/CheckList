# Store Checklist — Deployment Notes

Дата фиксации:
2026-07-18

## Продакшен

Проект:
Store Checklist

Стек:
- Django 5.2
- Python 3.10
- Passenger
- MySQL 8.0
- Beget


## Пути

Проект:

/home/a/autobud/checklist/public_html/django_app

Virtualenv:

/home/a/autobud/checklist/public_html/django_venv

Passenger:

/home/a/autobud/checklist/public_html/passenger_wsgi.py


## База данных

ВАЖНО:

Продакшен работает только через MySQL.

Проверка:

python manage.py shell -c "
from django.conf import settings
print(settings.DATABASES)
"


Ожидаемо:

ENGINE:
django.db.backends.mysql


База:

autobud_checkl


Файл .env:

DATABASE_ENGINE=mysql

MYSQL_DATABASE=autobud_checkl
MYSQL_USER=autobud_checkl
MYSQL_HOST=localhost


НЕ переключать DATABASE_ENGINE на sqlite.


## Backup

Перед изменениями обязательно:

1. Backup базы:

mysqldump \
-u autobud_checkl \
-p \
--no-tablespaces \
autobud_checkl > backup.sql


2. Backup проекта:

tar -czf project_backup.tar.gz django_app


## Миграции

Перед изменениями:

python manage.py showmigrations


После изменений:

python manage.py migrate


Проверка:

python manage.py check


## Перезапуск Passenger

После изменений:

touch /home/a/autobud/checklist/public_html/tmp/restart.txt


## Telegram

Рабочая схема:

Telegram
↓
Webhook
↓
/telegram/webhook/
↓
TelegramUpdateLog
↓
обработка
↓
TelegramOutboundMessage
↓
Telegram API


Webhook:

https://checklist.es-helper.ru/telegram/webhook/


Gateway:

https://tauto.gerbud.ru


Проверка:

python manage.py shell -c "
from checklists.telegram_client import send_telegram_request
print(send_telegram_request('getWebhookInfo', {}))
"


Если webhook сломался:

delete:

send_telegram_request('deleteWebhook', {})


set заново через setWebhook.


## Пользователи

Главный администратор:

Bud

Права:

superuser=True
staff=True


Главного администратора нельзя удалять через интерфейс.


## Текущие миграции

Последняя проверенная:

0018_telegram_update_response_tracking


## Правила разработки

Перед любыми изменениями:

1. Проверить базу.
2. Сделать backup.
3. Проверить миграции.
4. Не изменять структуру БД вручную без необходимости.


