import json
from datetime import date, timedelta

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from checklists.models import (
    DailyShiftAssignment,
    EmployeeProfile,
    ShiftTemplate,
    Store,
    StoreEmployee,
)
from checklists.test_portals import create_access_user


pytestmark = pytest.mark.django_db


@pytest.fixture
def shift_calendar_setup():
    store = Store.objects.create(
        name='Магазин календаря',
        code='shift-calendar-main',
        timezone='Europe/Moscow',
    )
    foreign_store = Store.objects.create(
        name='Чужой магазин календаря',
        code='shift-calendar-foreign',
        timezone='Europe/Moscow',
    )
    director, _, _ = create_access_user(
        'shift-calendar-director',
        EmployeeProfile.Role.STORE_DIRECTOR,
        store,
    )
    employee = StoreEmployee.objects.create(
        store=store,
        first_name='Анна',
        last_name='Иванова',
        display_name='Иванова Анна',
        position='Специалист',
        department=StoreEmployee.Department.SERVICE,
    )
    second_employee = StoreEmployee.objects.create(
        store=store,
        first_name='Сергей',
        last_name='Петров',
        display_name='Петров Сергей',
        position='Оператор',
        department=StoreEmployee.Department.CALL_CENTER,
    )
    foreign_employee = StoreEmployee.objects.create(
        store=foreign_store,
        first_name='Чужой',
        display_name='Чужой сотрудник',
    )
    template = ShiftTemplate.objects.create(
        store=store,
        name='День',
        shift_type=DailyShiftAssignment.ShiftType.WORK,
        shift_start='10:00',
        shift_end='22:00',
    )
    client = Client()
    client.force_login(director)
    return {
        'store': store,
        'foreign_store': foreign_store,
        'director': director,
        'employee': employee,
        'second_employee': second_employee,
        'foreign_employee': foreign_employee,
        'template': template,
        'client': client,
    }


def post_calendar(client, updates):
    return client.post(
        reverse('checklists:director_shift_calendar_update'),
        data=json.dumps({'updates': updates}),
        content_type='application/json',
    )


def test_calendar_page_contains_only_directors_store(shift_calendar_setup):
    month = (timezone.localdate() + timedelta(days=40)).replace(day=1)

    response = shift_calendar_setup['client'].get(
        reverse('checklists:director_shifts_bulk'),
        {'month': month.strftime('%Y-%m')},
    )
    content = response.content.decode()

    assert response.status_code == 200
    assert 'Календарь графика сотрудников' in content
    assert shift_calendar_setup['employee'].display_name in content
    assert shift_calendar_setup['second_employee'].display_name in content
    assert shift_calendar_setup['foreign_employee'].display_name not in content
    assert 'checklists/shift_calendar.js' in content
    assert 'id="shift-editor-modal"' in content
    assert 'id="fill-range-button"' in content
    assert 'data-editor-type="night"' in content


def test_calendar_rejects_employee_from_another_store(shift_calendar_setup):
    work_date = timezone.localdate() + timedelta(days=1)

    response = post_calendar(
        shift_calendar_setup['client'],
        [{
            'employee_id': shift_calendar_setup['foreign_employee'].pk,
            'date': work_date.isoformat(),
            'shift_type': DailyShiftAssignment.ShiftType.WORK,
        }],
    )

    assert response.status_code == 403
    assert not DailyShiftAssignment.objects.exists()


def test_calendar_rejects_past_dates(shift_calendar_setup):
    current_month = timezone.localdate().replace(day=1)
    past_month_date = current_month - timedelta(days=1)

    response = post_calendar(
        shift_calendar_setup['client'],
        [{
            'employee_id': shift_calendar_setup['employee'].pk,
            'date': past_month_date.isoformat(),
            'shift_type': DailyShiftAssignment.ShiftType.WORK,
        }],
    )

    assert response.status_code == 403
    assert 'прошлый месяц' in response.json()['error']
    assert not DailyShiftAssignment.objects.exists()


def test_calendar_rejects_elapsed_day_of_current_month(
    shift_calendar_setup,
    monkeypatch,
):
    fixed_today = date(2026, 7, 18)
    monkeypatch.setattr(
        'checklists.management_services.timezone.localdate',
        lambda: fixed_today,
    )

    response = post_calendar(
        shift_calendar_setup['client'],
        [{
            'employee_id': shift_calendar_setup['employee'].pk,
            'date': '2026-07-17',
            'shift_type': DailyShiftAssignment.ShiftType.WORK,
        }],
    )

    assert response.status_code == 403
    assert 'Прошедшие дни текущего месяца' in response.json()['error']
    assert not DailyShiftAssignment.objects.exists()


def test_calendar_bulk_update_saves_range_without_reload(
    shift_calendar_setup,
):
    first_date = timezone.localdate() + timedelta(days=1)
    dates = [first_date + timedelta(days=offset) for offset in range(3)]
    updates = [
        {
            'employee_id': shift_calendar_setup['employee'].pk,
            'date': work_date.isoformat(),
            'shift_type': DailyShiftAssignment.ShiftType.WORK,
            'template_id': shift_calendar_setup['template'].pk,
        }
        for work_date in dates
    ]

    response = post_calendar(shift_calendar_setup['client'], updates)

    assert response.status_code == 200
    assert response.json()['ok'] is True
    assert len(response.json()['cells']) == 3
    assignments = DailyShiftAssignment.objects.order_by('work_date')
    assert assignments.count() == 3
    assert all(
        assignment.shift_type == DailyShiftAssignment.ShiftType.WORK
        for assignment in assignments
    )
    assert all(
        assignment.shift_start.strftime('%H:%M') == '10:00'
        for assignment in assignments
    )
    assert all(
        assignment.created_by == shift_calendar_setup['director']
        for assignment in assignments
    )


def test_calendar_creates_edits_and_deletes_single_shift(
    shift_calendar_setup,
):
    work_date = timezone.localdate() + timedelta(days=3)
    employee = shift_calendar_setup['employee']

    created = post_calendar(
        shift_calendar_setup['client'],
        [{
            'employee_id': employee.pk,
            'date': work_date.isoformat(),
            'shift_type': DailyShiftAssignment.ShiftType.NIGHT,
            'comment': 'Ночная приёмка',
        }],
    )

    assert created.status_code == 200
    created_assignment = created.json()['cells'][0]['assignment']
    assert created_assignment['shift_type'] == 'night'
    assert created_assignment['short'] == 'Н'
    assert created_assignment['comment'] == 'Ночная приёмка'
    assignment = DailyShiftAssignment.objects.get(
        employee=employee,
        work_date=work_date,
    )
    assert assignment.shift_type == DailyShiftAssignment.ShiftType.NIGHT
    assert assignment.comment == 'Ночная приёмка'

    page = shift_calendar_setup['client'].get(
        reverse('checklists:director_shifts_bulk'),
        {'month': work_date.strftime('%Y-%m')},
    )
    assert f'data-assignment-id="{assignment.pk}"' in page.content.decode()
    assert 'data-comment="Ночная приёмка"' in page.content.decode()

    updated = post_calendar(
        shift_calendar_setup['client'],
        [{
            'employee_id': employee.pk,
            'date': work_date.isoformat(),
            'shift_type': DailyShiftAssignment.ShiftType.VACATION,
            'comment': 'Согласованный отпуск',
        }],
    )
    assert updated.status_code == 200
    assignment.refresh_from_db()
    assert assignment.shift_type == DailyShiftAssignment.ShiftType.VACATION
    assert assignment.comment == 'Согласованный отпуск'

    deleted = post_calendar(
        shift_calendar_setup['client'],
        [{
            'employee_id': employee.pk,
            'date': work_date.isoformat(),
            'shift_type': 'clear',
        }],
    )
    assert deleted.status_code == 200
    assert deleted.json()['cells'][0]['assignment'] is None
    assert not DailyShiftAssignment.objects.filter(pk=assignment.pk).exists()


def test_calendar_moves_existing_shift_to_another_date(
    shift_calendar_setup,
):
    employee = shift_calendar_setup['employee']
    original_date = timezone.localdate() + timedelta(days=4)
    new_date = original_date + timedelta(days=1)
    assignment = DailyShiftAssignment.objects.create(
        store=shift_calendar_setup['store'],
        employee=employee,
        work_date=original_date,
        shift_type=DailyShiftAssignment.ShiftType.WORK,
        comment='Исходный комментарий',
        created_by=shift_calendar_setup['director'],
    )

    response = post_calendar(
        shift_calendar_setup['client'],
        [{
            'employee_id': employee.pk,
            'original_date': original_date.isoformat(),
            'date': new_date.isoformat(),
            'shift_type': DailyShiftAssignment.ShiftType.VACATION,
            'comment': 'Перенесено директором',
        }],
    )

    assert response.status_code == 200
    assert response.json()['cells'][0] == {
        'employee_id': employee.pk,
        'date': original_date.isoformat(),
        'assignment': None,
    }
    assert not DailyShiftAssignment.objects.filter(pk=assignment.pk).exists()
    moved = DailyShiftAssignment.objects.get(
        employee=employee,
        work_date=new_date,
    )
    assert moved.shift_type == DailyShiftAssignment.ShiftType.VACATION
    assert moved.comment == 'Перенесено директором'


def test_copy_week_repeats_pattern_for_month(shift_calendar_setup):
    month = (timezone.localdate() + timedelta(days=40)).replace(day=1)
    week_start = month + timedelta(days=(7 - month.weekday()) % 7)
    source_updates = []
    for offset in range(7):
        shift_type = (
            DailyShiftAssignment.ShiftType.DAY_OFF
            if offset in {5, 6}
            else DailyShiftAssignment.ShiftType.WORK
        )
        source_updates.append({
            'employee_id': shift_calendar_setup['employee'].pk,
            'date': (week_start + timedelta(days=offset)).isoformat(),
            'shift_type': shift_type,
        })
    assert post_calendar(
        shift_calendar_setup['client'],
        source_updates,
    ).status_code == 200

    response = shift_calendar_setup['client'].post(
        reverse('checklists:director_shift_calendar_copy_week'),
        data=json.dumps({
            'month': month.strftime('%Y-%m'),
            'week_start': week_start.isoformat(),
            'employee_ids': [shift_calendar_setup['employee'].pk],
        }),
        content_type='application/json',
    )

    assert response.status_code == 200
    assignments = DailyShiftAssignment.objects.filter(
        employee=shift_calendar_setup['employee'],
        work_date__year=month.year,
        work_date__month=month.month,
    )
    assert assignments.count() >= 28
    assert all(
        assignment.shift_type
        == (
            DailyShiftAssignment.ShiftType.DAY_OFF
            if assignment.work_date.weekday() in {5, 6}
            else DailyShiftAssignment.ShiftType.WORK
        )
        for assignment in assignments
    )


def test_legacy_shift_creation_without_shift_type_still_works(
    shift_calendar_setup,
):
    work_date = timezone.localdate() + timedelta(days=2)

    response = shift_calendar_setup['client'].post(
        reverse(
            'checklists:director_shift_add',
            args=[work_date.isoformat()],
        ),
        {
            'employee': shift_calendar_setup['second_employee'].pk,
            'is_responsible_for_checklist': 'on',
            'shift_start': '09:00',
            'shift_end': '18:00',
            'comment': 'Старый сценарий',
        },
    )

    assert response.status_code == 302
    assignment = DailyShiftAssignment.objects.get(
        employee=shift_calendar_setup['second_employee'],
        work_date=work_date,
    )
    assert assignment.shift_type == DailyShiftAssignment.ShiftType.WORK
    assert assignment.comment == 'Старый сценарий'
