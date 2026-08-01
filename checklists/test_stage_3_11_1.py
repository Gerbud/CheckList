import json
from datetime import timedelta

import pytest
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import transaction
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from checklists.exceptions import OperationNotAllowedError
from checklists.management_services import (
    deactivate_managed_user,
    set_user_store_membership,
)
from checklists.models import (
    AuditLog,
    EmployeeProfile,
    Store,
    StoreAdHocTask,
    StoreChecklistSchedule,
    TelegramOutboundMessage,
    TelegramStoreBinding,
    TelegramUserProfile,
    UserStoreMembership,
)
from checklists.telegram_bot import process_telegram_update


pytestmark = pytest.mark.django_db


def make_store(code):
    store = Store.objects.create(name=f'Магазин {code}', code=code)
    StoreChecklistSchedule.objects.create(store=store)
    return store


def make_user(username, *, role=None, store=None):
    user = User.objects.create_user(
        username=username,
        password='Strong-Test-934!',
        first_name=username.capitalize(),
    )
    if role:
        EmployeeProfile.objects.create(
            user=user,
            role=role,
            store=store,
        )
    return user


def make_membership(user, store, role=UserStoreMembership.Role.EMPLOYEE):
    return UserStoreMembership.objects.create(
        user=user,
        store=store,
        role_in_store=role,
    )


def make_telegram(user, primary_store, telegram_id):
    profile = TelegramUserProfile.objects.create(
        user=user,
        telegram_user_id=telegram_id,
        telegram_chat_id=telegram_id,
        telegram_username=user.username,
        first_name=user.first_name,
        is_verified=True,
    )
    binding = TelegramStoreBinding.objects.create(
        user=user,
        store=primary_store,
        telegram_user_id=telegram_id,
        telegram_chat_id=telegram_id,
        username=user.username,
    )
    return profile, binding


def message_update(update_id, telegram_id, text):
    return {
        'update_id': update_id,
        'message': {
            'text': text,
            'from': {
                'id': telegram_id,
                'username': f'user{telegram_id}',
                'first_name': 'Иван',
                'is_bot': False,
            },
            'chat': {'id': telegram_id, 'type': 'private'},
        },
    }


def callback_update(update_id, telegram_id, data):
    return {
        'update_id': update_id,
        'callback_query': {
            'id': f'callback-{update_id}',
            'data': data,
            'from': {
                'id': telegram_id,
                'username': f'user{telegram_id}',
                'first_name': 'Иван',
                'is_bot': False,
            },
            'message': {'chat': {'id': telegram_id, 'type': 'private'}},
        },
    }


def make_task(store, author, text):
    return StoreAdHocTask.objects.create(
        store=store,
        date=timezone.localdate() + timedelta(days=1),
        section_code=StoreAdHocTask.SectionCode.MORNING,
        text=text,
        created_by=author,
    )


def test_one_store_user_creates_telegram_task_immediately():
    store = make_store('one-store')
    user = make_user('ivan-one')
    membership = make_membership(user, store)
    _, binding = make_telegram(user, store, 94001)

    result = process_telegram_update(
        message_update(94010, 94001, '/task Проверить склад')
    )

    assert result == 'processed'
    task = StoreAdHocTask.objects.get(
        created_by_telegram_binding=binding
    )
    assert task.store == membership.store
    assert task.created_by == user
    assert task.source == StoreAdHocTask.Source.TELEGRAM
    response = TelegramOutboundMessage.objects.get(
        idempotency_key=f'update:94010:quick-created-{membership.pk}'
    )
    assert 'Задача создана' in response.payload['text']
    assert store.name in response.payload['text']


def test_two_store_user_selects_store_before_telegram_task_creation():
    first = make_store('multi-first')
    second = make_store('multi-second')
    user = make_user('ivan-multi')
    first_membership = make_membership(
        user,
        first,
        UserStoreMembership.Role.DIRECTOR,
    )
    second_membership = make_membership(
        user,
        second,
        UserStoreMembership.Role.EMPLOYEE,
    )
    _, binding = make_telegram(user, first, 94002)

    process_telegram_update(
        message_update(94020, 94002, '/task Проверить склад')
    )

    assert not StoreAdHocTask.objects.filter(
        created_by_telegram_binding=binding
    ).exists()
    selection = TelegramOutboundMessage.objects.get(
        idempotency_key='update:94020:quick-store'
    )
    assert selection.payload['text'] == 'Выберите магазин:'
    callbacks = json.dumps(
        selection.payload['reply_markup'],
        ensure_ascii=False,
    )
    assert first.name in callbacks
    assert second.name in callbacks

    process_telegram_update(
        callback_update(
            94021,
            94002,
            f'quicktask:store:{second_membership.pk}',
        )
    )

    task = StoreAdHocTask.objects.get(
        created_by_telegram_binding=binding
    )
    assert task.store == second
    assert task.created_by == user
    assert first_membership.store == first


def test_director_deletes_any_task_in_own_store_but_not_foreign_store():
    own_store = make_store('delete-own')
    foreign_store = make_store('delete-foreign')
    director = make_user(
        'membership-director',
        role=EmployeeProfile.Role.STORE_DIRECTOR,
        store=own_store,
    )
    make_membership(
        director,
        own_store,
        UserStoreMembership.Role.DIRECTOR,
    )
    other_author = make_user('other-task-author')
    own_task = make_task(own_store, other_author, 'Задача своего магазина')
    own_task_id = own_task.pk
    foreign_task = make_task(
        foreign_store,
        other_author,
        'Задача чужого магазина',
    )
    client = Client()
    client.force_login(director)

    deleted = client.post(
        reverse('checklists:director_task_delete', args=[own_task.pk])
    )
    denied = client.post(
        reverse('checklists:director_task_delete', args=[foreign_task.pk])
    )

    assert deleted.status_code == 302
    assert not StoreAdHocTask.objects.filter(pk=own_task_id).exists()
    assert AuditLog.objects.filter(
        action=AuditLog.Action.STORE_TASK_DELETED,
        object_id=str(own_task_id),
        actor=director,
    ).exists()
    assert denied.status_code == 404
    assert StoreAdHocTask.objects.filter(pk=foreign_task.pk).exists()


def test_system_administrator_deletes_any_task():
    store = make_store('admin-any-task')
    author = make_user('admin-task-author')
    task = make_task(store, author, 'Любая задача')
    admin = make_user(
        'global-system-admin',
        role=EmployeeProfile.Role.SYSTEM_ADMIN,
    )
    client = Client()
    client.force_login(admin)

    response = client.post(
        reverse('checklists:system_task_delete', args=[task.pk])
    )

    assert response.status_code == 302
    assert not StoreAdHocTask.objects.filter(pk=task.pk).exists()


def test_employee_cannot_delete_store_task():
    store = make_store('employee-denied')
    employee = make_user('plain-employee')
    make_membership(
        employee,
        store,
        UserStoreMembership.Role.EMPLOYEE,
    )
    task = make_task(store, employee, 'Недоступное удаление')

    from checklists.ad_hoc_tasks import delete_ad_hoc_task
    with pytest.raises(OperationNotAllowedError):
        delete_ad_hoc_task(task, actor=employee)
    assert StoreAdHocTask.objects.filter(pk=task.pk).exists()


def test_bud_cannot_be_deleted_or_deactivated_at_model_and_service_level():
    bud = User.objects.create_superuser(
        username='Bud',
        password='Strong-Test-934!',
    )
    admin = User.objects.create_superuser(
        username='BackupRoot',
        password='Strong-Test-934!',
    )

    bud.is_active = False
    with pytest.raises(ValidationError):
        bud.save()
    bud.refresh_from_db()
    assert bud.is_active

    with pytest.raises(OperationNotAllowedError, match='отключить нельзя'):
        deactivate_managed_user(bud, admin)

    with pytest.raises(ValidationError):
        with transaction.atomic():
            bud.delete()
    assert User.objects.filter(pk=bud.pk).exists()


def test_user_card_can_add_and_remove_multiple_store_memberships():
    first = make_store('card-first')
    second = make_store('card-second')
    user = make_user('card-user')
    admin = User.objects.create_superuser(
        username='card-admin',
        password='Strong-Test-934!',
    )
    client = Client()
    client.force_login(admin)

    for store, role in (
        (first, UserStoreMembership.Role.DIRECTOR),
        (second, UserStoreMembership.Role.EMPLOYEE),
    ):
        response = client.post(
            reverse('checklists:system_user_membership_add', args=[user.pk]),
            {
                'store': store.pk,
                'role_in_store': role,
                'is_active': 'on',
            },
        )
        assert response.status_code == 302

    detail = client.get(
        reverse('checklists:system_user_detail', args=[user.pk])
    )
    assert first.name in detail.content.decode()
    assert second.name in detail.content.decode()
    membership = UserStoreMembership.objects.get(user=user, store=second)
    removed = client.post(
        reverse(
            'checklists:system_user_membership_remove',
            args=[user.pk, membership.pk],
        )
    )
    assert removed.status_code == 302
    assert not UserStoreMembership.objects.filter(pk=membership.pk).exists()


def test_membership_service_preserves_second_store():
    first = make_store('service-first')
    second = make_store('service-second')
    user = make_user('service-user')
    admin = User.objects.create_superuser(
        username='service-admin',
        password='Strong-Test-934!',
    )
    set_user_store_membership(
        user=user,
        store=first,
        role_in_store=UserStoreMembership.Role.DIRECTOR,
        actor=admin,
    )
    set_user_store_membership(
        user=user,
        store=second,
        role_in_store=UserStoreMembership.Role.EMPLOYEE,
        actor=admin,
    )
    assert UserStoreMembership.objects.filter(user=user).count() == 2


def test_telegram_section_can_reassign_and_disconnect_profile():
    store = make_store('telegram-management')
    first_user = make_user('telegram-first-user')
    second_user = make_user('telegram-second-user')
    make_membership(first_user, store)
    make_membership(second_user, store)
    profile, binding = make_telegram(first_user, store, 94003)
    admin = User.objects.create_superuser(
        username='telegram-management-admin',
        password='Strong-Test-934!',
    )
    client = Client()
    client.force_login(admin)

    listing = client.get(reverse('checklists:telegram_users'))
    assert first_user.username in listing.content.decode()
    assert store.name in listing.content.decode()

    reassigned = client.post(
        reverse(
            'checklists:telegram_profile_action',
            args=[profile.pk, 'reassign'],
        ),
        {'user': second_user.pk},
    )
    assert reassigned.status_code == 302
    profile.refresh_from_db()
    binding.refresh_from_db()
    assert profile.user == second_user
    assert binding.user == second_user

    disconnected = client.post(
        reverse(
            'checklists:telegram_profile_action',
            args=[profile.pk, 'disconnect'],
        )
    )
    assert disconnected.status_code == 302
    assert not TelegramUserProfile.objects.filter(pk=profile.pk).exists()
    binding.refresh_from_db()
    assert binding.user is None
