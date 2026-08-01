import os

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from checklists.models import (
    EmployeeProfile,
    Store,
    StoreEmployee,
    StoreTerminalAccount,
)


DEMO_EMPLOYEES = (
    ('Иван', 'Петров', 'Иван Петров', 10),
    ('Анна', 'Смирнова', 'Анна Смирнова', 20),
    ('Мария', 'Иванова', 'Мария Иванова', 30),
)


class Command(BaseCommand):
    help = 'Идемпотентно создаёт общий терминальный аккаунт магазина.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--with-demo-employees',
            action='store_true',
            help='Создать демонстрационных сотрудников только при DEBUG=True.',
        )

    @transaction.atomic
    def handle(self, *args, **options):
        username = os.getenv('STORE_TERMINAL_USERNAME', '').strip()
        password = os.getenv('STORE_TERMINAL_PASSWORD', '')
        if not username or not password:
            raise CommandError(
                'Задайте STORE_TERMINAL_USERNAME и STORE_TERMINAL_PASSWORD.'
            )
        try:
            validate_password(password)
        except ValidationError as exc:
            raise CommandError('Пароль терминала не прошёл проверку сложности.') from exc
        try:
            store = Store.objects.get(code='5-planets')
        except Store.DoesNotExist as exc:
            raise CommandError('Сначала выполните seed_checklist.') from exc

        User = get_user_model()
        user, user_created = User.objects.get_or_create(
            username=username,
            defaults={
                'is_active': True,
                'is_staff': False,
                'is_superuser': False,
            },
        )
        if user.is_staff or user.is_superuser:
            raise CommandError(
                'Выбранный пользователь имеет staff/admin права и не может '
                'быть терминалом.'
            )
        if user_created:
            user.set_password(password)
            user.save(update_fields=('password',))

        EmployeeProfile.objects.update_or_create(
            user=user,
            defaults={
                'store': store,
                'role': EmployeeProfile.Role.STORE_ACCOUNT,
                'is_active': True,
            },
        )

        terminal, terminal_created = StoreTerminalAccount.objects.get_or_create(
            store=store,
            defaults={'user': user, 'is_active': True},
        )
        if terminal.user_id != user.pk:
            raise CommandError(
                'У магазина уже настроен другой терминальный пользователь.'
            )

        employees_created = 0
        if options['with_demo_employees']:
            if not settings.DEBUG:
                raise CommandError(
                    'Демонстрационные сотрудники разрешены только при DEBUG=True.'
                )
            for first_name, last_name, display_name, sort_order in DEMO_EMPLOYEES:
                _, created = StoreEmployee.objects.get_or_create(
                    store=store,
                    display_name=display_name,
                    defaults={
                        'first_name': first_name,
                        'last_name': last_name,
                        'sort_order': sort_order,
                        'is_active': True,
                    },
                )
                employees_created += int(created)

        self.stdout.write(
            self.style.SUCCESS(
                'Терминал готов: '
                f'user_created={int(user_created)} '
                f'terminal_created={int(terminal_created)} '
                f'employees_created={employees_created}'
            )
        )
