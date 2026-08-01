from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from checklists.access_control import is_system_admin
from checklists.exceptions import OperationNotAllowedError
from checklists.models import (
    AuditLog,
    Store,
    TelegramPendingBinding,
    TelegramStoreBinding,
    TelegramSystemSettings,
    TelegramUserProfile,
    UserStoreMembership,
)
from checklists.telegram_queue import enqueue_telegram_message


@transaction.atomic
def update_telegram_system_settings(
    *,
    actor,
    data,
    new_token='',
    clear_token=False,
    new_webhook_secret='',
    clear_webhook_secret=False,
):
    if not is_system_admin(actor):
        raise OperationNotAllowedError('Только системный администратор меняет шлюз.')
    config = TelegramSystemSettings.objects.select_for_update().filter(pk=1).first()
    if config is None:
        config = TelegramSystemSettings(pk=1)
    safe_fields = (
        'alternative_api_base_url',
        'use_alternative_gateway',
        'fallback_to_official_api',
        'alternative_attempts',
        'official_attempts',
        'request_timeout_seconds',
        'retry_delay_seconds',
        'is_enabled',
        'incoming_mode',
        'webhook_max_connections',
        'webhook_allowed_updates',
        'immediate_ack_enabled',
        'immediate_ack_text',
    )
    old_value = {
        field: getattr(config, field)
        for field in safe_fields
    }
    for field in safe_fields:
        if field in data:
            setattr(config, field, data[field])
    token_changed = False
    if clear_token:
        config.bot_token = ''
        token_changed = True
    elif (new_token or '').strip():
        config.bot_token = new_token.strip()
        token_changed = True
    webhook_secret_changed = False
    if clear_webhook_secret:
        config.webhook_secret_token = ''
        webhook_secret_changed = True
    elif (new_webhook_secret or '').strip():
        config.webhook_secret_token = new_webhook_secret.strip()
        webhook_secret_changed = True
    config.updated_by = actor
    config.save()
    AuditLog.objects.create(
        actor=actor,
        store=None,
        object_type=config._meta.label_lower,
        object_id='1',
        action=AuditLog.Action.TELEGRAM_SYSTEM_SETTINGS_UPDATED,
        old_value=old_value,
        new_value={
            **{field: getattr(config, field) for field in safe_fields},
            'token_changed': token_changed,
            'token_configured': bool(config.bot_token),
            'webhook_secret_changed': webhook_secret_changed,
            'webhook_secret_configured': bool(config.webhook_secret_token),
        },
    )
    if old_value.get('incoming_mode') != config.incoming_mode:
        AuditLog.objects.create(
            actor=actor,
            store=None,
            object_type=config._meta.label_lower,
            object_id='1',
            action=AuditLog.Action.TELEGRAM_INCOMING_MODE_CHANGED,
            old_value={'incoming_mode': old_value.get('incoming_mode')},
            new_value={'incoming_mode': config.incoming_mode},
        )
    return config


@transaction.atomic
def link_telegram_user(*, binding, user):
    locked = TelegramStoreBinding.objects.select_for_update().select_related(
        'store'
    ).get(pk=binding.pk)
    if not user.is_active:
        raise ValidationError('Нельзя привязать неактивного пользователя.')
    has_membership = UserStoreMembership.objects.filter(
        user=user,
        store=locked.store,
        is_active=True,
    ).exists()
    profile = getattr(user, 'employee_profile', None)
    legacy_store_match = (
        profile is not None
        and profile.store_id == locked.store_id
        and profile.is_active
    )
    if not is_system_admin(user) and not has_membership and not legacy_store_match:
        raise ValidationError(
            'Пользователь не относится к выбранному магазину.'
        )
    conflict = TelegramUserProfile.objects.filter(
        telegram_user_id=locked.telegram_user_id
    ).exclude(user=user).exists()
    if conflict:
        raise ValidationError(
            'Этот Telegram ID уже связан с другим пользователем.'
        )
    TelegramUserProfile.objects.update_or_create(
        user=user,
        defaults={
            'telegram_user_id': locked.telegram_user_id,
            'telegram_chat_id': locked.telegram_chat_id,
            'telegram_username': locked.username,
            'first_name': locked.first_name,
            'last_name': locked.last_name,
            'is_verified': True,
        },
    )
    locked.user = user
    locked.save(update_fields=('user',))
    return locked


@transaction.atomic
def reassign_telegram_profile(*, profile, user, actor):
    if not is_system_admin(actor):
        raise OperationNotAllowedError(
            'Изменение Telegram-пользователя доступно администратору.'
        )
    locked = TelegramUserProfile.objects.select_for_update().select_related(
        'user'
    ).get(pk=profile.pk)
    if not user.is_active:
        raise ValidationError('Нельзя выбрать неактивного пользователя.')
    if TelegramUserProfile.objects.filter(user=user).exclude(pk=locked.pk).exists():
        raise ValidationError('У пользователя уже подключён Telegram.')
    old_user_id = locked.user_id
    locked.user = user
    locked.save(update_fields=('user', 'updated_at'))
    TelegramStoreBinding.objects.filter(
        telegram_user_id=locked.telegram_user_id
    ).update(user=user)
    AuditLog.objects.create(
        actor=actor,
        store=None,
        object_type=locked._meta.label_lower,
        object_id=str(locked.pk),
        action=AuditLog.Action.TELEGRAM_PROFILE_REASSIGNED,
        old_value={'user_id': old_user_id},
        new_value={'user_id': user.pk},
    )
    return locked


@transaction.atomic
def disconnect_telegram_profile(*, profile, actor):
    if not is_system_admin(actor):
        raise OperationNotAllowedError(
            'Отключение Telegram доступно администратору.'
        )
    locked = TelegramUserProfile.objects.select_for_update().get(pk=profile.pk)
    values = {
        'user_id': locked.user_id,
        'telegram_user_id': locked.telegram_user_id,
    }
    TelegramStoreBinding.objects.filter(
        telegram_user_id=locked.telegram_user_id
    ).update(user=None)
    AuditLog.objects.create(
        actor=actor,
        store=None,
        object_type=locked._meta.label_lower,
        object_id=str(locked.pk),
        action=AuditLog.Action.TELEGRAM_PROFILE_DISCONNECTED,
        old_value=values,
    )
    locked.delete()


@transaction.atomic
def approve_pending_binding(*, pending, store, actor, user=None):
    if not is_system_admin(actor):
        raise OperationNotAllowedError('Подтверждение доступно только администратору.')
    locked_store = Store.objects.select_for_update().get(pk=store.pk, is_active=True)
    locked = TelegramPendingBinding.objects.select_for_update().get(pk=pending.pk)
    if locked.status != TelegramPendingBinding.Status.PENDING:
        raise ValidationError('Заявка уже обработана.')
    if locked.expires_at <= timezone.now():
        locked.status = TelegramPendingBinding.Status.EXPIRED
        locked.save(update_fields=('status',))
        raise ValidationError('Срок действия заявки истёк.')
    binding, _ = TelegramStoreBinding.objects.update_or_create(
        telegram_user_id=locked.telegram_user_id,
        defaults={
            'store': locked_store,
            'telegram_chat_id': locked.telegram_chat_id,
            'username': locked.username,
            'first_name': locked.first_name,
            'last_name': locked.last_name,
            'is_active': True,
            'approved_by': actor,
            'approved_at': timezone.now(),
        },
    )
    locked.status = TelegramPendingBinding.Status.APPROVED
    locked.save(update_fields=('status',))
    if user is not None:
        binding = link_telegram_user(binding=binding, user=user)
    AuditLog.objects.create(
        actor=actor,
        store=locked_store,
        object_type=binding._meta.label_lower,
        object_id=str(binding.pk),
        action=AuditLog.Action.TELEGRAM_BINDING_APPROVED,
        new_value={'telegram_user_id': binding.telegram_user_id},
    )
    enqueue_telegram_message(
        store=locked_store,
        chat_id=binding.telegram_chat_id,
        message_type='telegram_binding_approved',
        idempotency_key=f'binding:{binding.pk}:approved:{locked.pk}',
        payload={
            'text': (
                '✅ Доступ подтверждён. '
                f'Вы привязаны к магазину «{locked_store.name}».'
            )
        },
    )
    return binding


@transaction.atomic
def disable_telegram_binding(*, binding, actor):
    if not is_system_admin(actor):
        raise OperationNotAllowedError('Отключение доступно только администратору.')
    locked = TelegramStoreBinding.objects.select_for_update().select_related(
        'store'
    ).get(pk=binding.pk)
    if locked.is_active:
        locked.is_active = False
        locked.save(update_fields=('is_active',))
        AuditLog.objects.create(
            actor=actor,
            store=locked.store,
            object_type=locked._meta.label_lower,
            object_id=str(locked.pk),
            action=AuditLog.Action.TELEGRAM_BINDING_DISABLED,
            new_value={'telegram_user_id': locked.telegram_user_id},
        )
    return locked


@transaction.atomic
def transfer_telegram_binding(*, binding, store, actor):
    if not is_system_admin(actor):
        raise OperationNotAllowedError('Перенос доступен только администратору.')
    locked = TelegramStoreBinding.objects.select_for_update().get(pk=binding.pk)
    locked_store = Store.objects.select_for_update().get(pk=store.pk, is_active=True)
    locked.store = locked_store
    locked.is_active = True
    locked.approved_by = actor
    locked.approved_at = timezone.now()
    locked.save(
        update_fields=('store', 'is_active', 'approved_by', 'approved_at')
    )
    AuditLog.objects.create(
        actor=actor,
        store=locked_store,
        object_type=locked._meta.label_lower,
        object_id=str(locked.pk),
        action=AuditLog.Action.TELEGRAM_BINDING_APPROVED,
        new_value={
            'telegram_user_id': locked.telegram_user_id,
            'transferred': True,
        },
    )
    return locked
