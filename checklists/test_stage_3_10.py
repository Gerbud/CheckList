from datetime import timedelta
import base64

import pytest
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from checklists.models import (
    EmployeeProfile,
    Store,
    StoreAdHocTask,
    StoreChecklistSchedule,
    TelegramOutboundMessage,
    TelegramStoreBinding,
    TelegramUserProfile,
)
from checklists.telegram_bot import process_telegram_update
from checklists.telegram_client import TelegramResponse
from checklists.telegram_queue import delete_telegram_message


pytestmark = pytest.mark.django_db


def make_store(code):
    store = Store.objects.create(name=f'Магазин {code}', code=code)
    StoreChecklistSchedule.objects.create(store=store)
    return store


def make_director(store, username):
    user = User.objects.create_user(username=username, password='Strong-Test-934!')
    EmployeeProfile.objects.create(
        user=user,
        role=EmployeeProfile.Role.STORE_DIRECTOR,
        store=store,
    )
    return user


def make_task(store, author, text='Проверить задачу'):
    return StoreAdHocTask.objects.create(
        store=store,
        date=timezone.localdate() + timedelta(days=1),
        section_code=StoreAdHocTask.SectionCode.MORNING,
        text=text,
        created_by=author,
    )


def message_update(update_id, user_id, text):
    return {
        'update_id': update_id,
        'message': {
            'text': text,
            'from': {
                'id': user_id,
                'username': f'user{user_id}',
                'first_name': 'Telegram',
                'is_bot': False,
            },
            'chat': {'id': user_id, 'type': 'private'},
        },
    }


def callback_update(update_id, user_id, data):
    return {
        'update_id': update_id,
        'callback_query': {
            'id': f'callback-{update_id}',
            'data': data,
            'from': {
                'id': user_id,
                'username': f'user{user_id}',
                'first_name': 'Telegram',
                'is_bot': False,
            },
            'message': {'chat': {'id': user_id, 'type': 'private'}},
        },
    }


def test_superuser_deletes_any_task():
    store = make_store('admin-delete')
    director = make_director(store, 'task-owner')
    task = make_task(store, director)
    admin = User.objects.create_superuser(
        username='root-task-admin',
        password='Strong-Test-934!',
    )
    client = Client()
    client.force_login(admin)

    response = client.post(
        reverse('checklists:system_task_delete', args=[task.pk])
    )

    assert response.status_code == 302
    assert not StoreAdHocTask.objects.filter(pk=task.pk).exists()


def test_director_deletes_task_in_own_store_only():
    store = make_store('director-delete')
    foreign_store = make_store('director-delete-foreign')
    owner = make_director(store, 'director-owner')
    other = make_director(foreign_store, 'director-other')
    own_task = make_task(store, owner, 'Своя задача')
    foreign_task = make_task(foreign_store, other, 'Чужая задача')
    client = Client()
    client.force_login(owner)

    forbidden = client.post(
        reverse('checklists:director_task_delete', args=[foreign_task.pk])
    )
    deleted = client.post(
        reverse('checklists:director_task_delete', args=[own_task.pk])
    )

    assert forbidden.status_code == 404
    assert StoreAdHocTask.objects.filter(pk=foreign_task.pk).exists()
    assert deleted.status_code == 302
    assert not StoreAdHocTask.objects.filter(pk=own_task.pk).exists()


def test_bud_is_listed_and_cannot_be_deleted():
    bud = User.objects.create_superuser(
        username='Bud',
        password='Strong-Test-934!',
    )
    admin = User.objects.create_superuser(
        username='SecondRoot',
        password='Strong-Test-934!',
    )
    client = Client()
    client.force_login(admin)

    listing = client.get(reverse('checklists:system_users'))
    detail = client.get(
        reverse('checklists:system_user_detail', args=[bud.pk])
    )
    forbidden = client.post(
        reverse('checklists:system_user_delete', args=[bud.pk])
    )

    assert 'Bud' in listing.content.decode()
    assert 'Главный администратор' in listing.content.decode()
    assert 'Удалить пользователя' not in detail.content.decode()
    assert forbidden.status_code == 403
    assert User.objects.filter(pk=bud.pk).exists()


def test_non_superuser_admin_can_be_deleted():
    actor = User.objects.create_superuser(
        username='deletion-root',
        password='Strong-Test-934!',
    )
    managed_admin = User.objects.create_user(
        username='ordinary-system-admin',
        password='Strong-Test-934!',
    )
    EmployeeProfile.objects.create(
        user=managed_admin,
        role=EmployeeProfile.Role.SYSTEM_ADMIN,
        store=None,
    )
    client = Client()
    client.force_login(actor)

    response = client.post(
        reverse('checklists:system_user_delete', args=[managed_admin.pk])
    )

    assert response.status_code == 302
    assert not User.objects.filter(pk=managed_admin.pk).exists()


def test_store_logo_upload_is_saved_and_rendered():
    store = make_store('logo-store')
    admin = User.objects.create_superuser(
        username='logo-admin',
        password='Strong-Test-934!',
    )
    image = SimpleUploadedFile(
        'logo.gif',
        base64.b64decode(
            'R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=='
        ),
        content_type='image/gif',
    )
    client = Client()
    client.force_login(admin)

    response = client.post(
        reverse('checklists:system_store_edit', args=[store.pk]),
        {
            'name': store.name,
            'code': store.code,
            'timezone': store.timezone,
            'is_active': 'on',
            'logo': image,
        },
    )

    store.refresh_from_db()
    assert response.status_code == 302
    assert store.logo.name.startswith('stores/logo/')
    detail = client.get(
        reverse('checklists:system_store_detail', args=[store.pk])
    )
    assert store.logo.url in detail.content.decode()


def test_linked_telegram_user_creates_task_with_author():
    store = make_store('telegram-author')
    user = make_director(store, 'telegram-linked-director')
    binding = TelegramStoreBinding.objects.create(
        store=store,
        user=user,
        telegram_user_id=93001,
        telegram_chat_id=93001,
        username='linked',
    )

    process_telegram_update(message_update(93010, 93001, '/start'))
    process_telegram_update(message_update(93011, 93001, '/task'))
    process_telegram_update(
        callback_update(93012, 93001, 'task:date:tomorrow')
    )
    process_telegram_update(
        callback_update(93013, 93001, 'task:section:morning')
    )
    process_telegram_update(
        message_update(93014, 93001, 'Задача из Telegram')
    )
    process_telegram_update(
        callback_update(93015, 93001, 'task:skip-description')
    )
    process_telegram_update(
        callback_update(93016, 93001, 'task:confirm')
    )

    task = StoreAdHocTask.objects.get(
        created_by_telegram_binding=binding
    )
    profile = TelegramUserProfile.objects.get(user=user)
    assert task.created_by == user
    assert task.source == StoreAdHocTask.Source.TELEGRAM
    assert profile.telegram_user_id == 93001
    assert profile.is_verified


def test_unlinked_telegram_user_cannot_start_task():
    store = make_store('telegram-unlinked')
    TelegramStoreBinding.objects.create(
        store=store,
        telegram_user_id=93002,
        telegram_chat_id=93002,
    )

    process_telegram_update(message_update(93020, 93002, '/task'))

    outbound = TelegramOutboundMessage.objects.get(
        idempotency_key='update:93020:account-not-linked'
    )
    assert outbound.payload['text'] == 'Ваш Telegram не привязан к аккаунту.'
    assert not StoreAdHocTask.objects.exists()


def test_telegram_message_deletion_calls_delete_message(monkeypatch):
    admin = User.objects.create_superuser(
        username='message-admin',
        password='Strong-Test-934!',
    )
    message = TelegramOutboundMessage.objects.create(
        chat_id='93003',
        method='sendMessage',
        payload={'chat_id': '93003', 'text': 'Сообщение'},
        message_type='test',
        idempotency_key='stage-3-10-delete',
        status=TelegramOutboundMessage.Status.SENT,
        telegram_message_id=445566,
        sent_at=timezone.now(),
    )
    calls = []

    def sender(method, payload):
        calls.append((method, payload))
        return TelegramResponse(
            data={'ok': True, 'result': True},
            alternative_attempts=1,
            official_attempts=0,
        )

    monkeypatch.setattr('checklists.telegram_queue.send_telegram_request', sender)
    delete_telegram_message(message, actor=admin)

    message.refresh_from_db()
    assert calls == [
        (
            'deleteMessage',
            {'chat_id': '93003', 'message_id': 445566},
        )
    ]
    assert message.status == TelegramOutboundMessage.Status.DELETED
    assert message.deleted_at is not None
    assert message.deleted_by == admin
