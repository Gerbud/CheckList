from datetime import timedelta

from django.conf import settings
from django.utils.formats import date_format
from django.utils import timezone

from checklists.ad_hoc_tasks import DAILY_TO_AD_HOC_SECTION
from checklists.calendar_services import iter_month_dates
from checklists.models import (
    AuditLog,
    ChecklistAnswer,
    ChecklistDayStatus,
    ChecklistItem,
    DailyChecklistStage,
    DailyShiftAssignment,
    Store,
    StoreAdHocTask,
    StoreEmployee,
    TelegramUserProfile,
    UserStoreMembership,
)
from checklists.telegram_queue import (
    enqueue_telegram_message,
    enqueue_template_message,
)


FINAL_STAGE_STATUSES = {
    DailyChecklistStage.Status.COMPLETED,
    DailyChecklistStage.Status.COMPLETED_LATE,
}


def _counts(stage):
    answers = ChecklistAnswer.objects.filter(
        daily_item__daily_checklist=stage.daily_checklist,
        daily_item__section_code=stage.section_code,
        daily_item__is_required=True,
    ).select_related('daily_item')
    remaining = 0
    failed = 0
    for answer in answers:
        if (
            answer.daily_item.answer_type_snapshot == ChecklistItem.AnswerType.INTEGER
            and answer.integer_value is None
        ) or (
            answer.daily_item.answer_type_snapshot == ChecklistItem.AnswerType.STATUS
            and answer.status == ChecklistAnswer.Status.PENDING
        ):
            remaining += 1
        if answer.status == ChecklistAnswer.Status.FAILED:
            failed += 1
    return remaining, failed


def _context(stage, remaining, failed):
    daily = stage.daily_checklist
    task_details = []
    for task in StoreAdHocTask.objects.filter(
        store=daily.store,
        date=daily.checklist_date,
        section_code=DAILY_TO_AD_HOC_SECTION[stage.section_code],
        status__in=(
            StoreAdHocTask.Status.PLANNED,
            StoreAdHocTask.Status.ACTIVE,
            StoreAdHocTask.Status.FAILED,
        ),
    ).select_related('completed_by_employee'):
        employee = (
            task.completed_by_employee.display_name
            if task.completed_by_employee_id
            else 'не указан'
        )
        comment = task.completion_comment or 'без комментария'
        task_details.append(
            f'• {task.text}; сотрудник: {employee}; комментарий: {comment}'
        )
    return {
        'store_name': daily.store.name,
        'date': daily.checklist_date.strftime('%d.%m.%Y'),
        'stage_name': stage.get_section_code_display(),
        'deadline': stage.deadline_at.isoformat(timespec='minutes'),
        'remaining_count': remaining,
        'failed_count': failed,
        'checklist_url': '/checklist/today/',
        'task_url': '/checklist/today/',
        'task_text': '',
        'task_description': '',
        'employee_name': '',
        'comment': '\n'.join(task_details),
    }


def schedule_telegram_notifications(*, at=None, store_code=None):
    at = at or timezone.now()
    stages = DailyChecklistStage.objects.select_related(
        'daily_checklist__store'
    ).filter(daily_checklist__store__is_active=True).exclude(
        daily_checklist__day_status=ChecklistDayStatus.DAY_OFF
    )
    if store_code:
        stages = stages.filter(daily_checklist__store__code=store_code)
    created = 0
    for stage in stages.iterator():
        store = stage.daily_checklist.store
        remaining, failed = _counts(stage)
        base_key = (
            f'reminder:{store.pk}:{stage.daily_checklist.checklist_date}:'
            f'{stage.section_code}'
        )
        candidates = []
        if stage.status in FINAL_STAGE_STATUSES:
            candidates.append('stage_closed')
        elif at >= stage.deadline_at:
            candidates.append('stage_overdue')
        else:
            seconds_left = (stage.deadline_at - at).total_seconds()
            if seconds_left <= 30 * 60:
                candidates.append('stage_reminder_30')
            if seconds_left <= 10 * 60:
                candidates.append('stage_reminder_10')
        if remaining or failed:
            if stage.status in FINAL_STAGE_STATUSES or at >= stage.deadline_at:
                candidates.append('incomplete_tasks')
        for message_type in candidates:
            messages = enqueue_template_message(
                store,
                message_type,
                _context(stage, remaining, failed),
                idempotency_key=f'{base_key}:{message_type}',
            )
            created += len(
                [message for message in messages if message.was_created]
            )
    return created


def schedule_employee_schedule_reminders(*, at=None, store_code=None):
    at = at or timezone.now()
    today = timezone.localdate(at)
    next_month = (today.replace(day=28) + timedelta(days=4)).replace(day=1)
    if (next_month - today).days != 3:
        return 0

    stores = Store.objects.filter(is_active=True)
    if store_code:
        stores = stores.filter(code=store_code)
    created = 0
    for store in stores.iterator():
        required_dates = set(iter_month_dates(next_month))
        employees = list(
            StoreEmployee.objects.filter(
                store=store,
                is_active=True,
            ).order_by('sort_order', 'display_name')
        )
        assigned_pairs = set(
            DailyShiftAssignment.objects.filter(
                store=store,
                work_date__year=next_month.year,
                work_date__month=next_month.month,
            ).values_list('employee_id', 'work_date')
        )
        incomplete_employees = [
            employee
            for employee in employees
            if any(
                (employee.pk, work_date) not in assigned_pairs
                for work_date in required_dates
            )
        ]
        if not incomplete_employees:
            continue
        profiles = TelegramUserProfile.objects.filter(
            is_verified=True,
            user__is_active=True,
            user__store_memberships__store=store,
            user__store_memberships__is_active=True,
            user__store_memberships__role_in_store__in=(
                UserStoreMembership.Role.DIRECTOR,
                UserStoreMembership.Role.ADMINISTRATOR,
            ),
        ).distinct()
        queued_for_store = 0
        employee_lines = '\n'.join(
            f'- {employee.display_name}'
            for employee in incomplete_employees
        )
        calendar_url = (
            f'{settings.SITE_URL}/director/shifts/bulk-create/'
            f'?month={next_month:%Y-%m}'
        )
        text = (
            f'График сотрудников на '
            f'{date_format(next_month, "F Y").lower()} '
            'заполнен не полностью.\n'
            f'Магазин: {store.name}\n'
            'Не заполнены:\n'
            f'{employee_lines}\n\n'
            f'Перейти к заполнению:\n{calendar_url}'
        )
        for profile in profiles:
            message = enqueue_telegram_message(
                chat_id=profile.telegram_chat_id,
                store=store,
                method='sendMessage',
                payload={'text': text},
                message_type='employee_schedule_missing',
                idempotency_key=(
                    f'employee-schedule-missing:{store.pk}:'
                    f'{next_month:%Y-%m}:{profile.pk}'
                ),
            )
            if message.was_created:
                created += 1
                queued_for_store += 1
        if queued_for_store:
            AuditLog.objects.create(
                actor=None,
                store=store,
                object_type=Store._meta.label_lower,
                object_id=str(store.pk),
                action=AuditLog.Action.EMPLOYEE_SCHEDULE_REMINDER_QUEUED,
                new_value={
                    'month': next_month.isoformat(),
                    'incomplete_employee_ids': [
                        employee.pk for employee in incomplete_employees
                    ],
                    'messages_created': queued_for_store,
                },
            )
    return created
