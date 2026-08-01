import os

from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from checklists.models import EmployeeProfile, Store


DEMO_USERS = (
    ('manager', 'DEMO_MANAGER_PASSWORD', EmployeeProfile.Role.STORE_DIRECTOR, True),
    ('employee', 'DEMO_EMPLOYEE_PASSWORD', None, False),
)


class Command(BaseCommand):
    help = 'Идемпотентно создаёт локальных демонстрационных пользователей.'

    def handle(self, *args, **options):
        passwords = {}
        errors = []
        for username, env_name, _, _ in DEMO_USERS:
            password = os.getenv(env_name)
            if not password:
                errors.append(f'Не задана переменная {env_name}.')
                continue
            try:
                validate_password(password, user=User(username=username))
            except ValidationError as exc:
                errors.append(f'{env_name}: {"; ".join(exc.messages)}')
            passwords[username] = password
        if errors:
            raise CommandError(' '.join(errors))

        try:
            store = Store.objects.get(code='5-planets')
        except Store.DoesNotExist as exc:
            raise CommandError(
                'Магазин «5 Планет» не найден. Сначала выполните seed_checklist.'
            ) from exc

        created_users = 0
        with transaction.atomic():
            for username, _, role, profile_active in DEMO_USERS:
                user, created = User.objects.get_or_create(
                    username=username,
                    defaults={'is_active': True, 'is_staff': False},
                )
                if created:
                    user.set_password(passwords[username])
                    user.save(update_fields=('password',))
                    created_users += 1
                EmployeeProfile.objects.update_or_create(
                    user=user,
                    defaults={
                        'store': store,
                        'role': role,
                        'is_active': profile_active,
                    },
                )

        if created_users:
            self.stdout.write(
                self.style.SUCCESS(
                    f'Созданы демонстрационные пользователи: {created_users}.'
                )
            )
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    'Демонстрационные пользователи уже существуют; изменений нет.'
                )
            )
