from datetime import date, timedelta

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count
from django.utils import timezone

from checklists.access_control import can_manage_store_shifts
from checklists.exceptions import OperationNotAllowedError
from checklists.management_services import (
    create_shift_assignment,
    delete_shift_assignment,
    ensure_shift_month_editable,
    update_shift_assignment,
)
from checklists.models import (
    AuditLog,
    DailyShiftAssignment,
    ShiftTemplate,
    StoreEmployee,
)


SHIFT_CELL_META = {
    DailyShiftAssignment.ShiftType.WORK: {
        'short': 'Д',
        'label': 'День',
        'css': 'work',
    },
    DailyShiftAssignment.ShiftType.NIGHT: {
        'short': 'Н',
        'label': 'Ночь',
        'css': 'night',
    },
    DailyShiftAssignment.ShiftType.DAY_OFF: {
        'short': 'В',
        'label': 'Выходной',
        'css': 'day-off',
    },
    DailyShiftAssignment.ShiftType.VACATION: {
        'short': 'О',
        'label': 'Отпуск',
        'css': 'vacation',
    },
    DailyShiftAssignment.ShiftType.SICK_LEAVE: {
        'short': 'Б',
        'label': 'Больничный',
        'css': 'sick',
    },
    DailyShiftAssignment.ShiftType.SERVICE: {
        'short': 'С',
        'label': 'Смена сервис',
        'css': 'service',
    },
    DailyShiftAssignment.ShiftType.PERSONAL: {
        'short': 'Л',
        'label': 'Личное отсутствие',
        'css': 'personal',
    },
}


def serialize_assignment(assignment):
    if assignment is None:
        return None
    meta = SHIFT_CELL_META[assignment.shift_type]
    return {
        'id': assignment.pk,
        'employee_id': assignment.employee_id,
        'date': assignment.work_date.isoformat(),
        'shift_type': assignment.shift_type,
        'label': meta['label'],
        'short': meta['short'],
        'css': meta['css'],
        'shift_start': (
            assignment.shift_start.strftime('%H:%M')
            if assignment.shift_start
            else ''
        ),
        'shift_end': (
            assignment.shift_end.strftime('%H:%M')
            if assignment.shift_end
            else ''
        ),
        'comment': assignment.comment or '',
    }


def month_completion(store, month_start, employees):
    employee_ids = [employee.pk for employee in employees]
    days_count = (
        (
            month_start.replace(day=28) + timedelta(days=4)
        ).replace(day=1)
        - month_start
    ).days
    counts = dict(
        DailyShiftAssignment.objects.filter(
            store=store,
            employee_id__in=employee_ids,
            work_date__year=month_start.year,
            work_date__month=month_start.month,
        )
        .values('employee_id')
        .annotate(total=Count('id'))
        .values_list('employee_id', 'total')
    )
    total_cells = len(employee_ids) * days_count
    filled_cells = sum(counts.values())
    incomplete_ids = {
        employee_id
        for employee_id in employee_ids
        if counts.get(employee_id, 0) < days_count
    }
    return {
        'days_count': days_count,
        'total_cells': total_cells,
        'filled_cells': filled_cells,
        'completion_percent': (
            round(100 * filled_cells / total_cells)
            if total_cells
            else 100
        ),
        'incomplete_ids': incomplete_ids,
    }


def _template_defaults(store, shift_type, template_id=None):
    template = None
    if template_id:
        template = ShiftTemplate.objects.filter(
            pk=template_id,
            store=store,
            is_active=True,
        ).first()
        if template is None:
            raise OperationNotAllowedError('Шаблон смены недоступен.')
        shift_type = template.shift_type
    elif shift_type in {
        DailyShiftAssignment.ShiftType.WORK,
        DailyShiftAssignment.ShiftType.NIGHT,
        DailyShiftAssignment.ShiftType.SERVICE,
    }:
        template = ShiftTemplate.objects.filter(
            store=store,
            shift_type=shift_type,
            is_active=True,
        ).order_by('sort_order', 'pk').first()
    return {
        'shift_type': shift_type,
        'shift_start': template.shift_start if template else None,
        'shift_end': template.shift_end if template else None,
        'is_responsible_for_checklist': (
            shift_type
            in {
                DailyShiftAssignment.ShiftType.WORK,
                DailyShiftAssignment.ShiftType.NIGHT,
                DailyShiftAssignment.ShiftType.SERVICE,
            }
        ),
        'comment': '',
    }


def _apply_cell(
    *,
    store,
    employee,
    work_date,
    actor,
    shift_type,
    template_id=None,
    explicit_defaults=None,
    comment=None,
    request_metadata=None,
):
    ensure_shift_month_editable(work_date)
    existing = DailyShiftAssignment.objects.filter(
        store=store,
        employee=employee,
        work_date=work_date,
    ).first()
    if shift_type == 'clear':
        if existing:
            delete_shift_assignment(
                store,
                existing,
                actor,
                request_metadata,
            )
        return None
    if shift_type not in DailyShiftAssignment.ShiftType.values:
        raise ValidationError('Неизвестный тип смены.')
    defaults = explicit_defaults or _template_defaults(
        store,
        shift_type,
        template_id,
    )
    defaults = dict(defaults)
    defaults['comment'] = (
        comment
        if comment is not None
        else (existing.comment or '' if existing else '')
    )
    data = {'employee': employee, **defaults}
    if existing:
        return update_shift_assignment(
            store,
            existing,
            data,
            actor,
            request_metadata,
        )
    return create_shift_assignment(
        store,
        work_date,
        data,
        actor,
        request_metadata,
    )


@transaction.atomic
def update_calendar_cells(
    *,
    store,
    updates,
    actor,
    request_metadata=None,
):
    if not can_manage_store_shifts(actor, store):
        raise OperationNotAllowedError('Нельзя изменять график.')
    if not isinstance(updates, list) or not updates or len(updates) > 1000:
        raise ValidationError('Передан недопустимый набор ячеек.')
    if not all(isinstance(update, dict) for update in updates):
        raise ValidationError('Передан недопустимый набор ячеек.')
    try:
        employee_ids = {
            int(update.get('employee_id', 0))
            for update in updates
        }
    except (TypeError, ValueError) as exc:
        raise ValidationError('Некорректный идентификатор сотрудника.') from exc
    employees = {
        employee.pk: employee
        for employee in StoreEmployee.objects.select_for_update().filter(
            store=store,
            is_active=True,
            pk__in=employee_ids,
        )
    }
    if set(employees) != employee_ids:
        raise OperationNotAllowedError(
            'Один из сотрудников относится к другому магазину.'
        )
    result = []
    for update in updates:
        try:
            work_date = date.fromisoformat(str(update.get('date', '')))
        except ValueError as exc:
            raise ValidationError('Некорректная дата смены.') from exc
        employee = employees[int(update['employee_id'])]
        original_date_value = update.get('original_date')
        original_date = work_date
        if original_date_value:
            try:
                original_date = date.fromisoformat(str(original_date_value))
            except ValueError as exc:
                raise ValidationError('Некорректная исходная дата смены.') from exc
        if original_date != work_date:
            ensure_shift_month_editable(original_date)
            ensure_shift_month_editable(work_date)
            original = DailyShiftAssignment.objects.select_for_update().filter(
                store=store,
                employee=employee,
                work_date=original_date,
            ).first()
            if original is None:
                raise ValidationError(
                    'Исходная смена для переноса не найдена.'
                )
            if DailyShiftAssignment.objects.filter(
                store=store,
                employee=employee,
                work_date=work_date,
            ).exists():
                raise ValidationError(
                    'На выбранную дату у сотрудника уже есть смена.'
                )
            delete_shift_assignment(
                store,
                original,
                actor,
                request_metadata,
            )
            result.append({
                'employee_id': employee.pk,
                'date': original_date.isoformat(),
                'assignment': None,
            })
        assignment = _apply_cell(
            store=store,
            employee=employee,
            work_date=work_date,
            actor=actor,
            shift_type=str(update.get('shift_type', '')),
            template_id=update.get('template_id'),
            comment=(
                str(update.get('comment', ''))[:2000]
                if 'comment' in update
                else None
            ),
            request_metadata=request_metadata,
        )
        result.append({
            'employee_id': employee.pk,
            'date': work_date.isoformat(),
            'assignment': serialize_assignment(assignment),
        })
    return result


@transaction.atomic
def copy_week_to_month(
    *,
    store,
    month_start,
    week_start,
    employee_ids,
    actor,
    request_metadata=None,
):
    if not can_manage_store_shifts(actor, store):
        raise OperationNotAllowedError('Нельзя копировать график.')
    employees = list(
        StoreEmployee.objects.select_for_update().filter(
            store=store,
            is_active=True,
            pk__in=employee_ids,
        )
    )
    if len(employees) != len(set(employee_ids)):
        raise OperationNotAllowedError('Список сотрудников недопустим.')
    month_end = (
        month_start.replace(day=28) + timedelta(days=4)
    ).replace(day=1) - timedelta(days=1)
    changed_cells = []
    for employee in employees:
        pattern = {
            assignment.work_date.weekday(): assignment
            for assignment in DailyShiftAssignment.objects.filter(
                store=store,
                employee=employee,
                work_date__range=(week_start, week_start + timedelta(days=6)),
            )
        }
        current = month_start
        while current <= month_end:
            source = pattern.get(current.weekday())
            if source and current >= timezone.localdate():
                assignment = _apply_cell(
                    store=store,
                    employee=employee,
                    work_date=current,
                    actor=actor,
                    shift_type=source.shift_type,
                    explicit_defaults={
                        'shift_type': source.shift_type,
                        'shift_start': source.shift_start,
                        'shift_end': source.shift_end,
                        'is_responsible_for_checklist': (
                            source.is_responsible_for_checklist
                        ),
                        'comment': source.comment or '',
                    },
                    request_metadata=request_metadata,
                )
                changed_cells.append({
                    'employee_id': employee.pk,
                    'date': current.isoformat(),
                    'assignment': serialize_assignment(assignment),
                })
            current += timedelta(days=1)
    return changed_cells


@transaction.atomic
def create_shift_template(*, store, data, actor, request_metadata=None):
    if not can_manage_store_shifts(actor, store):
        raise OperationNotAllowedError('Нельзя создать шаблон смены.')
    template = ShiftTemplate(store=store, **data)
    template.save()
    AuditLog.objects.create(
        actor=actor,
        store=store,
        object_type=template._meta.label_lower,
        object_id=str(template.pk),
        action=AuditLog.Action.SHIFT_TEMPLATE_CREATED,
        new_value={
            'name': template.name,
            'shift_type': template.shift_type,
        },
    )
    return template


@transaction.atomic
def delete_shift_template(*, store, template, actor):
    if not can_manage_store_shifts(actor, store):
        raise OperationNotAllowedError('Нельзя удалить шаблон смены.')
    locked = ShiftTemplate.objects.select_for_update().get(
        pk=template.pk,
        store=store,
    )
    object_id = locked.pk
    old_value = {'name': locked.name, 'shift_type': locked.shift_type}
    locked.delete()
    AuditLog.objects.create(
        actor=actor,
        store=store,
        object_type=ShiftTemplate._meta.label_lower,
        object_id=str(object_id),
        action=AuditLog.Action.SHIFT_TEMPLATE_DELETED,
        old_value=old_value,
    )
