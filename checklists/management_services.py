from dataclasses import dataclass
from datetime import time, timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.contrib.sessions.models import Session
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Max, Min
from django.utils import timezone

from checklists.access_control import (
    can_manage_stores,
    can_manage_store_employees,
    can_manage_store_notifications,
    can_manage_store_questions,
    can_manage_store_schedule,
    can_manage_store_shifts,
    can_manage_system_users,
    can_reopen_store_stage,
)
from checklists.exceptions import OperationNotAllowedError, TemplateConfigurationError
from checklists.models import (
    AnswerRevision,
    AuditLog,
    ChecklistAnswer,
    ChecklistItem,
    ChecklistNotification,
    ChecklistSection,
    ChecklistTemplate,
    ChecklistTemplateVersion,
    DailyChecklist,
    DailyChecklistItem,
    DailyChecklistStage,
    DailyShiftAssignment,
    EmployeeProfile,
    ShiftTemplate,
    Store,
    StoreChecklistSchedule,
    StoreEmployee,
    StoreNotificationSettings,
    StoreTerminalAccount,
    StoreAdHocTask,
    TelegramOutboundMessage,
    TelegramStoreBinding,
    TelegramStoreChat,
    UserStoreMembership,
)
from checklists.notifications import send_telegram_message
from checklists.services import publish_template_version, reopen_daily_checklist


def _metadata_values(request_metadata):
    if not request_metadata:
        return None, None
    if hasattr(request_metadata, 'META'):
        return (
            request_metadata.META.get('REMOTE_ADDR'),
            request_metadata.META.get('HTTP_USER_AGENT'),
        )
    return request_metadata.get('ip_address'), request_metadata.get('user_agent')


def _audit(
    *,
    actor,
    store,
    obj,
    action,
    old_value=None,
    new_value=None,
    request_metadata=None,
):
    ip_address, user_agent = _metadata_values(request_metadata)
    return AuditLog.objects.create(
        actor=actor,
        employee=None,
        store=store,
        object_type=obj._meta.label_lower,
        object_id=str(obj.pk),
        action=action,
        old_value=old_value,
        new_value=new_value,
        ip_address=ip_address,
        user_agent=user_agent,
    )


def _require(permission, message):
    if not permission:
        raise OperationNotAllowedError(message)


def _membership_role_for_profile(role):
    if role == EmployeeProfile.Role.STORE_DIRECTOR:
        return UserStoreMembership.Role.DIRECTOR
    if role == EmployeeProfile.Role.STORE_ACCOUNT:
        return UserStoreMembership.Role.EMPLOYEE
    return None


@transaction.atomic
def set_user_store_membership(
    *,
    user,
    store,
    role_in_store,
    actor,
    is_active=True,
    request_metadata=None,
):
    _require(
        can_manage_system_users(actor),
        'Нельзя изменять связи пользователей с магазинами.',
    )
    locked_store = Store.objects.select_for_update().get(
        pk=store.pk,
        is_active=True,
    )
    locked_user = get_user_model().objects.select_for_update().get(pk=user.pk)
    membership = UserStoreMembership.objects.select_for_update().filter(
        user=locked_user,
        store=locked_store,
    ).first()
    old_value = None
    created = membership is None
    if membership is None:
        membership = UserStoreMembership(
            user=locked_user,
            store=locked_store,
        )
    else:
        old_value = {
            'role_in_store': membership.role_in_store,
            'is_active': membership.is_active,
        }
    membership.role_in_store = role_in_store
    membership.is_active = is_active
    membership.full_clean()
    membership.save()
    _audit(
        actor=actor,
        store=locked_store,
        obj=membership,
        action=(
            AuditLog.Action.USER_STORE_MEMBERSHIP_CREATED
            if created
            else AuditLog.Action.USER_STORE_MEMBERSHIP_UPDATED
        ),
        old_value=old_value,
        new_value={
            'user_id': locked_user.pk,
            'role_in_store': role_in_store,
            'is_active': is_active,
        },
        request_metadata=request_metadata,
    )
    return membership


@transaction.atomic
def remove_user_store_membership(
    *,
    membership,
    actor,
    request_metadata=None,
):
    _require(
        can_manage_system_users(actor),
        'Нельзя удалять связи пользователей с магазинами.',
    )
    locked = UserStoreMembership.objects.select_for_update().select_related(
        'store',
        'user',
    ).get(pk=membership.pk)
    _audit(
        actor=actor,
        store=locked.store,
        obj=locked,
        action=AuditLog.Action.USER_STORE_MEMBERSHIP_DELETED,
        old_value={
            'user_id': locked.user_id,
            'role_in_store': locked.role_in_store,
            'is_active': locked.is_active,
        },
        request_metadata=request_metadata,
    )
    locked.delete()


@dataclass(frozen=True)
class StoreDeletionSummary:
    can_hard_delete: bool
    directors_count: int
    store_accounts_count: int
    profiles_count: int
    terminal_accounts_count: int
    employees_count: int
    shifts_count: int
    templates_count: int
    versions_count: int
    daily_checklists_count: int
    stages_count: int
    daily_items_count: int
    answers_count: int
    revisions_count: int
    notifications_count: int
    telegram_chats_count: int
    telegram_bindings_count: int
    telegram_outbound_count: int
    ad_hoc_tasks_count: int
    telegram_history_count: int
    audit_count: int
    audit_will_be_deleted: bool
    technical_settings_count: int
    schedule_configured: bool
    notifications_configured: bool
    blocking_reasons: tuple[str, ...]

    def audit_metadata(self):
        return {
            'directors_count': self.directors_count,
            'store_accounts_count': self.store_accounts_count,
            'employees_count': self.employees_count,
            'shifts_count': self.shifts_count,
            'templates_count': self.templates_count,
            'versions_count': self.versions_count,
            'daily_checklists_count': self.daily_checklists_count,
            'answers_count': self.answers_count,
            'revisions_count': self.revisions_count,
        }


def _model_values(obj, fields):
    values = {}
    for field in fields:
        value = getattr(obj, field)
        if hasattr(value, 'isoformat'):
            value = value.isoformat()
        elif hasattr(value, 'storage') and hasattr(value, 'name'):
            value = value.name
        values[field] = value
    return values


def _store_schedule_is_configured(store):
    schedule = StoreChecklistSchedule.objects.filter(store=store).first()
    if schedule is None:
        return False
    fields = (
        'opening_time',
        'morning_deadline',
        'daytime_deadline',
        'closing_deadline',
        'morning_completion_window_minutes',
        'day_completion_window_minutes',
        'evening_completion_window_minutes',
        'warning_minutes_before',
        'notifications_enabled',
        'is_active',
    )
    return any(
        getattr(schedule, field) != schedule._meta.get_field(field).get_default()
        for field in fields
    )


def _store_notifications_are_configured(store):
    settings_obj = StoreNotificationSettings.objects.filter(store=store).first()
    if settings_obj is None:
        return False
    return (
        settings_obj.telegram_chat_id,
        settings_obj.warning_enabled,
        settings_obj.overdue_enabled,
        settings_obj.completed_late_enabled,
        settings_obj.is_active,
    ) != ('', True, True, True, False)


def get_store_deletion_summary(store):
    profiles = EmployeeProfile.objects.filter(store=store)
    directors_count = profiles.filter(
        role=EmployeeProfile.Role.STORE_DIRECTOR
    ).count()
    store_accounts_count = profiles.filter(
        role=EmployeeProfile.Role.STORE_ACCOUNT
    ).count()
    profiles_count = profiles.count()
    terminal_accounts_count = StoreTerminalAccount.objects.filter(
        store=store
    ).count()
    employees_count = StoreEmployee.objects.filter(store=store).count()
    shifts_count = DailyShiftAssignment.objects.filter(store=store).count()
    templates_count = ChecklistTemplate.objects.filter(store=store).count()
    versions_count = ChecklistTemplateVersion.objects.filter(
        template__store=store
    ).count()
    daily_checklists_count = DailyChecklist.objects.filter(store=store).count()
    stages_count = DailyChecklistStage.objects.filter(
        daily_checklist__store=store
    ).count()
    daily_items_count = DailyChecklistItem.objects.filter(
        daily_checklist__store=store
    ).count()
    answers_count = ChecklistAnswer.objects.filter(
        daily_item__daily_checklist__store=store
    ).count()
    revisions_count = AnswerRevision.objects.filter(
        answer__daily_item__daily_checklist__store=store
    ).count()
    notifications_count = ChecklistNotification.objects.filter(
        stage__daily_checklist__store=store
    ).count()
    telegram_chats_count = TelegramStoreChat.objects.filter(store=store).count()
    telegram_bindings_count = TelegramStoreBinding.objects.filter(store=store).count()
    telegram_outbound_count = TelegramOutboundMessage.objects.filter(store=store).count()
    ad_hoc_tasks_count = StoreAdHocTask.objects.filter(store=store).count()
    audit_count = AuditLog.objects.filter(store=store).count()
    schedule_configured = _store_schedule_is_configured(store)
    notifications_configured = _store_notifications_are_configured(store)
    technical_settings_count = (
        StoreChecklistSchedule.objects.filter(store=store).count()
        + StoreNotificationSettings.objects.filter(store=store).count()
    )

    reasons = []
    for count, reason in (
        (profiles_count, 'Есть пользовательские профили.'),
        (terminal_accounts_count, 'Есть терминальный аккаунт.'),
        (employees_count, 'Есть сотрудники магазина.'),
        (shifts_count, 'Есть назначения на смены.'),
        (templates_count, 'Есть шаблоны чек-листов.'),
        (versions_count, 'Есть версии шаблонов.'),
        (daily_checklists_count, 'Есть ежедневные чек-листы.'),
        (stages_count, 'Есть исторические этапы.'),
        (daily_items_count, 'Есть снимки вопросов.'),
        (answers_count, 'Есть ответы.'),
        (revisions_count, 'Есть история изменений ответов.'),
        (notifications_count, 'Есть история Telegram-уведомлений.'),
        (telegram_chats_count, 'Есть настроенные Telegram-чаты.'),
        (telegram_bindings_count, 'Есть привязки Telegram-пользователей.'),
        (telegram_outbound_count, 'Есть история исходящих Telegram-сообщений.'),
        (ad_hoc_tasks_count, 'Есть разовые задачи магазина.'),
    ):
        if count:
            reasons.append(reason)
    if schedule_configured:
        reasons.append('Расписание изменено относительно технических значений.')
    if notifications_configured:
        reasons.append('Настройки Telegram содержат бизнес-конфигурацию.')

    can_hard_delete = not reasons
    return StoreDeletionSummary(
        can_hard_delete=can_hard_delete,
        directors_count=directors_count,
        store_accounts_count=store_accounts_count,
        profiles_count=profiles_count,
        terminal_accounts_count=terminal_accounts_count,
        employees_count=employees_count,
        shifts_count=shifts_count,
        templates_count=templates_count,
        versions_count=versions_count,
        daily_checklists_count=daily_checklists_count,
        stages_count=stages_count,
        daily_items_count=daily_items_count,
        answers_count=answers_count,
        revisions_count=revisions_count,
        notifications_count=notifications_count,
        telegram_chats_count=telegram_chats_count,
        telegram_bindings_count=telegram_bindings_count,
        telegram_outbound_count=telegram_outbound_count,
        ad_hoc_tasks_count=ad_hoc_tasks_count,
        telegram_history_count=(
            notifications_count
            + telegram_outbound_count
            + telegram_bindings_count
        ),
        audit_count=audit_count,
        audit_will_be_deleted=can_hard_delete and audit_count > 0,
        technical_settings_count=technical_settings_count,
        schedule_configured=schedule_configured,
        notifications_configured=notifications_configured,
        blocking_reasons=tuple(reasons),
    )


def _validate_role_store(role, store, is_active=True):
    if role in {
        EmployeeProfile.Role.STORE_ACCOUNT,
        EmployeeProfile.Role.STORE_DIRECTOR,
    }:
        if store is None:
            raise ValidationError({'store': 'Магазин обязателен.'})
        if is_active and not store.is_active:
            raise ValidationError({'store': 'Магазин неактивен.'})
    elif role == EmployeeProfile.Role.SYSTEM_ADMIN:
        if store is not None:
            raise ValidationError({'store': 'Администратор системы не имеет магазина.'})
    else:
        raise ValidationError({'role': 'Неизвестная роль.'})


def _invalidate_user_sessions(user_ids, *, exclude_session_key=None):
    user_ids = {str(user_id) for user_id in user_ids}
    if not user_ids:
        return 0
    deleted = 0
    for session in Session.objects.filter(expire_date__gte=timezone.now()).iterator():
        if session.session_key == exclude_session_key:
            continue
        if str(session.get_decoded().get('_auth_user_id')) in user_ids:
            session.delete()
            deleted += 1
    return deleted


def _ensure_store_account_slot(store, *, exclude_profile_id=None):
    query = EmployeeProfile.objects.select_for_update().filter(
        store=store,
        role=EmployeeProfile.Role.STORE_ACCOUNT,
        is_active=True,
        user__is_active=True,
    )
    if exclude_profile_id:
        query = query.exclude(pk=exclude_profile_id)
    if query.exists():
        raise ValidationError(
            {'store': 'У магазина уже есть активный аккаунт.'}
        )


@transaction.atomic
def create_store_with_defaults(
    *,
    actor,
    name,
    code,
    timezone_name,
    is_active=True,
    logo=None,
    terminal_username=None,
    terminal_password=None,
    request_metadata=None,
):
    _require(can_manage_stores(actor), 'Нельзя создать магазин.')
    store = Store(
        name=name,
        code=code,
        timezone=timezone_name,
        logo=logo,
        is_active=is_active,
    )
    store.full_clean()
    store.save()
    StoreChecklistSchedule.objects.create(store=store)
    StoreNotificationSettings.objects.create(store=store, is_active=False)
    ShiftTemplate.objects.bulk_create(
        (
            ShiftTemplate(
                store=store,
                name='День',
                shift_type=DailyShiftAssignment.ShiftType.WORK,
                shift_start=time(10),
                shift_end=time(22),
                sort_order=10,
            ),
            ShiftTemplate(
                store=store,
                name='Сервис',
                shift_type=DailyShiftAssignment.ShiftType.SERVICE,
                shift_start=time(9),
                shift_end=time(18),
                sort_order=20,
            ),
            ShiftTemplate(
                store=store,
                name='КЦ',
                shift_type=DailyShiftAssignment.ShiftType.WORK,
                shift_start=time(9),
                shift_end=time(21),
                sort_order=30,
            ),
        )
    )
    _audit(
        actor=actor,
        store=store,
        obj=store,
        action=AuditLog.Action.STORE_CREATED,
        new_value=_model_values(store, ('name', 'code', 'timezone', 'is_active')),
        request_metadata=request_metadata,
    )
    if terminal_username or terminal_password:
        if not (terminal_username and terminal_password):
            raise ValidationError('Для аккаунта магазина нужны логин и пароль.')
        create_managed_user(
            actor=actor,
            username=terminal_username,
            password=terminal_password,
            role=EmployeeProfile.Role.STORE_ACCOUNT,
            store=store,
            is_active=True,
            request_metadata=request_metadata,
        )
    return store


@transaction.atomic
def update_store(store, data, actor, request_metadata=None):
    _require(can_manage_stores(actor), 'Нельзя изменить магазин.')
    locked = Store.objects.select_for_update().get(pk=store.pk)
    fields = ('name', 'code', 'timezone', 'logo', 'is_active')
    old = _model_values(locked, fields)
    for field in fields:
        if field in data:
            setattr(locked, field, data[field])
    locked.full_clean()
    locked.save(update_fields=(*fields, 'updated_at'))
    action = AuditLog.Action.STORE_UPDATED
    if old['is_active'] != locked.is_active:
        action = (
            AuditLog.Action.STORE_ACTIVATED
            if locked.is_active
            else AuditLog.Action.STORE_DEACTIVATED
        )
        if not locked.is_active:
            EmployeeProfile.objects.filter(store=locked).update(is_active=False)
            StoreTerminalAccount.objects.filter(store=locked).update(is_active=False)
            TelegramStoreBinding.objects.filter(store=locked).update(is_active=False)
            TelegramStoreChat.objects.filter(store=locked).update(is_active=False)
    _audit(
        actor=actor,
        store=locked,
        obj=locked,
        action=action,
        old_value=old,
        new_value=_model_values(locked, fields),
        request_metadata=request_metadata,
    )
    return locked


@transaction.atomic
def update_store_logo(store, logo, actor, request_metadata=None):
    _require(
        can_manage_store_schedule(actor, store),
        'Нельзя изменить логотип магазина.',
    )
    locked = Store.objects.select_for_update().get(pk=store.pk)
    old_name = locked.logo.name if locked.logo else ''
    locked.logo = logo or None
    locked.save(update_fields=('logo', 'updated_at'))
    _audit(
        actor=actor,
        store=locked,
        obj=locked,
        action=AuditLog.Action.STORE_UPDATED,
        old_value={'logo': old_name},
        new_value={'logo': locked.logo.name if locked.logo else ''},
        request_metadata=request_metadata,
    )
    return locked


@transaction.atomic
def delete_store_safely(*, actor, store, request_metadata=None):
    _require(can_manage_stores(actor), 'Нельзя удалить магазин.')
    locked = Store.objects.select_for_update().get(pk=store.pk)
    summary = get_store_deletion_summary(locked)
    store_metadata = {
        'deleted_store_id': locked.pk,
        'deleted_store_name': locked.name,
        'deleted_store_code': locked.code,
        'deleted_store_was_active': locked.is_active,
    }
    if summary.can_hard_delete:
        audit_query = AuditLog.objects.select_for_update().filter(store=locked)
        audit_ids = list(audit_query.values_list('pk', flat=True))
        deleted_audit_entries_count = len(audit_ids)
        AuditLog.objects.filter(pk__in=audit_ids).delete()
        technical_settings_count = (
            StoreChecklistSchedule.objects.select_for_update()
            .filter(store=locked)
            .count()
            + StoreNotificationSettings.objects.select_for_update()
            .filter(store=locked)
            .count()
        )
        StoreChecklistSchedule.objects.select_for_update().filter(
            store=locked
        ).delete()
        StoreNotificationSettings.objects.select_for_update().filter(
            store=locked
        ).delete()
        deleted_id = locked.pk
        object_repr = f'{locked.name} ({locked.code})'
        locked.delete()
        ip_address, user_agent = _metadata_values(request_metadata)
        deleted_at = timezone.now()
        AuditLog.objects.create(
            actor=actor,
            employee=None,
            store=None,
            object_type=Store._meta.label_lower,
            object_id=str(deleted_id),
            action=AuditLog.Action.STORE_DELETED,
            old_value=None,
            new_value={
                **store_metadata,
                'object_repr': object_repr,
                'deleted_audit_entries_count': deleted_audit_entries_count,
                'deleted_technical_settings_count': technical_settings_count,
                'method': 'hard_delete_with_audit_cleanup',
                'deleted_at': deleted_at.isoformat(),
            },
            ip_address=ip_address,
            user_agent=user_agent,
        )
        return {
            'method': 'hard_delete_with_audit_cleanup',
            'store_id': deleted_id,
            'summary': summary,
            'deleted_audit_entries_count': deleted_audit_entries_count,
            'deleted_technical_settings_count': technical_settings_count,
        }

    profiles = list(
        EmployeeProfile.objects.select_for_update()
        .select_related('user')
        .filter(store=locked)
    )
    user_ids = [profile.user_id for profile in profiles]
    if actor.pk in user_ids:
        raise OperationNotAllowedError(
            'Нельзя отключить собственный административный доступ.'
        )
    terminals = list(
        StoreTerminalAccount.objects.select_for_update().filter(store=locked)
    )
    already_processed = (
        not locked.is_active
        and all(not profile.is_active and not profile.user.is_active for profile in profiles)
        and all(not terminal.is_active for terminal in terminals)
        and AuditLog.objects.filter(
            store=locked,
            action=AuditLog.Action.STORE_DEACTIVATED_WITH_HISTORY,
        ).exists()
    )
    if already_processed:
        return {
            'method': 'already_deactivated',
            'store_id': locked.pk,
            'summary': summary,
        }

    User = get_user_model()
    list(User.objects.select_for_update().filter(pk__in=user_ids))
    User.objects.filter(pk__in=user_ids).update(is_active=False)
    EmployeeProfile.objects.filter(pk__in=[profile.pk for profile in profiles]).update(
        is_active=False,
        updated_at=timezone.now(),
    )
    StoreTerminalAccount.objects.filter(pk__in=[item.pk for item in terminals]).update(
        is_active=False,
        updated_at=timezone.now(),
    )
    invalidated_sessions = _invalidate_user_sessions(user_ids)
    locked.is_active = False
    locked.save(update_fields=('is_active', 'updated_at'))
    _audit(
        actor=actor,
        store=locked,
        obj=locked,
        action=AuditLog.Action.STORE_DEACTIVATED_WITH_HISTORY,
        old_value={
            'store_id': locked.pk,
            'name': locked.name,
            'code': locked.code,
            'is_active': store.is_active,
        },
        new_value={
            'store_id': locked.pk,
            'name': locked.name,
            'code': locked.code,
            'is_active': False,
            'method': 'deactivated_with_history',
            'invalidated_sessions': invalidated_sessions,
            'blocking_reasons': list(summary.blocking_reasons),
            **summary.audit_metadata(),
        },
        request_metadata=request_metadata,
    )
    return {
        'method': 'deactivated_with_history',
        'store_id': locked.pk,
        'summary': summary,
        'invalidated_sessions': invalidated_sessions,
    }


def _audit_range(query):
    return query.aggregate(
        first_created_at=Min('created_at'),
        last_created_at=Max('created_at'),
    )


@transaction.atomic
def clear_store_audit_log(*, actor, store, request_metadata=None):
    _require(can_manage_stores(actor), 'Нельзя очистить журнал действий.')
    locked = Store.objects.select_for_update().get(pk=store.pk)
    audit_query = AuditLog.objects.select_for_update().filter(store=locked)
    audit_ids = list(audit_query.values_list('pk', flat=True))
    deleted_entries_count = len(audit_ids)
    if not deleted_entries_count:
        return {
            'method': 'already_empty',
            'store_id': locked.pk,
            'deleted_entries_count': 0,
        }

    date_range = _audit_range(audit_query)
    store_data = {
        'cleared_store_id': locked.pk,
        'cleared_store_name': locked.name,
        'cleared_store_code': locked.code,
    }
    AuditLog.objects.filter(pk__in=audit_ids).delete()
    cleared_at = timezone.now()
    ip_address, user_agent = _metadata_values(request_metadata)
    AuditLog.objects.create(
        actor=actor,
        employee=None,
        store=None,
        object_type=AuditLog._meta.label_lower,
        object_id=str(locked.pk),
        action=AuditLog.Action.AUDIT_LOG_CLEARED,
        new_value={
            'scope': 'store',
            **store_data,
            'deleted_entries_count': deleted_entries_count,
            'first_deleted_entry_at': (
                date_range['first_created_at'].isoformat()
                if date_range['first_created_at'] else None
            ),
            'last_deleted_entry_at': (
                date_range['last_created_at'].isoformat()
                if date_range['last_created_at'] else None
            ),
            'method': 'manual_store_audit_cleanup',
            'cleared_at': cleared_at.isoformat(),
        },
        ip_address=ip_address,
        user_agent=user_agent,
    )
    return {
        'method': 'manual_store_audit_cleanup',
        'store_id': locked.pk,
        'deleted_entries_count': deleted_entries_count,
    }


@transaction.atomic
def clear_all_audit_logs(*, actor, request_metadata=None):
    _require(can_manage_stores(actor), 'Нельзя очистить журнал действий.')
    audit_query = AuditLog.objects.select_for_update().all()
    audit_ids = list(audit_query.values_list('pk', flat=True))
    aggregate = audit_query.aggregate(
        first_created_at=Min('created_at'),
        last_created_at=Max('created_at'),
    )
    deleted_entries_count = len(audit_ids)
    affected_stores_count = audit_query.exclude(store=None).values(
        'store_id'
    ).distinct().count()
    deleted_global_entries_count = audit_query.filter(store=None).count()
    AuditLog.objects.filter(pk__in=audit_ids).delete()
    cleared_at = timezone.now()
    ip_address, user_agent = _metadata_values(request_metadata)
    AuditLog.objects.create(
        actor=actor,
        employee=None,
        store=None,
        object_type=AuditLog._meta.label_lower,
        object_id='all',
        action=AuditLog.Action.AUDIT_LOG_CLEARED,
        new_value={
            'scope': 'all',
            'deleted_entries_count': deleted_entries_count,
            'affected_stores_count': affected_stores_count,
            'deleted_global_entries_count': deleted_global_entries_count,
            'first_deleted_entry_at': (
                aggregate['first_created_at'].isoformat()
                if aggregate['first_created_at'] else None
            ),
            'last_deleted_entry_at': (
                aggregate['last_created_at'].isoformat()
                if aggregate['last_created_at'] else None
            ),
            'method': 'manual_full_audit_cleanup',
            'cleared_at': cleared_at.isoformat(),
        },
        ip_address=ip_address,
        user_agent=user_agent,
    )
    return {
        'method': 'manual_full_audit_cleanup',
        'deleted_entries_count': deleted_entries_count,
        'affected_stores_count': affected_stores_count,
        'deleted_global_entries_count': deleted_global_entries_count,
    }


@transaction.atomic
def create_managed_user(
    *,
    actor,
    username,
    password,
    role,
    store=None,
    first_name='',
    last_name='',
    email='',
    is_active=True,
    request_metadata=None,
):
    _require(can_manage_system_users(actor), 'Нельзя создать пользователя.')
    _validate_role_store(role, store, is_active)
    if store:
        Store.objects.select_for_update().get(pk=store.pk)
    if role == EmployeeProfile.Role.STORE_ACCOUNT and is_active:
        _ensure_store_account_slot(store)
    User = get_user_model()
    user = User(
        username=username.strip(),
        first_name=first_name.strip(),
        last_name=last_name.strip(),
        email=email.strip(),
        is_active=is_active,
        is_staff=False,
        is_superuser=False,
    )
    validate_password(password, user=user)
    user.set_password(password)
    user.full_clean()
    user.save()
    profile = EmployeeProfile(
        user=user,
        role=role,
        store=store,
        is_active=is_active,
    )
    profile.save()
    membership_role = _membership_role_for_profile(role)
    if store and membership_role:
        set_user_store_membership(
            user=user,
            store=store,
            role_in_store=membership_role,
            actor=actor,
            is_active=is_active,
            request_metadata=request_metadata,
        )
    if role == EmployeeProfile.Role.STORE_ACCOUNT:
        terminal = StoreTerminalAccount.objects.filter(store=store).first()
        if terminal:
            terminal.user = user
            terminal.is_active = is_active
            terminal.save()
        else:
            StoreTerminalAccount.objects.create(
                store=store,
                user=user,
                is_active=is_active,
            )
    _audit(
        actor=actor,
        store=store,
        obj=user,
        action=AuditLog.Action.USER_CREATED,
        new_value={
            'username': user.username,
            'role': role,
            'store_id': store.pk if store else None,
            'is_active': is_active,
        },
        request_metadata=request_metadata,
    )
    return user


@transaction.atomic
def update_managed_user(user, data, actor, request_metadata=None):
    _require(can_manage_system_users(actor), 'Нельзя изменить пользователя.')
    User = get_user_model()
    locked_user = User.objects.select_for_update().get(pk=user.pk)
    profile = EmployeeProfile.objects.select_for_update().get(user=locked_user)
    role = data['role']
    store = data.get('store')
    is_active = data.get('is_active', False)
    _validate_role_store(role, store, is_active)
    if role == EmployeeProfile.Role.STORE_ACCOUNT and is_active:
        Store.objects.select_for_update().get(pk=store.pk)
        _ensure_store_account_slot(store, exclude_profile_id=profile.pk)
    old = {
        'username': locked_user.username,
        'role': profile.role,
        'store_id': profile.store_id,
        'is_active': locked_user.is_active and profile.is_active,
    }
    if locked_user.pk == actor.pk and (
        role != EmployeeProfile.Role.SYSTEM_ADMIN or not is_active
    ):
        raise OperationNotAllowedError(
            'Нельзя лишить себя текущего административного доступа.'
        )
    locked_user.username = data['username'].strip()
    locked_user.first_name = data.get('first_name', '').strip()
    locked_user.last_name = data.get('last_name', '').strip()
    locked_user.email = data.get('email', '').strip()
    locked_user.is_active = is_active
    locked_user.is_staff = False
    locked_user.is_superuser = False
    locked_user.full_clean()

    terminal = StoreTerminalAccount.objects.select_for_update().filter(
        user=locked_user
    ).first()
    if terminal and (
        role != EmployeeProfile.Role.STORE_ACCOUNT
        or terminal.store_id != (store.pk if store else None)
        or not is_active
    ):
        terminal.is_active = False
        terminal.save(update_fields=('is_active', 'updated_at'))
    profile.role = role
    profile.store = store
    profile.is_active = is_active
    profile.save()
    membership_role = _membership_role_for_profile(role)
    if store and membership_role:
        set_user_store_membership(
            user=locked_user,
            store=store,
            role_in_store=membership_role,
            actor=actor,
            is_active=is_active,
            request_metadata=request_metadata,
        )
    if role == EmployeeProfile.Role.STORE_ACCOUNT:
        terminal_for_store = StoreTerminalAccount.objects.select_for_update().filter(
            store=store
        ).first()
        if terminal_for_store and terminal_for_store.user_id != locked_user.pk:
            terminal_for_store.user = locked_user
            terminal_for_store.is_active = is_active
            terminal_for_store.save()
        elif terminal_for_store:
            terminal_for_store.is_active = is_active
            terminal_for_store.save()
        else:
            StoreTerminalAccount.objects.create(
                store=store,
                user=locked_user,
                is_active=is_active,
            )
    locked_user.save()
    new = {
        'username': locked_user.username,
        'role': role,
        'store_id': store.pk if store else None,
        'is_active': is_active,
    }
    action = AuditLog.Action.USER_UPDATED
    if old['role'] != role:
        action = AuditLog.Action.USER_ROLE_CHANGED
    elif old['store_id'] != new['store_id']:
        action = AuditLog.Action.USER_STORE_CHANGED
    _audit(
        actor=actor,
        store=store,
        obj=locked_user,
        action=action,
        old_value=old,
        new_value=new,
        request_metadata=request_metadata,
    )
    return locked_user


@transaction.atomic
def activate_managed_user(user, actor, request_metadata=None):
    profile = EmployeeProfile.objects.select_for_update().select_related('store').get(
        user=user
    )
    data = {
        'username': user.username,
        'first_name': user.first_name,
        'last_name': user.last_name,
        'email': user.email,
        'role': profile.role,
        'store': profile.store,
        'is_active': True,
    }
    result = update_managed_user(user, data, actor, request_metadata)
    latest_log = AuditLog.objects.filter(
        actor=actor,
        object_type=result._meta.label_lower,
        object_id=str(result.pk),
    ).order_by('-pk').first()
    if latest_log:
        latest_log.action = AuditLog.Action.USER_ACTIVATED
        latest_log.save(update_fields=('action',))
    return result


@transaction.atomic
def deactivate_managed_user(user, actor, request_metadata=None):
    _require(can_manage_system_users(actor), 'Нельзя деактивировать пользователя.')
    if user.is_superuser or user.username.casefold() == 'bud':
        raise OperationNotAllowedError(
            'Главного администратора отключить нельзя.'
        )
    if user.pk == actor.pk:
        raise OperationNotAllowedError('Нельзя деактивировать самого себя.')
    profile = EmployeeProfile.objects.select_for_update().select_related('store').get(
        user=user
    )
    if profile.role == EmployeeProfile.Role.SYSTEM_ADMIN:
        active_admins = EmployeeProfile.objects.select_for_update().filter(
            role=EmployeeProfile.Role.SYSTEM_ADMIN,
            is_active=True,
            user__is_active=True,
        ).count()
        active_superusers = get_user_model().objects.filter(
            is_superuser=True,
            is_active=True,
        ).exclude(pk=user.pk).count()
        if active_admins <= 1 and active_superusers == 0:
            raise OperationNotAllowedError(
                'Нельзя деактивировать последнего администратора системы.'
            )
    locked_user = get_user_model().objects.select_for_update().get(pk=user.pk)
    locked_user.is_active = False
    locked_user.save(update_fields=('is_active',))
    profile.is_active = False
    profile.save(update_fields=('is_active', 'updated_at'))
    StoreTerminalAccount.objects.filter(user=locked_user).update(is_active=False)
    _audit(
        actor=actor,
        store=profile.store,
        obj=locked_user,
        action=AuditLog.Action.USER_DEACTIVATED,
        old_value={'is_active': True},
        new_value={'is_active': False},
        request_metadata=request_metadata,
    )
    return locked_user


@transaction.atomic
def delete_managed_user(user, actor, request_metadata=None):
    _require(can_manage_system_users(actor), 'Нельзя удалить пользователя.')
    locked_user = get_user_model().objects.select_for_update().get(pk=user.pk)
    if locked_user.is_superuser or locked_user.username.casefold() == 'bud':
        raise OperationNotAllowedError(
            'Главного администратора удалить нельзя.'
        )
    if locked_user.pk == actor.pk:
        raise OperationNotAllowedError('Нельзя удалить собственную учётную запись.')
    profile = EmployeeProfile.objects.select_for_update().filter(
        user=locked_user
    ).select_related('store').first()
    old_value = {
        'username': locked_user.username,
        'role': profile.role if profile else None,
        'store_id': profile.store_id if profile else None,
    }
    _audit(
        actor=actor,
        store=profile.store if profile else None,
        obj=locked_user,
        action=AuditLog.Action.USER_DELETED,
        old_value=old_value,
        request_metadata=request_metadata,
    )
    user_id = locked_user.pk
    locked_user.delete()
    return user_id


@transaction.atomic
def reset_managed_user_password(
    user,
    password,
    actor,
    request_metadata=None,
    current_session_key=None,
):
    _require(can_manage_system_users(actor), 'Нельзя сбросить пароль.')
    locked_user = get_user_model().objects.select_for_update().get(pk=user.pk)
    validate_password(password, user=locked_user)
    locked_user.set_password(password)
    locked_user.save(update_fields=('password',))
    _invalidate_user_sessions(
        [locked_user.pk],
        exclude_session_key=current_session_key,
    )
    profile = EmployeeProfile.objects.filter(user=locked_user).select_related('store').first()
    _audit(
        actor=actor,
        store=profile.store if profile else None,
        obj=locked_user,
        action=AuditLog.Action.USER_PASSWORD_RESET,
        new_value={'password_reset': True},
        request_metadata=request_metadata,
    )
    return locked_user


@transaction.atomic
def create_store_employee(store, data, actor, request_metadata=None):
    _require(can_manage_store_employees(actor, store), 'Нельзя создать сотрудника.')
    Store.objects.select_for_update().get(pk=store.pk)
    employee = StoreEmployee(store=store, **data)
    employee.save()
    _audit(
        actor=actor,
        store=store,
        obj=employee,
        action=AuditLog.Action.STORE_EMPLOYEE_CREATED,
        new_value=_model_values(
            employee,
            (
                'first_name',
                'last_name',
                'display_name',
                'position',
                'department',
                'personnel_number',
                'user_id',
                'sort_order',
                'is_active',
            ),
        ),
        request_metadata=request_metadata,
    )
    return employee


@transaction.atomic
def update_store_employee(store, employee, data, actor, request_metadata=None):
    _require(can_manage_store_employees(actor, store), 'Нельзя изменить сотрудника.')
    locked = StoreEmployee.objects.select_for_update().get(pk=employee.pk, store=store)
    fields = (
        'first_name',
        'last_name',
        'display_name',
        'position',
        'department',
        'personnel_number',
        'user_id',
        'sort_order',
    )
    old = _model_values(locked, (*fields, 'is_active'))
    for field in fields:
        source_field = 'user' if field == 'user_id' else field
        if source_field in data:
            setattr(locked, source_field, data[source_field])
    locked.save()
    _audit(
        actor=actor,
        store=store,
        obj=locked,
        action=AuditLog.Action.STORE_EMPLOYEE_UPDATED,
        old_value=old,
        new_value=_model_values(locked, (*fields, 'is_active')),
        request_metadata=request_metadata,
    )
    return locked


def _set_employee_active(store, employee, active, actor, request_metadata=None):
    with transaction.atomic():
        _require(can_manage_store_employees(actor, store), 'Нельзя изменить сотрудника.')
        locked = StoreEmployee.objects.select_for_update().get(pk=employee.pk, store=store)
        old = locked.is_active
        locked.is_active = active
        locked.save(update_fields=('is_active', 'updated_at'))
        _audit(
            actor=actor,
            store=store,
            obj=locked,
            action=(
                AuditLog.Action.STORE_EMPLOYEE_ACTIVATED
                if active
                else AuditLog.Action.STORE_EMPLOYEE_DEACTIVATED
            ),
            old_value={'is_active': old},
            new_value={'is_active': active},
            request_metadata=request_metadata,
        )
        return locked


def activate_store_employee(store, employee, actor, request_metadata=None):
    return _set_employee_active(store, employee, True, actor, request_metadata)


def deactivate_store_employee(store, employee, actor, request_metadata=None):
    return _set_employee_active(store, employee, False, actor, request_metadata)


def ensure_shift_month_editable(work_date, *, today=None):
    today = today or timezone.localdate()
    current_month = today.replace(day=1)
    target_month = work_date.replace(day=1)
    if target_month < current_month:
        raise OperationNotAllowedError(
            'График за прошлый месяц нельзя изменять.'
        )
    if work_date < today:
        raise OperationNotAllowedError(
            'Прошедшие дни текущего месяца нельзя изменять.'
        )


@transaction.atomic
def create_shift_assignment(store, work_date, data, actor, request_metadata=None):
    _require(can_manage_store_shifts(actor, store), 'Нельзя создать смену.')
    ensure_shift_month_editable(work_date)
    employee = StoreEmployee.objects.select_for_update().get(
        pk=data['employee'].pk,
        store=store,
        is_active=True,
    )
    assignment = DailyShiftAssignment(
        store=store,
        employee=employee,
        work_date=work_date,
        shift_type=data.get(
            'shift_type',
            DailyShiftAssignment.ShiftType.WORK,
        ),
        is_responsible_for_checklist=data.get('is_responsible_for_checklist', False),
        shift_start=data.get('shift_start'),
        shift_end=data.get('shift_end'),
        comment=data.get('comment') or '',
        created_by=actor,
    )
    assignment.save()
    _audit(
        actor=actor,
        store=store,
        obj=assignment,
        action=AuditLog.Action.SHIFT_ASSIGNMENT_CREATED,
        new_value={
            'employee_id': employee.pk,
            'work_date': work_date.isoformat(),
            'shift_type': assignment.shift_type,
        },
        request_metadata=request_metadata,
    )
    return assignment


@transaction.atomic
def update_shift_assignment(store, assignment, data, actor, request_metadata=None):
    _require(can_manage_store_shifts(actor, store), 'Нельзя изменить смену.')
    locked = DailyShiftAssignment.objects.select_for_update().get(
        pk=assignment.pk,
        store=store,
    )
    ensure_shift_month_editable(locked.work_date)
    employee = StoreEmployee.objects.select_for_update().get(
        pk=data['employee'].pk,
        store=store,
        is_active=True,
    )
    fields = (
        'employee_id',
        'shift_type',
        'is_responsible_for_checklist',
        'shift_start',
        'shift_end',
        'comment',
    )
    old = _model_values(locked, fields)
    locked.employee = employee
    for field in (
        'shift_type',
        'is_responsible_for_checklist',
        'shift_start',
        'shift_end',
        'comment',
    ):
        value = (
            data.get(field, locked.shift_type)
            if field == 'shift_type'
            else data.get(field)
        )
        setattr(locked, field, value)
    locked.save()
    _audit(
        actor=actor,
        store=store,
        obj=locked,
        action=AuditLog.Action.SHIFT_ASSIGNMENT_UPDATED,
        old_value=old,
        new_value=_model_values(locked, fields),
        request_metadata=request_metadata,
    )
    return locked


@transaction.atomic
def delete_shift_assignment(store, assignment, actor, request_metadata=None):
    _require(can_manage_store_shifts(actor, store), 'Нельзя удалить смену.')
    locked = DailyShiftAssignment.objects.select_for_update().get(
        pk=assignment.pk,
        store=store,
    )
    ensure_shift_month_editable(locked.work_date)
    old = {'employee_id': locked.employee_id, 'work_date': locked.work_date.isoformat()}
    object_id = locked.pk
    locked.delete()
    AuditLog.objects.create(
        actor=actor,
        store=store,
        object_type=DailyShiftAssignment._meta.label_lower,
        object_id=str(object_id),
        action=AuditLog.Action.SHIFT_ASSIGNMENT_DELETED,
        old_value=old,
    )


@transaction.atomic
def bulk_create_shift_assignments(store, data, actor, request_metadata=None):
    _require(can_manage_store_shifts(actor, store), 'Нельзя планировать смены.')
    ensure_shift_month_editable(data['start_date'])
    employees = list(
        StoreEmployee.objects.select_for_update().filter(
            store=store,
            is_active=True,
            pk__in=[employee.pk for employee in data['employees']],
        )
    )
    if len(employees) != len(data['employees']):
        raise OperationNotAllowedError('Список сотрудников недопустим.')
    weekdays = {int(value) for value in data['weekdays']}
    result = {'created': 0, 'updated': 0, 'skipped': 0, 'errors': []}
    current_date = data['start_date']
    while current_date <= data['end_date']:
        if current_date.weekday() in weekdays:
            for employee in employees:
                defaults = {
                    'shift_type': data.get(
                        'shift_type',
                        DailyShiftAssignment.ShiftType.WORK,
                    ),
                    'is_responsible_for_checklist': data.get('is_responsible_for_checklist', False),
                    'shift_start': data.get('shift_start'),
                    'shift_end': data.get('shift_end'),
                    'comment': data.get('comment') or '',
                    'created_by': actor,
                }
                assignment = DailyShiftAssignment.objects.filter(
                    store=store,
                    employee=employee,
                    work_date=current_date,
                ).first()
                if assignment and data['mode'] == 'create':
                    result['skipped'] += 1
                    continue
                try:
                    if assignment:
                        for field, value in defaults.items():
                            setattr(assignment, field, value)
                        assignment.save()
                        result['updated'] += 1
                    else:
                        DailyShiftAssignment.objects.create(
                            store=store,
                            employee=employee,
                            work_date=current_date,
                            **defaults,
                        )
                        result['created'] += 1
                except (ValidationError, IntegrityError) as exc:
                    result['errors'].append(f'{current_date}: {employee.display_name}: {exc}')
        current_date += timedelta(days=1)
    _audit(
        actor=actor,
        store=store,
        obj=store,
        action=AuditLog.Action.SHIFT_ASSIGNMENTS_BULK_CREATED,
        new_value={key: value for key, value in result.items() if key != 'errors'},
        request_metadata=request_metadata,
    )
    return result


def _published_version_for_store(store, *, lock=False):
    query = ChecklistTemplateVersion.objects.select_related('template').filter(
        template__store=store,
        template__is_active=True,
        status=ChecklistTemplateVersion.Status.PUBLISHED,
    )
    if lock:
        query = query.select_for_update()
    versions = list(query)
    if len(versions) != 1:
        raise TemplateConfigurationError(
            'У магазина должна быть ровно одна опубликованная версия.'
        )
    return versions[0]


def get_current_questions(store):
    version = _published_version_for_store(store)
    return ChecklistItem.objects.filter(
        section__version=version,
    ).select_related('section').order_by('section__sort_order', 'sort_order', 'id')


def _clone_published_version(store, actor):
    source = _published_version_for_store(store, lock=True)
    template = ChecklistTemplate.objects.select_for_update().get(pk=source.template_id)
    next_number = (
        ChecklistTemplateVersion.objects.filter(template=template).aggregate(
            value=Max('version_number')
        )['value']
        or 0
    ) + 1
    draft = ChecklistTemplateVersion.objects.create(
        template=template,
        version_number=next_number,
        created_by=actor,
    )
    item_map = {}
    for section in source.sections.prefetch_related('items').order_by('sort_order', 'id'):
        cloned_section = ChecklistSection.objects.create(
            version=draft,
            name=section.name,
            code=section.code,
            sort_order=section.sort_order,
        )
        for item in section.items.all().order_by('sort_order', 'id'):
            cloned = ChecklistItem.objects.create(
                section=cloned_section,
                text=item.text,
                description=item.description,
                sort_order=item.sort_order,
                is_active=item.is_active,
                is_required=item.is_required,
                answer_type=item.answer_type,
                comment_required_on_failure=item.comment_required_on_failure,
                allow_not_applicable=item.allow_not_applicable,
                effective_from=item.effective_from,
                effective_until=item.effective_until,
            )
            item_map[item.pk] = cloned
    return source, draft, item_map


def _question_values(item):
    return {
        'text': item.text,
        'description': item.description,
        'section_code': item.section.code,
        'sort_order': item.sort_order,
        'is_active': item.is_active,
        'is_required': item.is_required,
        'answer_type': item.answer_type,
        'allow_not_applicable': item.allow_not_applicable,
        'comment_required_on_failure': item.comment_required_on_failure,
        'effective_from': item.effective_from.isoformat() if item.effective_from else None,
        'effective_until': item.effective_until.isoformat() if item.effective_until else None,
    }


def _apply_question_data(item, data, draft):
    section = draft.sections.get(code=data['section_code'])
    item.section = section
    data = {**data}
    data.setdefault('answer_type', item.answer_type)
    for field in (
        'text',
        'description',
        'sort_order',
        'is_active',
        'is_required',
        'answer_type',
        'allow_not_applicable',
        'comment_required_on_failure',
        'effective_from',
        'effective_until',
    ):
        setattr(item, field, data[field])
    item.save()


@transaction.atomic
def create_checklist_question(store, data, actor, request_metadata=None):
    _require(can_manage_store_questions(actor, store), 'Нельзя создать вопрос.')
    _, draft, _ = _clone_published_version(store, actor)
    item = ChecklistItem(section=draft.sections.get(code=data['section_code']))
    _apply_question_data(item, data, draft)
    publish_template_version(draft, actor)
    _audit(
        actor=actor,
        store=store,
        obj=item,
        action=AuditLog.Action.CHECKLIST_QUESTION_CREATED,
        new_value=_question_values(item),
        request_metadata=request_metadata,
    )
    return item


@transaction.atomic
def update_checklist_question(store, question, data, actor, request_metadata=None):
    _require(can_manage_store_questions(actor, store), 'Нельзя изменить вопрос.')
    source_question = ChecklistItem.objects.select_related(
        'section__version__template'
    ).get(pk=question.pk, section__version__template__store=store)
    old = _question_values(source_question)
    _, draft, item_map = _clone_published_version(store, actor)
    cloned = item_map.get(source_question.pk)
    if cloned is None:
        raise OperationNotAllowedError('Вопрос не входит в текущую версию.')
    _apply_question_data(cloned, data, draft)
    publish_template_version(draft, actor)
    _audit(
        actor=actor,
        store=store,
        obj=cloned,
        action=AuditLog.Action.CHECKLIST_QUESTION_UPDATED,
        old_value=old,
        new_value=_question_values(cloned),
        request_metadata=request_metadata,
    )
    return cloned


def _set_question_active(store, question, active, actor, request_metadata=None):
    source = ChecklistItem.objects.select_related('section').get(
        pk=question.pk,
        section__version__template__store=store,
    )
    data = _question_values(source)
    data['effective_from'] = source.effective_from
    data['effective_until'] = source.effective_until
    data['is_active'] = active
    result = update_checklist_question(store, source, data, actor, request_metadata)
    latest_log = AuditLog.objects.filter(
        object_type=result._meta.label_lower,
        object_id=str(result.pk),
    ).order_by('-pk').first()
    if latest_log:
        latest_log.action = (
            AuditLog.Action.CHECKLIST_QUESTION_ACTIVATED
            if active
            else AuditLog.Action.CHECKLIST_QUESTION_DEACTIVATED
        )
        latest_log.save(update_fields=('action',))
    return result


def activate_checklist_question(store, question, actor, request_metadata=None):
    return _set_question_active(store, question, True, actor, request_metadata)


def deactivate_checklist_question(store, question, actor, request_metadata=None):
    return _set_question_active(store, question, False, actor, request_metadata)


def get_checklist_question_history(question):
    """Return conservative history evidence for a versioned question."""
    question = ChecklistItem.objects.select_related(
        'section__version__template'
    ).get(pk=question.pk)
    version = question.section.version
    snapshot_exists = DailyChecklistItem.objects.filter(
        source_item=question,
    ).exists()
    answer_exists = ChecklistAnswer.objects.filter(
        daily_item__source_item=question,
    ).exists()
    published_version = version.status in {
        ChecklistTemplateVersion.Status.PUBLISHED,
        ChecklistTemplateVersion.Status.ARCHIVED,
    }
    return {
        'is_used': published_version or snapshot_exists or answer_exists,
        'published_version': published_version,
        'snapshot_exists': snapshot_exists,
        'answer_exists': answer_exists,
    }


@transaction.atomic
def delete_checklist_question(
    *,
    actor,
    store,
    question,
    request_metadata=None,
):
    _require(can_manage_store_questions(actor, store), 'Нельзя удалить вопрос.')
    Store.objects.select_for_update().get(pk=store.pk)
    locked = ChecklistItem.objects.select_for_update().select_related(
        'section__version__template'
    ).get(
        pk=question.pk,
        section__version__template__store=store,
    )
    version = ChecklistTemplateVersion.objects.select_for_update().get(
        pk=locked.section.version_id,
    )
    ChecklistTemplate.objects.select_for_update().get(pk=version.template_id)
    history = get_checklist_question_history(locked)
    values = _question_values(locked)
    question_id = locked.pk
    existing_removal = AuditLog.objects.filter(
        store=store,
        object_type=ChecklistItem._meta.label_lower,
        object_id=str(question_id),
        action__in=(
            AuditLog.Action.CHECKLIST_QUESTION_DELETED,
            AuditLog.Action.CHECKLIST_QUESTION_REMOVED_FROM_TEMPLATE,
        ),
    ).exists()
    if existing_removal:
        return {
            'method': 'already_processed',
            'historically_used': history['is_used'],
            'question_id': question_id,
        }

    if history['is_used']:
        current = _published_version_for_store(store, lock=True)
        if current.pk != version.pk:
            raise OperationNotAllowedError(
                'Вопрос не входит в текущую опубликованную версию.'
            )
        _, draft, item_map = _clone_published_version(store, actor)
        cloned = item_map.get(question_id)
        if cloned is None:
            raise OperationNotAllowedError('Вопрос уже исключён из шаблона.')
        cloned_section_id = cloned.section_id
        cloned.delete()
        remaining = ChecklistItem.objects.select_for_update().filter(
            section_id=cloned_section_id,
        ).order_by('sort_order', 'id')
        for sort_order, item in enumerate(remaining, start=1):
            if item.sort_order != sort_order:
                item.sort_order = sort_order
                item.save(update_fields=('sort_order', 'updated_at'))
        publish_template_version(draft, actor)
        _audit(
            actor=actor,
            store=store,
            obj=locked,
            action=AuditLog.Action.CHECKLIST_QUESTION_REMOVED_FROM_TEMPLATE,
            old_value=values,
            new_value={
                'question_id': question_id,
                'text': locked.text,
                'section_code': locked.section.code,
                'sort_order': locked.sort_order,
                'method': 'removed_from_new_version',
                'published_version_id': draft.pk,
            },
            request_metadata=request_metadata,
        )
        return {
            'method': 'removed_from_new_version',
            'historically_used': True,
            'question_id': question_id,
            'published_version': draft,
        }

    if version.status != ChecklistTemplateVersion.Status.DRAFT:
        raise OperationNotAllowedError(
            'Физически удалить можно только неопубликованный вопрос.'
        )
    section_id = locked.section_id
    locked.delete()
    remaining = ChecklistItem.objects.select_for_update().filter(
        section_id=section_id,
    ).order_by('sort_order', 'id')
    for sort_order, item in enumerate(remaining, start=1):
        if item.sort_order != sort_order:
            item.sort_order = sort_order
            item.save(update_fields=('sort_order', 'updated_at'))
    ip_address, user_agent = _metadata_values(request_metadata)
    AuditLog.objects.create(
        actor=actor,
        employee=None,
        store=store,
        object_type=ChecklistItem._meta.label_lower,
        object_id=str(question_id),
        action=AuditLog.Action.CHECKLIST_QUESTION_DELETED,
        old_value=values,
        new_value={
            'question_id': question_id,
            'text': values['text'],
            'section_code': values['section_code'],
            'sort_order': values['sort_order'],
            'method': 'hard_delete',
        },
        ip_address=ip_address,
        user_agent=user_agent,
    )
    return {
        'method': 'hard_delete',
        'historically_used': False,
        'question_id': question_id,
    }


@transaction.atomic
def reorder_checklist_questions(
    store,
    section_code,
    ordered_ids,
    actor,
    request_metadata=None,
):
    _require(can_manage_store_questions(actor, store), 'Нельзя изменить порядок.')
    if len(ordered_ids) != len(set(ordered_ids)):
        raise ValidationError('В порядке есть дубли ID.')
    source = _published_version_for_store(store, lock=True)
    source_items = list(
        ChecklistItem.objects.filter(
            section__version=source,
            section__code=section_code,
        ).order_by('sort_order', 'id')
    )
    if set(ordered_ids) != {item.pk for item in source_items}:
        raise ValidationError('Список ID не совпадает с вопросами этапа.')
    _, draft, item_map = _clone_published_version(store, actor)
    for position, source_id in enumerate(ordered_ids, start=1):
        ChecklistItem.objects.filter(pk=item_map[source_id].pk).update(
            sort_order=position
        )
    publish_template_version(draft, actor)
    _audit(
        actor=actor,
        store=store,
        obj=draft,
        action=AuditLog.Action.CHECKLIST_QUESTIONS_REORDERED,
        old_value={'ordered_ids': [item.pk for item in source_items]},
        new_value={'ordered_ids': ordered_ids, 'section_code': section_code},
        request_metadata=request_metadata,
    )
    return draft


@transaction.atomic
def update_store_schedule(store, data, actor, request_metadata=None):
    _require(can_manage_store_schedule(actor, store), 'Нельзя изменить расписание.')
    Store.objects.select_for_update().get(pk=store.pk)
    schedule, _ = StoreChecklistSchedule.objects.select_for_update().get_or_create(
        store=store
    )
    fields = (
        'opening_time',
        'morning_deadline',
        'daytime_deadline',
        'closing_deadline',
        'morning_completion_window_minutes',
        'day_completion_window_minutes',
        'evening_completion_window_minutes',
        'warning_minutes_before',
        'notifications_enabled',
        'working_weekdays',
        'is_active',
    )
    old = _model_values(schedule, fields)
    for field in fields:
        if field in data:
            setattr(schedule, field, data[field])
    schedule.save()
    new = _model_values(schedule, fields)
    completion_window_fields = (
        ('opening', 'morning_completion_window_minutes'),
        ('during_day', 'day_completion_window_minutes'),
        ('closing', 'evening_completion_window_minutes'),
    )
    completion_window_changes = [
        {
            'stage': stage,
            'field': field,
            'old_minutes': old[field],
            'new_minutes': new[field],
        }
        for stage, field in completion_window_fields
        if old[field] != new[field]
    ]
    if completion_window_changes:
        new['completion_window_changes'] = completion_window_changes
    _audit(
        actor=actor,
        store=store,
        obj=schedule,
        action=AuditLog.Action.STORE_SCHEDULE_UPDATED,
        old_value=old,
        new_value=new,
        request_metadata=request_metadata,
    )
    return schedule


@transaction.atomic
def update_store_notification_settings(store, data, actor, request_metadata=None):
    _require(can_manage_store_notifications(actor, store), 'Нельзя изменить уведомления.')
    Store.objects.select_for_update().get(pk=store.pk)
    notification_settings, _ = (
        StoreNotificationSettings.objects.select_for_update().get_or_create(
            store=store,
            defaults={'is_active': False},
        )
    )
    fields = (
        'telegram_chat_id',
        'warning_enabled',
        'overdue_enabled',
        'completed_late_enabled',
        'is_active',
    )
    old = _model_values(notification_settings, fields)
    for field in fields:
        setattr(notification_settings, field, data[field])
    notification_settings.save()
    _audit(
        actor=actor,
        store=store,
        obj=notification_settings,
        action=AuditLog.Action.STORE_NOTIFICATION_SETTINGS_UPDATED,
        old_value=old,
        new_value={
            **_model_values(notification_settings, fields),
            'telegram_chat_id': 'configured' if notification_settings.telegram_chat_id else '',
        },
        request_metadata=request_metadata,
    )
    return notification_settings


def send_store_test_notification(store, actor, request_metadata=None):
    _require(can_manage_store_notifications(actor, store), 'Нельзя отправить тест.')
    notification_settings = StoreNotificationSettings.objects.get(
        store=store,
        is_active=True,
    )
    if not notification_settings.telegram_chat_id:
        raise ValidationError('Не задан Telegram chat ID.')
    message_id = send_telegram_message(
        notification_settings.telegram_chat_id,
        f'✅ Тестовое сообщение: {store.name}',
    )
    with transaction.atomic():
        _audit(
            actor=actor,
            store=store,
            obj=notification_settings,
            action=AuditLog.Action.TELEGRAM_TEST_MESSAGE_SENT,
            new_value={'sent': True, 'message_id': message_id},
            request_metadata=request_metadata,
        )
    return message_id


@transaction.atomic
def reopen_stage_with_reason(
    daily,
    section_code,
    reason,
    actor,
    request_metadata=None,
):
    _require(can_reopen_store_stage(actor, daily.store), 'Нельзя открыть этап.')
    reason = (reason or '').strip()
    if len(reason) < 5:
        raise ValidationError('Причина должна содержать минимум 5 символов.')
    locked = DailyChecklist.objects.select_for_update().get(
        pk=daily.pk,
        store=daily.store,
    )
    result = reopen_daily_checklist(
        locked,
        actor,
        request_metadata=request_metadata,
        section_code=section_code,
    )
    stage = result.stages.get(section_code=section_code)
    _audit(
        actor=actor,
        store=result.store,
        obj=stage,
        action=AuditLog.Action.CHECKLIST_STAGE_REOPENED,
        old_value={'section_code': section_code},
        new_value={'section_code': section_code, 'reason': reason},
        request_metadata=request_metadata,
    )
    return result
