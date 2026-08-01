import os

from django.conf import settings
from django.core.checks import ERROR, run_checks
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = 'Проверяет обязательные настройки production deployment.'

    def handle(self, *args, **options):
        engine = settings.DATABASES['default']['ENGINE']
        if engine != 'django.db.backends.mysql':
            raise CommandError(
                'Production требует DATABASE_ENGINE=mysql; '
                f'текущий backend: {engine}.'
            )
        required = (
            'MYSQL_DATABASE',
            'MYSQL_USER',
            'MYSQL_PASSWORD',
            'MYSQL_HOST',
            'MYSQL_PORT',
        )
        missing = [name for name in required if not os.getenv(name, '').strip()]
        if missing:
            raise CommandError(
                'Не заданы обязательные переменные: ' + ', '.join(missing)
            )
        issues = run_checks(include_deployment_checks=True)
        errors = [issue for issue in issues if issue.level >= ERROR]
        for issue in issues:
            self.stdout.write(str(issue))
        if errors:
            raise CommandError(
                f'Deployment check обнаружил ошибок: {len(errors)}.'
            )
        self.stdout.write(
            self.style.SUCCESS(
                'Deployment check пройден: DATABASE_ENGINE=mysql, '
                'обязательные переменные заданы.'
            )
        )
