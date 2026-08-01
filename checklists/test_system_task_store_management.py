from datetime import timedelta

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from checklists.models import (
    AuditLog,
    EmployeeProfile,
    Store,
    StoreAdHocTask,
)
from checklists.test_portals import create_access_user


pytestmark = pytest.mark.django_db


@pytest.fixture
def task_store_setup():
    source_store = Store.objects.create(
        name='Исходный магазин',
        code='task-source-store',
        timezone='Europe/Moscow',
    )
    target_store = Store.objects.create(
        name='Новый магазин',
        code='task-target-store',
        timezone='Europe/Moscow',
    )
    director, _, _ = create_access_user(
        'task-store-director',
        EmployeeProfile.Role.STORE_DIRECTOR,
        source_store,
    )
    admin, _, _ = create_access_user(
        'task-store-admin',
        EmployeeProfile.Role.SYSTEM_ADMIN,
    )
    task = StoreAdHocTask.objects.create(
        store=source_store,
        date=timezone.localdate() + timedelta(days=20),
        section_code=StoreAdHocTask.SectionCode.DAY,
        text='Проверить выкладку',
        description='Проверить центральную витрину',
        is_required=True,
        created_by=director,
    )
    return {
        'source_store': source_store,
        'target_store': target_store,
        'director': director,
        'admin': admin,
        'task': task,
    }


def task_form_payload(task, **overrides):
    payload = {
        'date': task.date.isoformat(),
        'section_code': task.section_code,
        'text': task.text,
        'description': task.description,
        'is_required': 'on',
        'confirmation': 'on',
    }
    payload.update(overrides)
    return payload


def test_system_admin_sees_and_changes_task_store_with_audit(
    task_store_setup,
):
    client = Client()
    client.force_login(task_store_setup['admin'])
    task = task_store_setup['task']
    target_store = task_store_setup['target_store']
    edit_url = reverse('checklists:system_task_edit', args=[task.pk])

    page = client.get(edit_url)

    assert page.status_code == 200
    assert page.context['form'].fields['store'].queryset.filter(
        pk=target_store.pk,
    ).exists()
    assert page.content.decode().index('Магазин') < page.content.decode().index(
        'Дата'
    )
    response = client.post(
        edit_url,
        task_form_payload(task, store=target_store.pk),
    )

    assert response.status_code == 302
    task.refresh_from_db()
    assert task.store == target_store
    audit = AuditLog.objects.filter(
        object_type=task._meta.label_lower,
        object_id=str(task.pk),
        action=AuditLog.Action.STORE_TASK_UPDATED,
    ).latest('created_at')
    assert audit.old_value['store_id'] == task_store_setup['source_store'].pk
    assert audit.new_value['store_id'] == target_store.pk
    assert audit.store == target_store


def test_director_task_form_does_not_expose_or_accept_foreign_store(
    task_store_setup,
):
    client = Client()
    client.force_login(task_store_setup['director'])
    task = task_store_setup['task']
    edit_url = reverse('checklists:director_task_edit', args=[task.pk])

    page = client.get(edit_url)

    assert page.status_code == 200
    assert 'store' not in page.context['form'].fields
    response = client.post(
        edit_url,
        task_form_payload(
            task,
            store=task_store_setup['target_store'].pk,
            text='Изменённая задача',
        ),
    )

    assert response.status_code == 302
    task.refresh_from_db()
    assert task.store == task_store_setup['source_store']
    assert task.text == 'Изменённая задача'


def test_system_admin_copies_task_without_changing_source(task_store_setup):
    client = Client()
    client.force_login(task_store_setup['admin'])
    source = task_store_setup['task']
    target_store = task_store_setup['target_store']
    copy_date = source.date + timedelta(days=1)

    response = client.post(
        reverse('checklists:system_task_copy', args=[source.pk]),
        {
            'target_store': target_store.pk,
            'date': copy_date.isoformat(),
            'confirmation': 'on',
        },
    )

    assert response.status_code == 302
    source.refresh_from_db()
    assert source.store == task_store_setup['source_store']
    copied = StoreAdHocTask.objects.exclude(pk=source.pk).get()
    assert copied.store == target_store
    assert copied.date == copy_date
    assert copied.text == source.text
    assert copied.description == source.description
    assert copied.section_code == source.section_code
    assert copied.is_required is source.is_required
    assert copied.status == StoreAdHocTask.Status.PLANNED
    assert copied.created_by == task_store_setup['admin']
    audit = AuditLog.objects.get(
        object_type=copied._meta.label_lower,
        object_id=str(copied.pk),
        action=AuditLog.Action.STORE_TASK_CREATED_BY_ADMIN,
    )
    assert audit.new_value['copied_from_task_id'] == source.pk
    assert audit.new_value['copied_from_store_id'] == source.store_id


def test_system_task_list_shows_store_and_edit_link(task_store_setup):
    client = Client()
    client.force_login(task_store_setup['admin'])

    response = client.get(reverse('checklists:system_tasks'))
    content = response.content.decode()

    assert response.status_code == 200
    assert task_store_setup['source_store'].name in content
    assert reverse(
        'checklists:system_task_edit',
        args=[task_store_setup['task'].pk],
    ) in content
