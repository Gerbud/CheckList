from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Max

from checklists.management_services import _clone_published_version
from checklists.models import (
    ChecklistItem,
    ChecklistSection,
    DailyChecklistStage,
    Store,
)
from checklists.services import publish_template_version


DESCRIPTION = 'Введите текущее количество заказов в указанном статусе'
QUESTION_TEXTS = (
    'Сколько заказов находится в статусе „Готов к отгрузке“?',
    'Сколько заказов находится в статусе „Ожидает поставки“?',
)
QUESTION_SPECS = tuple(
    (section_code, text)
    for section_code in (
        DailyChecklistStage.SectionCode.OPENING,
        DailyChecklistStage.SectionCode.CLOSING,
    )
    for text in QUESTION_TEXTS
)


class Command(BaseCommand):
    help = 'Добавляет четыре числовых вопроса о количестве заказов.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--store-code',
            default='5',
            help='Точный код магазина (по умолчанию: 5).',
        )

    @transaction.atomic
    def handle(self, *args, **options):
        store_code = options['store_code'].strip()
        try:
            store = Store.objects.select_for_update().get(code=store_code)
        except Store.DoesNotExist as exc:
            raise CommandError(
                f'Магазин с кодом «{store_code}» не найден.'
            ) from exc

        current = list(
            ChecklistItem.objects.filter(
                section__version__template__store=store,
                section__version__status='published',
            ).select_related('section')
        )
        existing = {
            (item.section.code, item.answer_type, item.text)
            for item in current
        }
        missing = [
            (section_code, text)
            for section_code, text in QUESTION_SPECS
            if (
                section_code,
                ChecklistItem.AnswerType.INTEGER,
                text,
            )
            not in existing
        ]
        if not missing:
            self.stdout.write(
                self.style.SUCCESS('Все числовые вопросы уже существуют.')
            )
            return

        _, draft, _ = _clone_published_version(store, actor=None)
        next_orders = {
            section.code: (
                section.items.aggregate(value=Max('sort_order'))['value'] or 0
            )
            for section in draft.sections.prefetch_related('items')
        }
        for section_code, text in missing:
            try:
                section = draft.sections.get(code=section_code)
            except ChecklistSection.DoesNotExist as exc:
                raise CommandError(
                    f'В шаблоне нет этапа «{section_code}».'
                ) from exc
            next_orders[section_code] += 1
            ChecklistItem.objects.create(
                section=section,
                text=text,
                description=DESCRIPTION,
                sort_order=next_orders[section_code],
                is_active=True,
                is_required=True,
                answer_type=ChecklistItem.AnswerType.INTEGER,
                allow_not_applicable=False,
                comment_required_on_failure=False,
            )

        publish_template_version(draft, actor=None)
        self.stdout.write(
            self.style.SUCCESS(
                f'Добавлено числовых вопросов: {len(missing)}.'
            )
        )
