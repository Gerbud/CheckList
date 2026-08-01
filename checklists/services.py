import hashlib
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from checklists.exceptions import (
    AnswerValidationError,
    ChecklistCompletionError,
    ChecklistLockedError,
    DuplicateDailyChecklistError,
    InvalidTemplateVersionStateError,
    OperationNotAllowedError,
    TemplateConfigurationError,
)
from checklists.models import (
    AnswerRevision,
    AuditLog,
    ChecklistAnswer,
    ChecklistDayStatus,
    ChecklistItem,
    ChecklistTemplate,
    ChecklistTemplateVersion,
    DailyChecklist,
    DailyChecklistItem,
    DailyChecklistStage,
    DailyShiftAssignment,
    EmployeeProfile,
    Store,
    StoreChecklistSchedule,
    StoreEmployee,
    StoreTerminalAccount,
)
from checklists.calendar_services import get_store_day_status


STAGE_CODES = (
    DailyChecklistStage.SectionCode.OPENING,
    DailyChecklistStage.SectionCode.DURING_DAY,
    DailyChecklistStage.SectionCode.CLOSING,
)


def _store_timezone(store):
    try:
        return ZoneInfo(store.timezone)
    except ZoneInfoNotFoundError as exc:
        raise TemplateConfigurationError(
            f'Неизвестный часовой пояс магазина: {store.timezone}.'
        ) from exc


def build_stage_schedule(store, checklist_date):
    """Возвращает границы трёх этапов в часовом поясе магазина."""
    tz = _store_timezone(store)
    schedule_settings, _ = StoreChecklistSchedule.objects.get_or_create(
        store=store
    )
    try:
        schedule_settings.full_clean()
    except ValidationError as exc:
        raise TemplateConfigurationError(
            f'Некорректное расписание магазина: {exc}'
        ) from exc
    if not schedule_settings.is_active:
        raise TemplateConfigurationError(
            'Расписание чек-листа магазина отключено.'
        )

    stage_times = (
        (
            DailyChecklistStage.SectionCode.OPENING,
            schedule_settings.opening_time,
            schedule_settings.morning_deadline,
            schedule_settings.morning_completion_window_minutes,
        ),
        (
            DailyChecklistStage.SectionCode.DURING_DAY,
            schedule_settings.morning_deadline,
            schedule_settings.daytime_deadline,
            schedule_settings.day_completion_window_minutes,
        ),
        (
            DailyChecklistStage.SectionCode.CLOSING,
            schedule_settings.daytime_deadline,
            schedule_settings.closing_deadline,
            schedule_settings.evening_completion_window_minutes,
        ),
    )
    schedule = {}
    previous_boundary = None
    for section_code, opens_time, deadline_time, window_minutes in stage_times:
        opens_at = timezone.make_aware(
            datetime.combine(checklist_date, opens_time),
            tz,
        )
        if previous_boundary is not None:
            opens_at = previous_boundary
        deadline_at = timezone.make_aware(
            datetime.combine(checklist_date, deadline_time),
            tz,
        )
        while deadline_at <= opens_at:
            deadline_at += timedelta(days=1)
        completion_available_at = (
            opens_at
            if window_minutes == 0
            else max(
                opens_at,
                deadline_at - timedelta(minutes=window_minutes),
            )
        )
        schedule[section_code] = {
            'opens_at': opens_at,
            'completion_available_at': completion_available_at,
            'deadline_at': deadline_at,
        }
        previous_boundary = deadline_at
    return schedule


def get_stage_warning_minutes(stage):
    try:
        schedule = stage.daily_checklist.store.checklist_schedule
    except StoreChecklistSchedule.DoesNotExist:
        schedule = StoreChecklistSchedule(store=stage.daily_checklist.store)
    schedule.full_clean()
    return schedule.warning_minutes_before


def _scheduled_stage_state(stage, at):
    if at < stage.opens_at:
        return DailyChecklistStage.Status.LOCKED
    if at >= stage.deadline_at:
        return DailyChecklistStage.Status.OVERDUE
    return DailyChecklistStage.Status.AVAILABLE


def get_stage_state(stage, at=None):
    at = at or timezone.now()
    if not timezone.is_aware(at):
        raise ValueError('Время определения статуса этапа должно быть aware.')
    if stage.status in {
        DailyChecklistStage.Status.COMPLETED,
        DailyChecklistStage.Status.COMPLETED_LATE,
    }:
        return stage.status
    return _scheduled_stage_state(stage, at)


def get_stage_completion_available_at(stage):
    """Snapshot-граница, после которой этап разрешено завершить."""
    return stage.completion_available_at


def can_complete_stage(stage, at=None):
    at = at or timezone.now()
    if not timezone.is_aware(at):
        raise ValueError('Время проверки завершения этапа должно быть aware.')
    if stage.status in {
        DailyChecklistStage.Status.COMPLETED,
        DailyChecklistStage.Status.COMPLETED_LATE,
    }:
        return False
    return at >= get_stage_completion_available_at(stage)


def get_current_stage(daily_checklist, at=None):
    """Доступный этап приоритетнее старого просроченного и будущего."""
    at = at or timezone.now()
    stages = list(daily_checklist.stages.all().order_by('opens_at', 'id'))
    unfinished = [
        stage
        for stage in stages
        if get_stage_state(stage, at)
        not in {
            DailyChecklistStage.Status.COMPLETED,
            DailyChecklistStage.Status.COMPLETED_LATE,
        }
    ]
    for wanted in (
        DailyChecklistStage.Status.AVAILABLE,
        DailyChecklistStage.Status.OVERDUE,
        DailyChecklistStage.Status.LOCKED,
    ):
        matching = [
            stage
            for stage in unfinished
            if get_stage_state(stage, at) == wanted
        ]
        candidate = matching[-1] if wanted == DailyChecklistStage.Status.OVERDUE and matching else (
            matching[0] if matching else None
        )
        if candidate:
            return candidate
    return stages[-1] if stages else None


def _display_order(employee_id, checklist_date, section_code, item_identity):
    secret = getattr(settings, 'RANDOMIZATION_SECRET', settings.SECRET_KEY)
    payload = (
        f'{secret}|{employee_id}|{checklist_date.isoformat()}|'
        f'{section_code}|{item_identity}'
    ).encode()
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], 'big') & (
        2**63 - 1
    )


def _request_metadata_values(request_metadata):
    if not request_metadata:
        return None, None
    if hasattr(request_metadata, 'META'):
        meta = request_metadata.META
        return meta.get('REMOTE_ADDR'), meta.get('HTTP_USER_AGENT')
    return (
        request_metadata.get('ip_address'),
        request_metadata.get('user_agent'),
    )


def _write_audit_log(
    *,
    actor,
    store,
    obj,
    action,
    field_name='',
    old_value=None,
    new_value=None,
    employee=None,
    request_metadata=None,
):
    ip_address, user_agent = _request_metadata_values(request_metadata)
    return AuditLog.objects.create(
        actor=actor,
        employee=employee,
        store=store,
        object_type=obj._meta.label_lower,
        object_id=str(obj.pk),
        action=action,
        field_name=field_name,
        old_value=old_value,
        new_value=new_value,
        ip_address=ip_address,
        user_agent=user_agent,
    )


def _locked_actor_profile(actor, store):
    if actor is None or not actor.is_active:
        raise OperationNotAllowedError('Требуется активный пользователь.')
    try:
        profile = EmployeeProfile.objects.get(user=actor)
    except EmployeeProfile.DoesNotExist as exc:
        raise OperationNotAllowedError(
            'У пользователя нет профиля сотрудника.'
        ) from exc
    if not profile.is_active:
        raise OperationNotAllowedError(
            'Профиль неактивен.'
        )
    if profile.role == EmployeeProfile.Role.SYSTEM_ADMIN:
        if profile.store_id is not None:
            raise OperationNotAllowedError(
                'Администратор системы имеет некорректный магазин.'
            )
    elif profile.store_id != store.pk:
        raise OperationNotAllowedError(
            'Профиль относится к другому магазину.'
        )
    return profile


def _locked_actor_identity(actor, store):
    if actor is None or not actor.is_active:
        raise OperationNotAllowedError('Требуется активный пользователь.')
    try:
        terminal = StoreTerminalAccount.objects.select_related('store').get(
            user=actor,
            is_active=True,
            store=store,
            store__is_active=True,
        )
    except StoreTerminalAccount.DoesNotExist:
        return 'individual', _locked_actor_profile(actor, store)
    return 'terminal', terminal


def _ensure_actor_can_edit_checklist(actor, daily_checklist):
    account_type, account = _locked_actor_identity(
        actor,
        daily_checklist.store,
    )
    if account_type == 'terminal':
        if account.pk == daily_checklist.terminal_account_id:
            return account_type, account
        raise OperationNotAllowedError('Терминал не может изменять чужой чек-лист.')
    profile = account
    if profile.pk == daily_checklist.employee_id:
        return account_type, profile
    if profile.role in {
        EmployeeProfile.Role.STORE_DIRECTOR,
        EmployeeProfile.Role.SYSTEM_ADMIN,
    }:
        return account_type, profile
    raise OperationNotAllowedError('Нельзя изменять чек-лист другого сотрудника.')


def _lock_action_employee(employee, daily, account_type):
    if employee is None:
        if account_type == 'terminal':
            raise OperationNotAllowedError('Сначала выберите сотрудника магазина.')
        return None
    try:
        locked_employee = StoreEmployee.objects.select_for_update().get(
            pk=employee.pk,
            store=daily.store,
            is_active=True,
        )
    except StoreEmployee.DoesNotExist as exc:
        raise OperationNotAllowedError(
            'Сотрудник неактивен или относится к другому магазину.'
        ) from exc
    return locked_employee


def _ensure_actor_is_manager(actor, store):
    if actor is not None and actor.is_active and actor.is_superuser:
        return None
    profile = _locked_actor_profile(actor, store)
    if profile.role not in {
        EmployeeProfile.Role.STORE_DIRECTOR,
        EmployeeProfile.Role.SYSTEM_ADMIN,
    }:
        raise OperationNotAllowedError(
            'Операция доступна только менеджеру или администратору.'
        )
    return profile


def create_daily_checklist(account, checklist_date):
    try:
        with transaction.atomic():
            if isinstance(account, StoreTerminalAccount):
                account = (
                    StoreTerminalAccount.objects.select_for_update()
                    .select_related('store', 'user')
                    .get(pk=account.pk)
                )
                if not account.is_active or not account.user.is_active:
                    raise OperationNotAllowedError(
                        'Терминальный аккаунт неактивен.'
                    )
                daily_account = {'terminal_account': account, 'employee': None}
                identity = f'terminal:{account.pk}'
            else:
                account = (
                    EmployeeProfile.objects.select_for_update()
                    .select_related('store', 'user')
                    .get(pk=account.pk)
                )
                if not account.is_active or not account.user.is_active:
                    raise OperationNotAllowedError('Профиль сотрудника неактивен.')
                daily_account = {'employee': account, 'terminal_account': None}
                identity = f'individual:{account.pk}'
            Store.objects.select_for_update().get(pk=account.store_id)
            if not account.store.is_active:
                raise OperationNotAllowedError('Магазин неактивен.')
            day_status = get_store_day_status(account.store, checklist_date)
            if day_status == ChecklistDayStatus.DAY_OFF:
                raise OperationNotAllowedError(
                    'На эту дату установлен выходной. Чек-лист не требуется.'
                )

            if DailyChecklist.objects.select_for_update().filter(
                store=account.store,
                checklist_date=checklist_date,
                **daily_account,
            ).exists():
                raise DuplicateDailyChecklistError(
                    'Чек-лист сотрудника на эту дату уже создан.'
                )

            published_versions = list(
                ChecklistTemplateVersion.objects.select_for_update()
                .select_related('template')
                .filter(
                    template__store=account.store,
                    template__is_active=True,
                    status=ChecklistTemplateVersion.Status.PUBLISHED,
                )
            )
            if len(published_versions) != 1:
                raise TemplateConfigurationError(
                    'У магазина должна быть ровно одна активная '
                    'опубликованная версия шаблона.'
                )
            version = published_versions[0]

            daily = DailyChecklist.objects.create(
                store=account.store,
                **daily_account,
                checklist_date=checklist_date,
                template_version=version,
                status=DailyChecklist.Status.DRAFT,
                day_status=day_status,
                started_at=timezone.now(),
            )

            now = timezone.now()
            schedule = build_stage_schedule(account.store, checklist_date)
            DailyChecklistStage.objects.bulk_create(
                [
                    DailyChecklistStage(
                        daily_checklist=daily,
                        section_code=section_code,
                        status=_scheduled_stage_state(
                            DailyChecklistStage(
                                opens_at=limits['opens_at'],
                                completion_available_at=(
                                    limits['completion_available_at']
                                ),
                                deadline_at=limits['deadline_at'],
                            ),
                            now,
                        ),
                        opens_at=limits['opens_at'],
                        completion_available_at=(
                            limits['completion_available_at']
                        ),
                        deadline_at=limits['deadline_at'],
                    )
                    for section_code, limits in schedule.items()
                ]
            )
            from checklists.notifications import schedule_stage_notifications

            for stage in daily.stages.all():
                schedule_stage_notifications(stage)

            source_items = list(
                version.sections.order_by('sort_order', 'id')
                .prefetch_related('items')
            )
            snapshots = []
            for section in source_items:
                for item in section.items.all().order_by('sort_order', 'id'):
                    if (
                        not item.is_active
                        or (
                            item.effective_from
                            and checklist_date < item.effective_from
                        )
                        or (
                            item.effective_until
                            and checklist_date > item.effective_until
                        )
                    ):
                        continue
                    snapshots.append(
                        DailyChecklistItem(
                            daily_checklist=daily,
                            source_item=item,
                            section_code=section.code,
                            section_name=section.name,
                            section_sort_order=section.sort_order,
                            item_text=item.text,
                            item_description=item.description,
                            item_sort_order=item.sort_order,
                            is_required=item.is_required,
                            answer_type_snapshot=item.answer_type,
                            display_order=_display_order(
                                identity,
                                checklist_date,
                                section.code,
                                item.pk,
                            ),
                            comment_required_on_failure=(
                                item.comment_required_on_failure
                            ),
                            allow_not_applicable=item.allow_not_applicable,
                        )
                    )
            # Сохраняем по одному: это гарантирует получение PK и на MySQL,
            # где возврат AutoField из bulk_create зависит от версии сервера.
            for snapshot in snapshots:
                snapshot.save()
            ChecklistAnswer.objects.bulk_create(
                [
                    ChecklistAnswer(
                        daily_item=snapshot,
                        status=(
                            None
                            if snapshot.answer_type_snapshot
                            == ChecklistItem.AnswerType.INTEGER
                            else ChecklistAnswer.Status.PENDING
                        ),
                    )
                    for snapshot in snapshots
                ]
            )
            from checklists.ad_hoc_tasks import attach_pending_tasks_to_daily

            attach_pending_tasks_to_daily(daily)

            _write_audit_log(
                actor=account.user,
                store=account.store,
                obj=daily,
                action=AuditLog.Action.DAILY_CHECKLIST_CREATED,
                new_value={
                    'status': daily.status,
                    'checklist_date': checklist_date.isoformat(),
                    'template_version_id': version.pk,
                },
            )
            return daily
    except (IntegrityError, ValidationError) as exc:
        raise DuplicateDailyChecklistError(
            'Чек-лист сотрудника на эту дату уже создан.'
        ) from exc


@transaction.atomic
def update_answer(
    answer,
    status,
    comment,
    actor,
    employee=None,
    change_reason=None,
    request_metadata=None,
    at=None,
    integer_value=None,
):
    if employee is not None and not isinstance(employee, StoreEmployee):
        if request_metadata is None:
            request_metadata = employee
            employee = None
        else:
            raise OperationNotAllowedError('Некорректный сотрудник действия.')
    comment = comment or ''

    answer_location = (
        ChecklistAnswer.objects.select_related('daily_item')
        .only(
            'daily_item__daily_checklist_id',
            'daily_item__section_code',
        )
        .get(pk=answer.pk)
    )
    daily = (
        DailyChecklist.objects.select_for_update()
        .select_related('store', 'employee', 'terminal_account')
        .get(pk=answer_location.daily_item.daily_checklist_id)
    )
    account_type, _ = _ensure_actor_can_edit_checklist(actor, daily)
    action_employee = _lock_action_employee(employee, daily, account_type)
    if daily.status == DailyChecklist.Status.COMPLETED:
        raise ChecklistLockedError(
            'Сначала завершённый чек-лист должен быть открыт повторно.'
        )
    stage = DailyChecklistStage.objects.select_for_update().get(
        daily_checklist=daily,
        section_code=answer_location.daily_item.section_code,
    )
    locked_answer = (
        ChecklistAnswer.objects.select_for_update()
        .select_related('daily_item')
        .get(pk=answer.pk)
    )
    answer_type = locked_answer.daily_item.answer_type_snapshot
    if answer_type == ChecklistItem.AnswerType.INTEGER:
        if status is not None:
            raise AnswerValidationError(
                'Для числового вопроса нельзя передавать статус.'
            )
        if (
            isinstance(integer_value, bool)
            or not isinstance(integer_value, int)
            or integer_value < 0
        ):
            raise AnswerValidationError(
                'Укажите целое неотрицательное число.'
            )
        if comment:
            raise AnswerValidationError(
                'Комментарий не используется для числового вопроса.'
            )
    else:
        if status not in ChecklistAnswer.Status.values:
            raise AnswerValidationError('Неизвестный статус ответа.')
        if integer_value is not None:
            raise AnswerValidationError(
                'Числовое значение нельзя передать для статусного вопроса.'
            )
    operation_time = at or timezone.now()
    stage_state = get_stage_state(stage, operation_time)
    if stage_state in {
        DailyChecklistStage.Status.COMPLETED,
        DailyChecklistStage.Status.COMPLETED_LATE,
    }:
        raise ChecklistLockedError('Завершённый этап доступен только для чтения.')
    if stage.status != stage_state:
        stage.status = stage_state
        stage.save(update_fields=('status', 'updated_at'))
    if (
        status == ChecklistAnswer.Status.FAILED
        and locked_answer.daily_item.comment_required_on_failure
        and not comment.strip()
    ):
        raise AnswerValidationError(
            'Для невыполненного пункта обязателен комментарий.'
        )
    if (
        status == ChecklistAnswer.Status.NOT_APPLICABLE
        and not locked_answer.daily_item.allow_not_applicable
    ):
        raise AnswerValidationError(
            'Для этого пункта нельзя выбрать «не применимо».'
        )

    old_status = locked_answer.status
    old_integer_value = locked_answer.integer_value
    old_comment = locked_answer.comment
    is_saved_answer = locked_answer.answered_at is not None
    content_changed = (
        old_status != status
        or old_integer_value != integer_value
        or old_comment != comment
    )
    employee_changed = (
        action_employee is not None
        and locked_answer.last_edited_by_employee_id is not None
        and locked_answer.last_edited_by_employee_id != action_employee.pk
    )
    if is_saved_answer and (content_changed or employee_changed):
        reason = (change_reason or '').strip()
        if len(reason) < 5:
            raise AnswerValidationError(
                'Для изменения сохранённого ответа укажите причину '
                'не короче 5 символов.'
            )
    else:
        reason = ''

    locked_answer.status = status
    locked_answer.integer_value = integer_value
    locked_answer.comment = comment
    locked_answer.answered_by = actor
    if not is_saved_answer:
        locked_answer.answered_at = operation_time
        if action_employee is not None:
            locked_answer.answered_by_employee = action_employee
    if action_employee is not None:
        locked_answer.last_edited_by_employee = action_employee
    try:
        locked_answer.save(
            update_fields=(
                'status',
                'integer_value',
                'comment',
                'answered_by',
                'answered_by_employee',
                'last_edited_by_employee',
                'answered_at',
                'updated_at',
            )
        )
    except ValidationError as exc:
        raise AnswerValidationError(str(exc)) from exc

    if old_status != status:
        _write_audit_log(
            actor=actor,
            store=daily.store,
            obj=locked_answer,
            action=AuditLog.Action.ANSWER_STATUS_CHANGED,
            field_name='status',
            old_value=old_status,
            new_value=status,
            employee=action_employee,
            request_metadata=request_metadata,
        )
    if old_comment != comment:
        _write_audit_log(
            actor=actor,
            store=daily.store,
            obj=locked_answer,
            action=AuditLog.Action.ANSWER_COMMENT_CHANGED,
            field_name='comment',
            old_value=old_comment,
            new_value=comment,
            employee=action_employee,
            request_metadata=request_metadata,
        )
    if is_saved_answer and (content_changed or employee_changed):
        ip_address, user_agent = _request_metadata_values(request_metadata)
        revision = AnswerRevision.objects.create(
            answer=locked_answer,
            daily_item=locked_answer.daily_item,
            changed_by_user=actor,
            changed_by_employee=action_employee,
            previous_status=old_status,
            new_status=status,
            previous_integer_value=old_integer_value,
            new_integer_value=integer_value,
            previous_comment=old_comment,
            new_comment=comment,
            change_reason=reason,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        _write_audit_log(
            actor=actor,
            employee=action_employee,
            store=daily.store,
            obj=revision,
            action=AuditLog.Action.ANSWER_REVISED,
            field_name='answer',
            old_value={
                'status': old_status,
                'integer_value': old_integer_value,
                'comment': old_comment,
            },
            new_value={
                'status': status,
                'integer_value': integer_value,
                'comment': comment,
                'change_reason': reason,
            },
            request_metadata=request_metadata,
        )
    if action_employee is not None:
        stage.last_edited_by_employee = action_employee
        stage.save(update_fields=('last_edited_by_employee', 'updated_at'))
    if not is_saved_answer or content_changed or employee_changed:
        from checklists.ad_hoc_tasks import sync_ad_hoc_task_from_answer

        sync_ad_hoc_task_from_answer(
            locked_answer,
            employee=action_employee,
            actor=actor,
            request_metadata=request_metadata,
        )
    return locked_answer


def _complete_daily_if_all_stages_finished(
    daily,
    stages,
    actor,
    request_metadata,
    employee=None,
):
    final_statuses = {
        DailyChecklistStage.Status.COMPLETED,
        DailyChecklistStage.Status.COMPLETED_LATE,
    }
    if len(stages) != len(STAGE_CODES) or any(
        stage.status not in final_statuses for stage in stages
    ):
        return False
    old_status = daily.status
    daily.status = DailyChecklist.Status.COMPLETED
    known_completion_times = [
        stage.completed_at for stage in stages if stage.completed_at is not None
    ]
    daily.completed_at = max(known_completion_times) if known_completion_times else None
    daily.save(update_fields=('status', 'completed_at', 'updated_at'))
    _write_audit_log(
        actor=actor,
        store=daily.store,
        obj=daily,
        action=AuditLog.Action.DAILY_CHECKLIST_COMPLETED,
        field_name='status',
        old_value=old_status,
        new_value=daily.status,
        employee=employee,
        request_metadata=request_metadata,
    )
    return True


@transaction.atomic
def complete_checklist_stage(
    stage,
    actor,
    employee=None,
    request_metadata=None,
    at=None,
):
    if employee is not None and not isinstance(employee, StoreEmployee):
        if request_metadata is None:
            request_metadata = employee
            employee = None
        else:
            raise OperationNotAllowedError('Некорректный сотрудник действия.')
    stage_location = DailyChecklistStage.objects.only(
        'daily_checklist_id'
    ).get(pk=stage.pk)
    daily = (
        DailyChecklist.objects.select_for_update()
        .select_related('store', 'employee', 'terminal_account')
        .get(pk=stage_location.daily_checklist_id)
    )
    stages = list(
        DailyChecklistStage.objects.select_for_update()
        .filter(daily_checklist=daily)
        .order_by('opens_at', 'id')
    )
    locked_stage = next(candidate for candidate in stages if candidate.pk == stage.pk)
    account_type, _ = _ensure_actor_can_edit_checklist(actor, daily)
    action_employee = _lock_action_employee(employee, daily, account_type)
    if daily.status == DailyChecklist.Status.COMPLETED:
        raise ChecklistLockedError('Чек-лист уже завершён.')

    completed_at = at or timezone.now()
    state = get_stage_state(locked_stage, completed_at)
    if state in {
        DailyChecklistStage.Status.COMPLETED,
        DailyChecklistStage.Status.COMPLETED_LATE,
    }:
        raise ChecklistLockedError('Этап уже завершён.')
    if not can_complete_stage(locked_stage, completed_at):
        available_local = get_stage_completion_available_at(
            locked_stage
        ).astimezone(_store_timezone(daily.store))
        raise ChecklistLockedError(
            'Завершить этап можно только после '
            f'{available_local:%H:%M}.'
        )

    answers = list(
        ChecklistAnswer.objects.select_for_update().select_related(
            'daily_item'
        ).filter(
            daily_item__daily_checklist=daily,
            daily_item__section_code=locked_stage.section_code,
        )
    )
    item_count = daily.items.filter(
        section_code=locked_stage.section_code
    ).count()
    if len(answers) != item_count:
        raise ChecklistCompletionError(
            'У каждого пункта этапа должен быть ответ.'
        )
    if any(
        answer.daily_item.is_required
        and (
            (
                answer.daily_item.answer_type_snapshot
                == ChecklistItem.AnswerType.INTEGER
                and answer.integer_value is None
            )
            or (
                answer.daily_item.answer_type_snapshot
                == ChecklistItem.AnswerType.STATUS
                and answer.status == ChecklistAnswer.Status.PENDING
            )
        )
        for answer in answers
    ):
        raise ChecklistCompletionError(
            'Чтобы завершить чек-лист, ответьте на все пункты.'
        )

    old_status = locked_stage.status
    result = (
        DailyChecklistStage.Status.COMPLETED
        if completed_at <= locked_stage.deadline_at
        else DailyChecklistStage.Status.COMPLETED_LATE
    )
    locked_stage.status = result
    locked_stage.completed_at = completed_at
    locked_stage.completed_by_employee = action_employee
    if action_employee is not None:
        locked_stage.last_edited_by_employee = action_employee
    if locked_stage.first_completed_at is None:
        locked_stage.first_completed_at = completed_at
        locked_stage.first_completed_by_employee = action_employee
    locked_stage.save(
        update_fields=(
            'status',
            'completed_at',
            'completed_by_employee',
            'last_edited_by_employee',
            'first_completed_at',
            'first_completed_by_employee',
            'updated_at',
        )
    )
    _write_audit_log(
        actor=actor,
        store=daily.store,
        obj=locked_stage,
        action=AuditLog.Action.CHECKLIST_STAGE_COMPLETED,
        field_name='status',
        old_value={
            'status': old_status,
            'section_code': locked_stage.section_code,
            'deadline_at': locked_stage.deadline_at.isoformat(),
            'completed_at': None,
            'result': old_status,
        },
        new_value={
            'status': result,
            'section_code': locked_stage.section_code,
            'deadline_at': locked_stage.deadline_at.isoformat(),
            'completed_at': completed_at.isoformat(),
            'result': result,
        },
        employee=action_employee,
        request_metadata=request_metadata,
    )
    from checklists.notifications import (
        create_completed_late_notification,
        create_completed_with_issues_notification,
    )

    issues_notification = create_completed_with_issues_notification(locked_stage)
    if (
        issues_notification is None
        and result == DailyChecklistStage.Status.COMPLETED_LATE
    ):
        create_completed_late_notification(locked_stage)
    _complete_daily_if_all_stages_finished(
        daily,
        stages,
        actor,
        request_metadata,
        action_employee,
    )
    return locked_stage


@transaction.atomic
def complete_daily_checklist(daily_checklist, actor, request_metadata=None):
    """Совместимый API: день закрывается только уже завершёнными этапами."""
    daily = (
        DailyChecklist.objects.select_for_update()
        .select_related('store', 'employee', 'terminal_account')
        .get(pk=daily_checklist.pk)
    )
    _ensure_actor_can_edit_checklist(actor, daily)
    if daily.status == DailyChecklist.Status.COMPLETED:
        raise ChecklistLockedError('Чек-лист уже завершён.')
    stages = list(
        DailyChecklistStage.objects.select_for_update()
        .filter(daily_checklist=daily)
        .order_by('opens_at', 'id')
    )
    if not _complete_daily_if_all_stages_finished(
        daily,
        stages,
        actor,
        request_metadata,
        None,
    ):
        raise ChecklistCompletionError(
            'Чек-лист завершится автоматически после всех трёх этапов.'
        )
    return daily


@transaction.atomic
def reopen_daily_checklist(
    daily_checklist,
    actor,
    request_metadata=None,
    section_code=None,
    at=None,
):
    daily = (
        DailyChecklist.objects.select_for_update()
        .select_related('store')
        .get(pk=daily_checklist.pk)
    )
    _ensure_actor_is_manager(actor, daily.store)
    if section_code is None and daily.status != DailyChecklist.Status.COMPLETED:
        raise ChecklistLockedError(
            'Повторно открыть можно только завершённый чек-лист.'
        )

    stages = list(
        DailyChecklistStage.objects.select_for_update()
        .filter(daily_checklist=daily)
        .order_by('opens_at', 'id')
    )
    if section_code and section_code not in DailyChecklistStage.SectionCode.values:
        raise OperationNotAllowedError('Неизвестный этап чек-листа.')
    selected_stages = [
        stage
        for stage in stages
        if section_code is None or stage.section_code == section_code
    ]
    if not selected_stages:
        raise OperationNotAllowedError('Этап чек-листа не найден.')
    final_stage_statuses = {
        DailyChecklistStage.Status.COMPLETED,
        DailyChecklistStage.Status.COMPLETED_LATE,
    }
    if any(stage.status not in final_stage_statuses for stage in selected_stages):
        raise ChecklistLockedError(
            'Повторно открыть можно только завершённый этап.'
        )
    reopened_at = at or timezone.now()
    for stage in selected_stages:
        stage.completed_at = None
        stage.completed_by_employee = None
        stage.reopened_count += 1
        stage.status = _scheduled_stage_state(stage, reopened_at)
        stage.save(
            update_fields=(
                'status',
                'completed_at',
                'completed_by_employee',
                'reopened_count',
                'updated_at',
            )
        )

    old_status = daily.status
    daily.status = DailyChecklist.Status.REOPENED
    daily.completed_at = None
    daily.reopened_at = reopened_at
    daily.reopened_by = actor
    daily.save(
        update_fields=(
            'status',
            'completed_at',
            'reopened_at',
            'reopened_by',
            'updated_at',
        )
    )
    _write_audit_log(
        actor=actor,
        store=daily.store,
        obj=daily,
        action=AuditLog.Action.DAILY_CHECKLIST_REOPENED,
        field_name='status',
        old_value=old_status,
        new_value={
            'status': daily.status,
            'section_code': section_code,
        },
        request_metadata=request_metadata,
    )
    return daily


def get_employee_stage_participation(store, work_date):
    assignments = list(
        DailyShiftAssignment.objects.filter(
            store=store,
            work_date=work_date,
        ).select_related('employee')
    )
    result = []
    for assignment in assignments:
        employee = assignment.employee
        first_answers = ChecklistAnswer.objects.filter(
            daily_item__daily_checklist__store=store,
            daily_item__daily_checklist__checklist_date=work_date,
            answered_by_employee=employee,
        )
        revisions = AnswerRevision.objects.filter(
            answer__daily_item__daily_checklist__store=store,
            answer__daily_item__daily_checklist__checklist_date=work_date,
            changed_by_employee=employee,
        )
        completed_stages = DailyChecklistStage.objects.filter(
            daily_checklist__store=store,
            daily_checklist__checklist_date=work_date,
            completed_by_employee=employee,
        )
        sections = set(
            first_answers.values_list('daily_item__section_code', flat=True)
        )
        sections.update(
            revisions.values_list(
                'answer__daily_item__section_code',
                flat=True,
            )
        )
        sections.update(
            completed_stages.values_list('section_code', flat=True)
        )
        row = {
            'employee': employee,
            'worked_by_schedule': True,
            'is_responsible_for_checklist': (
                assignment.is_responsible_for_checklist
            ),
            'opening_participated': 'opening' in sections,
            'during_day_participated': 'during_day' in sections,
            'closing_participated': 'closing' in sections,
            'answers_filled': first_answers.count(),
            'answers_changed': revisions.count(),
            'stages_completed': list(
                completed_stages.values_list('section_code', flat=True)
            ),
        }
        row['no_participation'] = not sections
        result.append(row)
    return result


def get_missing_employee_actions(store, work_date):
    participation = get_employee_stage_participation(store, work_date)
    stages_without_actions = []
    for section_code in DailyChecklistStage.SectionCode.values:
        has_answers = ChecklistAnswer.objects.filter(
            daily_item__daily_checklist__store=store,
            daily_item__daily_checklist__checklist_date=work_date,
            daily_item__section_code=section_code,
            answered_at__isnull=False,
        ).exists()
        has_completion = DailyChecklistStage.objects.filter(
            daily_checklist__store=store,
            daily_checklist__checklist_date=work_date,
            section_code=section_code,
            completed_at__isnull=False,
        ).exists()
        if not has_answers and not has_completion:
            stages_without_actions.append(section_code)
    return {
        'employees_without_actions': [
            row['employee'] for row in participation if row['no_participation']
        ],
        'responsible_without_participation': [
            row['employee']
            for row in participation
            if row['is_responsible_for_checklist'] and row['no_participation']
        ],
        'stages_without_actions': stages_without_actions,
    }


def get_shift_completion_report(store, work_date):
    participation = get_employee_stage_participation(store, work_date)
    return {
        'store': store,
        'work_date': work_date,
        'employees': participation,
        'missing': get_missing_employee_actions(store, work_date),
    }


@transaction.atomic
def publish_template_version(version, actor, request_metadata=None):
    version_location = ChecklistTemplateVersion.objects.only(
        'template_id'
    ).get(pk=version.pk)
    template = (
        ChecklistTemplate.objects.select_for_update()
        .select_related('store')
        .get(pk=version_location.template_id)
    )
    locked_versions = list(
        ChecklistTemplateVersion.objects.select_for_update()
        .filter(template=template)
        .order_by('pk')
    )
    locked_version = next(
        (candidate for candidate in locked_versions if candidate.pk == version.pk),
        None,
    )
    if locked_version is None:
        raise ChecklistTemplateVersion.DoesNotExist
    if actor is not None:
        _ensure_actor_is_manager(actor, template.store)
    if locked_version.status != ChecklistTemplateVersion.Status.DRAFT:
        raise InvalidTemplateVersionStateError(
            'Опубликовать можно только черновик версии.'
        )

    published_versions = [
        candidate
        for candidate in locked_versions
        if candidate.pk != locked_version.pk
        and candidate.status == ChecklistTemplateVersion.Status.PUBLISHED
    ]
    if published_versions:
        ChecklistTemplateVersion.objects.filter(
            pk__in=[candidate.pk for candidate in published_versions]
        ).update(status=ChecklistTemplateVersion.Status.ARCHIVED)

    locked_version.status = ChecklistTemplateVersion.Status.PUBLISHED
    locked_version.published_at = timezone.now()
    if locked_version.created_by_id is None and actor is not None:
        locked_version.created_by = actor
    locked_version._publishing_via_service = True
    try:
        locked_version.save(
            update_fields=('status', 'published_at', 'created_by')
        )
    finally:
        del locked_version._publishing_via_service
    _write_audit_log(
        actor=actor,
        store=template.store,
        obj=locked_version,
        action=AuditLog.Action.TEMPLATE_VERSION_PUBLISHED,
        field_name='status',
        old_value=ChecklistTemplateVersion.Status.DRAFT,
        new_value=locked_version.status,
        request_metadata=request_metadata,
    )
    return locked_version
