from datetime import date, datetime, timedelta, timezone as dt_timezone

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from checklists.exceptions import OperationNotAllowedError
from checklists.calendar_services import set_store_day_status
from checklists.management_services import create_shift_assignment
from checklists.models import (
    ChecklistAnswer,
    ChecklistDayStatus,
    ChecklistNotification,
    DailyChecklist,
    DailyChecklistStage,
    DailyShiftAssignment,
    EmployeeProfile,
    Store,
    StoreDayStatus,
    StoreChecklistSchedule,
    StoreEmployee,
    TelegramOutboundMessage,
    TelegramUserProfile,
    UserStoreMembership,
)
from checklists.reporting_v2 import (
    build_daily_rows,
    build_report_dashboard,
    make_report_period,
)
from checklists.services import create_daily_checklist
from checklists.telegram_reminders import (
    schedule_employee_schedule_reminders,
)
from checklists.test_portals import create_access_user, make_template


pytestmark = pytest.mark.django_db
NORMAL_DATE = date(2026, 8, 3)
TESTING_DATE = date(2026, 8, 4)
DAY_OFF_DATE = date(2026, 8, 5)


@pytest.fixture
def calendar_setup():
    store = Store.objects.create(
        name='Календарный магазин',
        code='calendar-store',
        timezone='Europe/Moscow',
    )
    director, _, _ = create_access_user(
        'calendar-director',
        EmployeeProfile.Role.STORE_DIRECTOR,
        store,
    )
    terminal_user, _, terminal = create_access_user(
        'calendar-terminal',
        EmployeeProfile.Role.STORE_ACCOUNT,
        store,
    )
    make_template(store, director)
    return {
        'store': store,
        'director': director,
        'terminal_user': terminal_user,
        'terminal': terminal,
    }


def complete_daily(daily):
    ChecklistAnswer.objects.filter(
        daily_item__daily_checklist=daily
    ).update(
        status=ChecklistAnswer.Status.COMPLETED,
        answered_at=timezone.now(),
    )
    DailyChecklistStage.objects.filter(daily_checklist=daily).update(
        status=DailyChecklistStage.Status.COMPLETED,
        completed_at=timezone.now(),
    )


def test_testing_day_is_kept_in_history_but_not_in_rating(calendar_setup):
    normal_daily = create_daily_checklist(
        calendar_setup['terminal'],
        NORMAL_DATE,
    )
    complete_daily(normal_daily)
    StoreDayStatus.objects.create(
        store=calendar_setup['store'],
        date=TESTING_DATE,
        status=ChecklistDayStatus.TESTING,
        changed_by=calendar_setup['director'],
    )
    testing_daily = create_daily_checklist(
        calendar_setup['terminal'],
        TESTING_DATE,
    )
    period = make_report_period(NORMAL_DATE, TESTING_DATE)

    dashboard = build_report_dashboard(calendar_setup['store'], period)
    rows = build_daily_rows(calendar_setup['store'], period)

    completion = next(
        card for card in dashboard['cards']
        if card['code'] == 'completion_rate'
    )
    assert completion['value'] == '100%'
    assert len(rows) == 2
    testing_row = next(
        row for row in rows if row['daily'].pk == testing_daily.pk
    )
    assert testing_row['excluded_from_statistics'] is True
    assert testing_row['health_label'] == 'Не учитывается в статистике'


def test_day_off_requires_no_checklist_and_does_not_hurt_statistics(
    calendar_setup,
):
    normal_daily = create_daily_checklist(
        calendar_setup['terminal'],
        NORMAL_DATE,
    )
    complete_daily(normal_daily)
    StoreDayStatus.objects.create(
        store=calendar_setup['store'],
        date=DAY_OFF_DATE,
        status=ChecklistDayStatus.DAY_OFF,
        changed_by=calendar_setup['director'],
    )

    with pytest.raises(OperationNotAllowedError, match='выходной'):
        create_daily_checklist(calendar_setup['terminal'], DAY_OFF_DATE)

    period = make_report_period(NORMAL_DATE, DAY_OFF_DATE)
    dashboard = build_report_dashboard(calendar_setup['store'], period)
    completion = next(
        card for card in dashboard['cards']
        if card['code'] == 'completion_rate'
    )
    assert completion['value'] == '100%'
    assert not DailyChecklist.objects.filter(
        store=calendar_setup['store'],
        checklist_date=DAY_OFF_DATE,
    ).exists()


def test_day_off_cancels_pending_checklist_notifications(calendar_setup):
    daily = create_daily_checklist(
        calendar_setup['terminal'],
        DAY_OFF_DATE,
    )
    stage = daily.stages.get(section_code='opening')
    ChecklistNotification.objects.create(
        stage=stage,
        notification_type=(
            ChecklistNotification.NotificationType.DEADLINE_WARNING
        ),
        scheduled_for=stage.opens_at,
    )

    set_store_day_status(
        store=calendar_setup['store'],
        work_date=DAY_OFF_DATE,
        status=ChecklistDayStatus.DAY_OFF,
        actor=calendar_setup['director'],
    )

    daily.refresh_from_db()
    assert daily.day_status == ChecklistDayStatus.DAY_OFF
    assert not ChecklistNotification.objects.filter(stage=stage).exists()


def test_director_cannot_change_previous_month(calendar_setup):
    today = timezone.localdate()
    current_month = today.replace(day=1)
    previous_month_end = current_month - timedelta(days=1)
    work_date = previous_month_end.replace(day=1)
    employee = StoreEmployee.objects.create(
        store=calendar_setup['store'],
        first_name='Анна',
        display_name='Анна',
    )

    with pytest.raises(OperationNotAllowedError, match='прошлый месяц'):
        create_shift_assignment(
            calendar_setup['store'],
            work_date,
            {
                'employee': employee,
                'is_responsible_for_checklist': True,
                'shift_start': None,
                'shift_end': None,
                'comment': '',
            },
            calendar_setup['director'],
        )


def test_employee_sees_only_personal_schedule(calendar_setup):
    user = User.objects.create_user(
        username='schedule-employee',
        password='Strong-Test-934!',
    )
    UserStoreMembership.objects.create(
        user=user,
        store=calendar_setup['store'],
        role_in_store=UserStoreMembership.Role.EMPLOYEE,
    )
    employee = StoreEmployee.objects.create(
        store=calendar_setup['store'],
        user=user,
        first_name='Иван',
        display_name='Иван',
    )
    foreign_store = Store.objects.create(
        name='Чужой магазин',
        code='foreign-schedule-store',
    )
    foreign_employee = StoreEmployee.objects.create(
        store=foreign_store,
        first_name='Чужой',
        display_name='Чужой сотрудник',
    )
    DailyShiftAssignment.objects.create(
        store=calendar_setup['store'],
        employee=employee,
        work_date=NORMAL_DATE,
        shift_type=DailyShiftAssignment.ShiftType.WORK,
        shift_start='09:00',
        shift_end='18:00',
    )
    DailyShiftAssignment.objects.create(
        store=calendar_setup['store'],
        employee=employee,
        work_date=NORMAL_DATE + timedelta(days=1),
        shift_type=DailyShiftAssignment.ShiftType.VACATION,
    )
    DailyShiftAssignment.objects.create(
        store=calendar_setup['store'],
        employee=employee,
        work_date=NORMAL_DATE + timedelta(days=2),
        shift_type=DailyShiftAssignment.ShiftType.DAY_OFF,
    )
    DailyShiftAssignment.objects.create(
        store=foreign_store,
        employee=foreign_employee,
        work_date=NORMAL_DATE,
        shift_start='10:00',
        shift_end='19:00',
    )
    client = Client()
    client.force_login(user)

    response = client.get(
        reverse('checklists:employee_schedule'),
        {'month': '2026-08'},
    )
    content = response.content.decode()

    assert response.status_code == 200
    assert calendar_setup['store'].name in content
    assert '09:00' in content
    assert 'Отпуск' in content
    assert 'Выходной' in content
    assert foreign_store.name not in content


def test_schedule_reminder_is_queued_three_days_before_month(
    calendar_setup,
):
    StoreEmployee.objects.create(
        store=calendar_setup['store'],
        first_name='Иван',
        last_name='Иванов',
        display_name='Иванов Иван',
    )
    UserStoreMembership.objects.create(
        user=calendar_setup['director'],
        store=calendar_setup['store'],
        role_in_store=UserStoreMembership.Role.DIRECTOR,
    )
    TelegramUserProfile.objects.create(
        user=calendar_setup['director'],
        telegram_user_id=777001,
        telegram_chat_id=777001,
        telegram_username='calendar_director',
        is_verified=True,
    )
    at = datetime(2026, 7, 29, 12, 0, tzinfo=dt_timezone.utc)

    created = schedule_employee_schedule_reminders(at=at)

    assert created == 1
    message = TelegramOutboundMessage.objects.get(
        message_type='employee_schedule_missing'
    )
    assert message.chat_id == '777001'
    assert 'заполнен не полностью' in message.payload['text']
    assert 'Иванов Иван' in message.payload['text']
    assert calendar_setup['store'].name in message.payload['text']
    assert '/director/shifts/bulk-create/?month=2026-08' in (
        message.payload['text']
    )
    assert schedule_employee_schedule_reminders(at=at) == 0


def test_workweek_marks_unselected_weekday_as_day_off(calendar_setup):
    schedule, _ = StoreChecklistSchedule.objects.get_or_create(
        store=calendar_setup['store']
    )
    schedule.working_weekdays = [0, 1, 2, 3, 4]
    schedule.save()
    saturday = date(2026, 8, 1)

    with pytest.raises(OperationNotAllowedError, match='выходной'):
        create_daily_checklist(calendar_setup['terminal'], saturday)


def test_questions_page_loads_sortablejs(calendar_setup):
    client = Client()
    client.force_login(calendar_setup['director'])

    response = client.get(reverse('checklists:director_questions'))

    assert response.status_code == 200
    content = response.content.decode()
    assert 'Sortable.min.js' in content
    assert 'data-question-list' in content

    question = (
        calendar_setup['store']
        .checklist_templates.get()
        .versions.get(status='published')
        .sections.get(code='opening')
        .items.get()
    )
    reordered = client.post(
        reverse('checklists:director_questions_reorder'),
        {
            'section_code': 'opening',
            'ordered_ids': [str(question.pk)],
        },
        HTTP_X_REQUESTED_WITH='XMLHttpRequest',
    )
    result = reordered.json()
    assert reordered.status_code == 200
    assert result['ok'] is True
    assert result['ordered_ids'] != [question.pk]
