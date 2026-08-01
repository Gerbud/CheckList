from datetime import time

from django.core.management.base import BaseCommand
from django.db import transaction

from checklists.models import (
    ChecklistItem,
    ChecklistSection,
    ChecklistTemplate,
    ChecklistTemplateVersion,
    Store,
    StoreChecklistSchedule,
)
from checklists.services import publish_template_version


SECTIONS = (
    (
        'opening',
        'Открытие магазина',
        1,
        (
            (1, 'Я нахожусь в рабочей форме, внешний вид соответствует стандарту.'),
            (2, 'Входная зона, двери, витрины и торговый зал чистые.'),
            (3, 'Товар, оборудование и рабочие поверхности чистые.'),
            (4, 'Рабочее место и кассовая зона приведены в порядок.'),
            (5, 'Доступный товар выставлен, пустые места на витрине устранены.'),
            (6, 'На всём товаре есть актуальные и корректные ценники.'),
            (7, 'Заказы клиентов, готовые к выдаче, проверены и подготовлены.'),
        ),
    ),
    (
        'during_day',
        'В течение дня',
        2,
        (
            (8, 'Каждый посетитель получил приветствие и предложение помощи.'),
            (
                9,
                'Свободное от общения с клиентами время используется для рабочих задач.',
            ),
            (
                10,
                'Все обращения и вопросы клиентов обработаны или переданы ответственному.',
            ),
            (
                11,
                'В рабочем чате предоставлены ответы на все вопросы и обращения.',
            ),
            (
                12,
                'Заказы на перемещение собраны, проверены и готовы к отгрузке.',
            ),
            (
                13,
                'Поступивший товар принят, проверен и размещён; расхождения зафиксированы.',
            ),
        ),
    ),
    (
        'closing',
        'Закрытие смены',
        3,
        (
            (
                14,
                'Необработанных заказов, обращений и задач без ответственного не осталось.',
            ),
            (
                15,
                'По незакрытым вопросам указаны ответственный, следующий шаг и срок.',
            ),
            (16, 'Касса и документы сверены, смена закрыта корректно.'),
            (
                17,
                'Торговый зал, склад, кассовая зона и рабочее место оставлены в порядке.',
            ),
            (
                18,
                'Итоги смены и важные комментарии переданы в рабочий чат.',
            ),
        ),
    ),
)


class Command(BaseCommand):
    help = 'Создаёт начальный опубликованный чек-лист магазина «5 Планет».'

    @transaction.atomic
    def handle(self, *args, **options):
        store, store_created = Store.objects.get_or_create(
            code='5-planets',
            defaults={
                'name': '5 Планет',
                'timezone': 'Europe/Moscow',
                'is_active': True,
            },
        )
        _, schedule_created = StoreChecklistSchedule.objects.get_or_create(
            store=store,
            defaults={
                'opening_time': time(9),
                'morning_deadline': time(11),
                'daytime_deadline': time(20),
                'closing_deadline': time(22),
                'warning_minutes_before': 30,
                'notifications_enabled': True,
                'is_active': True,
            },
        )
        template, template_created = ChecklistTemplate.objects.get_or_create(
            store=store,
            name='Ежедневный чек-лист сотрудника',
            defaults={'is_active': True},
        )
        version, version_created = ChecklistTemplateVersion.objects.get_or_create(
            template=template,
            version_number=1,
            defaults={'status': ChecklistTemplateVersion.Status.DRAFT},
        )

        created_sections = 0
        created_items = 0
        for code, name, sort_order, items in SECTIONS:
            section, section_created = ChecklistSection.objects.get_or_create(
                version=version,
                code=code,
                defaults={'name': name, 'sort_order': sort_order},
            )
            created_sections += int(section_created)
            for item_sort_order, text in items:
                _, item_created = ChecklistItem.objects.get_or_create(
                    section=section,
                    sort_order=item_sort_order,
                    defaults={
                        'text': text,
                        'is_active': True,
                        'comment_required_on_failure': True,
                        'allow_not_applicable': False,
                    },
                )
                created_items += int(item_created)

        published = False
        if version.status == ChecklistTemplateVersion.Status.DRAFT:
            publish_template_version(version, actor=None)
            published = True

        if any(
            (
                store_created,
                schedule_created,
                template_created,
                version_created,
                created_sections,
                created_items,
                published,
            )
        ):
            self.stdout.write(
                self.style.SUCCESS(
                    'Начальный чек-лист создан: '
                    f'{created_sections} разделов, {created_items} пунктов.'
                )
            )
        else:
            self.stdout.write(
                self.style.SUCCESS('Начальный чек-лист уже существует; изменений нет.')
            )
