import calendar
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from checklists.access_control import can_manage_store_schedule
from checklists.exceptions import OperationNotAllowedError
from checklists.models import (
    AuditLog,
    ChecklistDayStatus,
    ChecklistNotification,
    DailyChecklist,
    StoreChecklistSchedule,
    StoreDayStatus,
)


EXCLUDED_FROM_STATISTICS = {
    ChecklistDayStatus.TESTING,
    ChecklistDayStatus.DAY_OFF,
    ChecklistDayStatus.EMERGENCY,
}


def get_store_day_status(store, work_date):
    override = StoreDayStatus.objects.filter(
        store=store,
        date=work_date,
    ).values_list('status', flat=True).first()
    if override:
        return override
    schedule, _ = StoreChecklistSchedule.objects.get_or_create(store=store)
    if work_date.weekday() not in schedule.working_weekdays:
        return ChecklistDayStatus.DAY_OFF
    return ChecklistDayStatus.NORMAL


def day_counts_in_statistics(status):
    return status not in EXCLUDED_FROM_STATISTICS


def is_store_working_day(store, work_date):
    return get_store_day_status(store, work_date) != ChecklistDayStatus.DAY_OFF


def month_bounds(value):
    first = value.replace(day=1)
    last = first.replace(day=calendar.monthrange(first.year, first.month)[1])
    return first, last


def iter_month_dates(value):
    first, last = month_bounds(value)
    current = first
    while current <= last:
        yield current
        current += timedelta(days=1)


def working_dates_for_month(store, value):
    return [
        work_date
        for work_date in iter_month_dates(value)
        if is_store_working_day(store, work_date)
    ]


@transaction.atomic
def set_store_day_status(
    *,
    store,
    work_date,
    status,
    actor,
    comment='',
    request_metadata=None,
):
    if not can_manage_store_schedule(actor, store):
        raise OperationNotAllowedError('Нельзя изменить статус дня.')
    if status not in ChecklistDayStatus.values:
        raise OperationNotAllowedError('Неизвестный статус дня.')
    locked_store = type(store).objects.select_for_update().get(pk=store.pk)
    current = StoreDayStatus.objects.select_for_update().filter(
        store=locked_store,
        date=work_date,
    ).first()
    old_status = (
        current.status
        if current is not None
        else get_store_day_status(locked_store, work_date)
    )
    day_status, _ = StoreDayStatus.objects.update_or_create(
        store=locked_store,
        date=work_date,
        defaults={
            'status': status,
            'comment': (comment or '').strip(),
            'changed_by': actor,
        },
    )
    DailyChecklist.objects.filter(
        store=locked_store,
        checklist_date=work_date,
    ).update(day_status=status, updated_at=timezone.now())
    if status == ChecklistDayStatus.DAY_OFF:
        ChecklistNotification.objects.filter(
            stage__daily_checklist__store=locked_store,
            stage__daily_checklist__checklist_date=work_date,
            status__in=(
                ChecklistNotification.Status.PENDING,
                ChecklistNotification.Status.FAILED,
            ),
        ).delete()

    ip_address = None
    user_agent = None
    if request_metadata:
        meta = (
            request_metadata.META
            if hasattr(request_metadata, 'META')
            else request_metadata
        )
        ip_address = meta.get('REMOTE_ADDR')
        user_agent = meta.get('HTTP_USER_AGENT')
    AuditLog.objects.create(
        actor=actor,
        store=locked_store,
        object_type=day_status._meta.label_lower,
        object_id=str(day_status.pk),
        action=AuditLog.Action.STORE_DAY_STATUS_UPDATED,
        field_name='status',
        old_value={'status': old_status},
        new_value={
            'status': status,
            'date': work_date.isoformat(),
            'comment': day_status.comment,
        },
        ip_address=ip_address,
        user_agent=user_agent,
    )
    return day_status
