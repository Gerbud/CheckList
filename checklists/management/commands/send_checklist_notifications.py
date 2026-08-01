from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from checklists.notifications import (
    preview_due_notifications,
    process_due_notifications,
)


class Command(BaseCommand):
    help = 'Планирует и отправляет уведомления о дедлайнах чек-листов.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Показать уведомления без изменений базы и HTTP-запросов.',
        )
        parser.add_argument(
            '--at',
            help='Timezone-aware дата и время ISO 8601 для проверки.',
        )
        parser.add_argument(
            '--limit',
            type=int,
            help='Максимальное количество уведомлений за запуск.',
        )

    def handle(self, *args, **options):
        at = timezone.now()
        if options['at']:
            at = parse_datetime(options['at'])
            if at is None or timezone.is_naive(at):
                raise CommandError(
                    '--at должен быть корректным timezone-aware ISO_DATETIME.'
                )
        limit = options['limit']
        if limit is not None and limit <= 0:
            raise CommandError('--limit должен быть больше нуля.')

        if options['dry_run']:
            previews = preview_due_notifications(at, limit)
            for item in previews:
                create_label = 'new' if item['would_create'] else 'existing'
                self.stdout.write(
                    'WOULD SEND '
                    f"stage={item['stage_id']} "
                    f"type={item['notification_type']} "
                    f"scheduled_for={item['scheduled_for'].isoformat()} "
                    f"chat={item['chat_id']} {create_label}"
                )
            result = {
                'created': sum(item['would_create'] for item in previews),
                'sent': 0,
                'skipped': 0,
                'failed': 0,
            }
        else:
            result = process_due_notifications(at, limit)

        self.stdout.write(
            ' '.join(
                f'{name}={result[name]}'
                for name in ('created', 'sent', 'skipped', 'failed')
            )
        )
