# CODEX_REPORT_STAGE_3_9_STABLE

Дата: 2026-07-18

## Рабочее состояние

Проект:
Store Checklist Django

Окружение:
- Django 5.2
- Python 3.10
- MySQL 8.0 Beget
- Passenger

## База данных

ВАЖНО:

Продакшен использует MySQL.

Параметры берутся из:
.env

DATABASE_ENGINE=mysql

Нельзя возвращаться на SQLite автоматически.

Перед любыми изменениями:
- проверять settings.DATABASES
- проверять DATABASE_ENGINE
- делать backup базы


## Текущая база

Рабочая база:
autobud_checkl

Содержит:
- пользователей
- вопросы
- настройки магазинов
- Telegram настройки
- задачи


## Пользователи

Главный пользователь:

Bud

Права:
- superuser=True
- staff=True

Нельзя удалять через интерфейс.


## Telegram

Работает через:

Webhook:
https://checklist.es-helper.ru/telegram/webhook/

Gateway:
https://tauto.gerbud.ru


Схема:

Telegram
→ webhook
→ TelegramUpdateLog
→ обработка
→ TelegramOutboundMessage


Cron используется только как резерв.


## Миграции

Последняя примененная:

0018_telegram_update_response_tracking


## Запрещено

Не менять:
- БД без миграции
- структуру Telegram очередей без проверки
- настройки webhook без проверки


