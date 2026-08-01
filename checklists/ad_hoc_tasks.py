from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.utils import timezone

from checklists.access_control import can_manage_store_tasks, is_system_admin
from checklists.exceptions import ChecklistLockedError, OperationNotAllowedError
from checklists.models import (
    AuditLog,
    ChecklistAnswer,
    ChecklistItem,
    DailyChecklist,
    DailyChecklistItem,
    DailyChecklistStage,
    Store,
    StoreAdHocTask,
    StoreEmployee,
    TelegramStoreBinding,
    UserStoreMembership,
)
from checklists.services import build_stage_schedule
from checklists.telegram_queue import enqueue_template_message


AD_HOC_TO_DAILY_SECTION = {
    StoreAdHocTask.SectionCode.MORNING: DailyChecklistStage.SectionCode.OPENING,
    StoreAdHocTask.SectionCode.DAY: DailyChecklistStage.SectionCode.DURING_DAY,
    StoreAdHocTask.SectionCode.EVENING: DailyChecklistStage.SectionCode.CLOSING,
}
DAILY_TO_AD_HOC_SECTION = {
    value: key for key, value in AD_HOC_TO_DAILY_SECTION.items()
}
SECTION_ORDER = {
    StoreAdHocTask.SectionCode.MORNING: 0,
    StoreAdHocTask.SectionCode.DAY: 1,
    StoreAdHocTask.SectionCode.EVENING: 2,
}


def _request_values(request_metadata):
    request_metadata = request_metadata or {}
    return (
        request_metadata.get('ip_address'),
        request_metadata.get('user_agent'),
    )


def is_ad_hoc_stage_closed(store, work_date, section_code, *, at=None):
    if section_code not in AD_HOC_TO_DAILY_SECTION:
        raise ValidationError('Неизвестный этап разовой задачи.')
    at = at or timezone.now()
    daily_section = AD_HOC_TO_DAILY_SECTION[section_code]
    stages = DailyChecklistStage.objects.filter(
        daily_checklist__store=store,
        daily_checklist__checklist_date=work_date,
        section_code=daily_section,
    ).select_related('daily_checklist')
    for stage in stages:
        if stage.daily_checklist.status == DailyChecklist.Status.COMPLETED:
            return True
        if stage.status in {
            DailyChecklistStage.Status.COMPLETED,
            DailyChecklistStage.Status.COMPLETED_LATE,
        }:
            return True
        if at >= stage.deadline_at:
            return True
    if stages.exists():
        return False
    schedule = build_stage_schedule(store, work_date)
    return at >= schedule[daily_section]['deadline_at']


def available_ad_hoc_sections(store, work_date, *, at=None):
    return [
        code
        for code in StoreAdHocTask.SectionCode.values
        if not is_ad_hoc_stage_closed(store, work_date, code, at=at)
    ]


def _task_context(task, employee_name='', comment=''):
    return {
        'store_name': task.store.name,
        'date': task.date.strftime('%d.%m.%Y'),
        'stage_name': task.get_section_code_display(),
        'task_text': task.text,
        'task_description': task.description,
        'employee_name': employee_name,
        'comment': comment,
        'task_url': '/checklist/today/',
        'checklist_url': '/checklist/today/',
        'deadline': '',
        'remaining_count': '',
        'failed_count': '',
    }


def _select_daily_for_task(store, work_date):
    return (
        DailyChecklist.objects.select_for_update()
        .filter(store=store, checklist_date=work_date)
        .order_by(models.F('terminal_account_id').desc(nulls_last=True), 'id')
        .first()
    )


def _attach_task_locked(task, daily):
    daily_section = AD_HOC_TO_DAILY_SECTION[task.section_code]
    stage = DailyChecklistStage.objects.select_for_update().get(
        daily_checklist=daily,
        section_code=daily_section,
    )
    if (
        daily.status == DailyChecklist.Status.COMPLETED
        or stage.status
        in {
            DailyChecklistStage.Status.COMPLETED,
            DailyChecklistStage.Status.COMPLETED_LATE,
        }
        or timezone.now() >= stage.deadline_at
    ):
        raise ChecklistLockedError('Выбранный этап уже закрыт.')
    last_sort = (
        DailyChecklistItem.objects.filter(
            daily_checklist=daily,
            section_code=daily_section,
        ).aggregate(value=models.Max('item_sort_order'))['value']
        or 0
    )
    last_display = (
        DailyChecklistItem.objects.filter(
            daily_checklist=daily,
            section_code=daily_section,
        ).aggregate(value=models.Max('display_order'))['value']
        or 0
    )
    item = DailyChecklistItem.objects.create(
        daily_checklist=daily,
        source_item=None,
        section_code=daily_section,
        section_name=stage.get_section_code_display(),
        section_sort_order=SECTION_ORDER[task.section_code],
        item_text=task.text,
        item_description=task.description,
        item_sort_order=last_sort + 1,
        is_required=task.is_required,
        answer_type_snapshot=ChecklistItem.AnswerType.STATUS,
        display_order=last_display + 1,
        comment_required_on_failure=True,
        allow_not_applicable=False,
    )
    ChecklistAnswer.objects.create(
        daily_item=item,
        status=ChecklistAnswer.Status.PENDING,
    )
    StoreAdHocTask.objects.filter(pk=task.pk).update(
        daily_checklist=daily,
        daily_stage=stage,
        daily_item=item,
        status=StoreAdHocTask.Status.ACTIVE,
        updated_at=timezone.now(),
    )
    task.refresh_from_db()
    return task


@transaction.atomic
def attach_pending_tasks_to_daily(daily):
    locked_daily = DailyChecklist.objects.select_for_update().get(pk=daily.pk)
    tasks = list(
        StoreAdHocTask.objects.select_for_update().filter(
            store=locked_daily.store,
            date=locked_daily.checklist_date,
            daily_item__isnull=True,
            status=StoreAdHocTask.Status.PLANNED,
        )
    )
    attached = []
    for task in tasks:
        if not is_ad_hoc_stage_closed(
            task.store,
            task.date,
            task.section_code,
        ):
            attached.append(_attach_task_locked(task, locked_daily))
    return attached


@transaction.atomic
def create_ad_hoc_task(
    *,
    store,
    date,
    section_code,
    text,
    description='',
    is_required=True,
    source=StoreAdHocTask.Source.WEB,
    created_by=None,
    created_by_telegram_binding=None,
    request_metadata=None,
    audit_context=None,
):
    locked_store = Store.objects.select_for_update().get(pk=store.pk)
    if not locked_store.is_active:
        raise OperationNotAllowedError('Магазин неактивен.')
    if created_by_telegram_binding is not None:
        binding = TelegramStoreBinding.objects.select_for_update().get(
            pk=created_by_telegram_binding.pk,
            is_active=True,
        )
        if binding.user_id != getattr(created_by, 'pk', None):
            raise OperationNotAllowedError(
                'Telegram-привязка не соответствует автору задачи.'
            )
        has_membership = UserStoreMembership.objects.filter(
            user=created_by,
            store=locked_store,
            is_active=True,
        ).exists()
        if not has_membership and binding.store_id != locked_store.pk:
            raise OperationNotAllowedError(
                'Пользователь Telegram не связан с магазином.'
            )
    else:
        binding = None
    text = (text or '').strip()
    description = (description or '').strip()
    if not text:
        raise ValidationError({'text': 'Введите текст задачи.'})
    if is_ad_hoc_stage_closed(locked_store, date, section_code):
        raise ChecklistLockedError('Выбранный этап уже закрыт.')
    task = StoreAdHocTask.objects.create(
        store=locked_store,
        date=date,
        section_code=section_code,
        text=text,
        description=description,
        is_required=is_required,
        status=StoreAdHocTask.Status.PLANNED,
        source=source,
        created_by=created_by,
        created_by_telegram_binding=binding,
    )
    daily = _select_daily_for_task(locked_store, date)
    if daily is not None:
        task = _attach_task_locked(task, daily)
    ip_address, user_agent = _request_values(request_metadata)
    audit_value = {
        'date': date.isoformat(),
        'section_code': section_code,
        'source': source,
    }
    audit_value.update(audit_context or {})
    AuditLog.objects.create(
        actor=created_by,
        store=locked_store,
        object_type=task._meta.label_lower,
        object_id=str(task.pk),
        action=(
            AuditLog.Action.STORE_TASK_CREATED_BY_ADMIN
            if source == StoreAdHocTask.Source.WEB
            else AuditLog.Action.TELEGRAM_TASK_CREATED
        ),
        new_value=audit_value,
        ip_address=ip_address,
        user_agent=user_agent,
    )
    enqueue_template_message(
        locked_store,
        'task_created',
        _task_context(task),
        idempotency_key=f'task:{task.pk}:created',
    )
    return task


@transaction.atomic
def update_ad_hoc_task(task, *, data, actor, request_metadata=None):
    locked = StoreAdHocTask.objects.select_for_update().select_related(
        'store', 'daily_stage', 'daily_item'
    ).get(pk=task.pk)
    if locked.status in {
        StoreAdHocTask.Status.COMPLETED,
        StoreAdHocTask.Status.FAILED,
        StoreAdHocTask.Status.CANCELLED,
    }:
        raise OperationNotAllowedError('Завершённую задачу изменить нельзя.')
    target_store = data.get('store') or locked.store
    store_changed = target_store.pk != locked.store_id
    if store_changed and not is_system_admin(actor):
        raise OperationNotAllowedError(
            'Менять магазин задачи может только системный администратор.'
        )
    if store_changed:
        target_store = Store.objects.select_for_update().get(
            pk=target_store.pk,
            is_active=True,
        )
        if locked.daily_checklist_id or locked.daily_stage_id or locked.daily_item_id:
            raise OperationNotAllowedError(
                'Нельзя перенести задачу, уже добавленную в ежедневный чек-лист.'
            )
    if is_ad_hoc_stage_closed(
        target_store,
        data['date'],
        data['section_code'],
    ):
        raise ChecklistLockedError('Выбранный этап уже закрыт.')
    if locked.daily_item_id and (
        data['date'] != locked.date or data['section_code'] != locked.section_code
    ):
        raise OperationNotAllowedError(
            'После добавления в чек-лист дату и этап менять нельзя.'
        )
    old = {
        'store_id': locked.store_id,
        'store_name': locked.store.name,
        'date': locked.date.isoformat(),
        'section_code': locked.section_code,
        'text': locked.text,
    }
    locked.store = target_store
    if (
        store_changed
        and locked.created_by_telegram_binding_id
        and locked.created_by_telegram_binding.store_id != target_store.pk
    ):
        locked.created_by_telegram_binding = None
    for field in ('date', 'section_code', 'text', 'description', 'is_required'):
        setattr(locked, field, data[field])
    locked.full_clean()
    locked.save()
    if locked.daily_item_id:
        locked.daily_item.item_text = locked.text
        locked.daily_item.item_description = locked.description
        locked.daily_item.is_required = locked.is_required
        locked.daily_item.save(
            update_fields=('item_text', 'item_description', 'is_required')
        )
    ip_address, user_agent = _request_values(request_metadata)
    AuditLog.objects.create(
        actor=actor,
        store=locked.store,
        object_type=locked._meta.label_lower,
        object_id=str(locked.pk),
        action=AuditLog.Action.STORE_TASK_UPDATED,
        old_value=old,
        new_value={
            'store_id': locked.store_id,
            'store_name': locked.store.name,
            'date': locked.date.isoformat(),
            'section_code': locked.section_code,
            'text': locked.text,
        },
        ip_address=ip_address,
        user_agent=user_agent,
    )
    return locked


@transaction.atomic
def copy_ad_hoc_task(
    task,
    *,
    target_store,
    date,
    actor,
    request_metadata=None,
):
    if not is_system_admin(actor):
        raise OperationNotAllowedError(
            'Копировать задачи между магазинами может только системный администратор.'
        )
    source = StoreAdHocTask.objects.select_for_update().select_related(
        'store',
    ).get(pk=task.pk)
    if source.store_id == target_store.pk:
        raise ValidationError(
            {'target_store': 'Выберите другой магазин.'}
        )
    return create_ad_hoc_task(
        store=target_store,
        date=date,
        section_code=source.section_code,
        text=source.text,
        description=source.description,
        is_required=source.is_required,
        source=StoreAdHocTask.Source.WEB,
        created_by=actor,
        request_metadata=request_metadata,
        audit_context={
            'copied_from_task_id': source.pk,
            'copied_from_store_id': source.store_id,
        },
    )


@transaction.atomic
def delete_ad_hoc_task(task, *, actor, request_metadata=None):
    locked = StoreAdHocTask.objects.select_for_update().select_related(
        'store',
        'created_by',
    ).get(pk=task.pk)
    if not is_system_admin(actor) and not can_manage_store_tasks(
        actor,
        locked.store,
    ):
        raise OperationNotAllowedError('Удаление задачи запрещено.')
    task_id = locked.pk
    task_value = {
        'date': locked.date.isoformat(),
        'section_code': locked.section_code,
        'text': locked.text,
        'created_by_id': locked.created_by_id,
    }
    ip_address, user_agent = _request_values(request_metadata)
    AuditLog.objects.create(
        actor=actor,
        store=locked.store,
        object_type=locked._meta.label_lower,
        object_id=str(task_id),
        action=AuditLog.Action.STORE_TASK_DELETED,
        old_value=task_value,
        ip_address=ip_address,
        user_agent=user_agent,
    )
    locked.delete()
    return task_id


@transaction.atomic
def cancel_ad_hoc_task(task, *, actor, request_metadata=None):
    locked = StoreAdHocTask.objects.select_for_update().select_related(
        'store', 'daily_stage', 'daily_item'
    ).get(pk=task.pk)
    if locked.status in {
        StoreAdHocTask.Status.COMPLETED,
        StoreAdHocTask.Status.FAILED,
    }:
        raise OperationNotAllowedError('Выполненную задачу отменить нельзя.')
    if locked.status == StoreAdHocTask.Status.CANCELLED:
        return locked
    if is_ad_hoc_stage_closed(locked.store, locked.date, locked.section_code):
        raise ChecklistLockedError('Закрытый этап изменить нельзя.')
    locked.status = StoreAdHocTask.Status.CANCELLED
    locked.save(update_fields=('status', 'updated_at'))
    if locked.daily_item_id:
        locked.daily_item.is_required = False
        locked.daily_item.item_text = f'[Отменено] {locked.text}'
        locked.daily_item.save(update_fields=('is_required', 'item_text'))
    ip_address, user_agent = _request_values(request_metadata)
    AuditLog.objects.create(
        actor=actor,
        store=locked.store,
        object_type=locked._meta.label_lower,
        object_id=str(locked.pk),
        action=AuditLog.Action.STORE_TASK_CANCELLED,
        new_value={'status': locked.status},
        ip_address=ip_address,
        user_agent=user_agent,
    )
    return locked


def sync_ad_hoc_task_from_answer(
    answer,
    *,
    employee,
    actor,
    request_metadata=None,
):
    try:
        task_id = answer.daily_item.ad_hoc_task.pk
    except StoreAdHocTask.DoesNotExist:
        return None
    if answer.status not in {
        ChecklistAnswer.Status.COMPLETED,
        ChecklistAnswer.Status.FAILED,
    }:
        return None
    task = StoreAdHocTask.objects.select_for_update().select_related('store').get(
        pk=task_id
    )
    if employee is None:
        raise OperationNotAllowedError('Для разовой задачи выберите сотрудника.')
    locked_employee = StoreEmployee.objects.select_for_update().get(
        pk=employee.pk,
        store=task.store,
        is_active=True,
    )
    failed = answer.status == ChecklistAnswer.Status.FAILED
    if failed and not answer.comment.strip():
        raise ValidationError('Для невыполненной задачи обязателен комментарий.')
    task.status = (
        StoreAdHocTask.Status.FAILED
        if failed
        else StoreAdHocTask.Status.COMPLETED
    )
    task.completed_by_employee = locked_employee
    task.completion_comment = answer.comment
    task.completed_at = answer.answered_at or timezone.now()
    task.save(
        update_fields=(
            'status',
            'completed_by_employee',
            'completion_comment',
            'completed_at',
            'updated_at',
        )
    )
    action = (
        AuditLog.Action.TELEGRAM_TASK_FAILED
        if failed
        else AuditLog.Action.TELEGRAM_TASK_COMPLETED
    )
    ip_address, user_agent = _request_values(request_metadata)
    AuditLog.objects.create(
        actor=actor,
        employee=locked_employee,
        store=task.store,
        object_type=task._meta.label_lower,
        object_id=str(task.pk),
        action=action,
        new_value={
            'status': task.status,
            'comment': task.completion_comment,
        },
        ip_address=ip_address,
        user_agent=user_agent,
    )
    template_type = 'task_failed' if failed else 'task_completed'
    enqueue_template_message(
        task.store,
        template_type,
        _task_context(
            task,
            employee_name=locked_employee.display_name,
            comment=task.completion_comment,
        ),
        idempotency_key=f'task:{task.pk}:{task.status}:{answer.updated_at.isoformat()}',
    )
    return task
