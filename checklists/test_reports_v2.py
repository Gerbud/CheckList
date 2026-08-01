from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from django.test import Client
from django.urls import reverse

from checklists.models import (
    AnswerRevision,
    ChecklistAnswer,
    DailyChecklistStage,
    DailyShiftAssignment,
    EmployeeProfile,
    Store,
    StoreAdHocTask,
    StoreEmployee,
)
from checklists.reporting_v2 import (
    build_employee_rows,
    build_recurring_problems,
    build_report_dashboard,
    calculate_store_health,
    collect_store_facts,
    get_task_analytics,
    make_report_period,
)
from checklists.services import create_daily_checklist
from checklists.test_portals import create_access_user, make_template


pytestmark = pytest.mark.django_db
WORK_DATE = date(2026, 7, 16)
MOSCOW = ZoneInfo('Europe/Moscow')
NOW = datetime(2026, 7, 16, 21, 0, tzinfo=MOSCOW)


@pytest.fixture
def report_setup(monkeypatch):
    monkeypatch.setattr('django.utils.timezone.now', lambda: NOW)
    own = Store.objects.create(
        name='Магазин аналитики',
        code='reports-own',
        timezone='Europe/Moscow',
    )
    foreign = Store.objects.create(
        name='Чужой магазин',
        code='reports-foreign',
        timezone='Europe/Moscow',
    )
    director, _, _ = create_access_user(
        'reports-director',
        EmployeeProfile.Role.STORE_DIRECTOR,
        own,
    )
    admin, _, _ = create_access_user(
        'reports-admin',
        EmployeeProfile.Role.SYSTEM_ADMIN,
    )
    terminal_user, _, terminal = create_access_user(
        'reports-terminal',
        EmployeeProfile.Role.STORE_ACCOUNT,
        own,
    )
    employee = StoreEmployee.objects.create(
        store=own,
        first_name='Анна',
        last_name='Тестова',
        display_name='Анна Тестова',
        personnel_number='R-1',
    )
    foreign_employee = StoreEmployee.objects.create(
        store=foreign,
        first_name='Чужой',
        display_name='Чужой сотрудник',
    )
    make_template(own, director)
    daily = create_daily_checklist(terminal, WORK_DATE)
    period = make_report_period(WORK_DATE, WORK_DATE)
    return {
        'own': own,
        'foreign': foreign,
        'director': director,
        'admin': admin,
        'terminal_user': terminal_user,
        'employee': employee,
        'foreign_employee': foreign_employee,
        'daily': daily,
        'period': period,
    }


def complete_all(report_setup):
    ChecklistAnswer.objects.filter(
        daily_item__daily_checklist=report_setup['daily']
    ).update(
        status=ChecklistAnswer.Status.COMPLETED,
        answered_by_employee=report_setup['employee'],
        last_edited_by_employee=report_setup['employee'],
        answered_at=NOW - timedelta(hours=1),
    )
    DailyChecklistStage.objects.filter(
        daily_checklist=report_setup['daily']
    ).update(
        status=DailyChecklistStage.Status.COMPLETED,
        completed_at=NOW - timedelta(hours=1),
        completed_by_employee=report_setup['employee'],
    )


def report_url(name, **params):
    base = reverse(f'checklists:{name}')
    if not params:
        return base
    from urllib.parse import urlencode
    return f'{base}?{urlencode(params)}'


@pytest.mark.parametrize(
    'name',
    (
        'director_reports',
        'director_report_daily',
        'director_report_employees',
        'director_report_revisions',
        'director_report_tasks',
        'director_report_problems',
        'director_report_recurring',
    ),
)
def test_director_can_open_all_report_pages(client, report_setup, name):
    client.force_login(report_setup['director'])
    response = client.get(report_url(name, period='today'))
    assert response.status_code == 200
    assert report_setup['own'].name in response.content.decode()


def test_system_admin_requires_and_uses_selected_store(client, report_setup):
    client.force_login(report_setup['admin'])
    assert client.get(reverse('checklists:director_reports')).status_code == 403
    client.post(
        reverse('checklists:system_select_managed_store'),
        {'store': report_setup['own'].pk},
    )
    response = client.get(reverse('checklists:director_reports'))
    assert response.status_code == 200
    assert report_setup['own'].name in response.content.decode()
    assert report_setup['foreign'].name not in response.content.decode()


def test_store_account_cannot_open_reports(client, report_setup):
    client.force_login(report_setup['terminal_user'])
    assert client.get(reverse('checklists:director_reports')).status_code == 403


def test_store_health_is_normal_when_everything_is_complete(report_setup):
    complete_all(report_setup)
    health = calculate_store_health(
        report_setup['own'],
        report_setup['period'],
    )
    assert health['code'] == 'normal'
    assert health['reasons'] == []


def test_store_health_requires_attention_for_failed_answer(report_setup):
    complete_all(report_setup)
    answer = ChecklistAnswer.objects.filter(
        daily_item__daily_checklist=report_setup['daily']
    ).first()
    ChecklistAnswer.objects.filter(pk=answer.pk).update(
        status=ChecklistAnswer.Status.FAILED,
        comment='Проверка не выполнена',
    )
    health = calculate_store_health(
        report_setup['own'],
        report_setup['period'],
    )
    assert health['code'] == 'attention'
    assert any('не выполнено' in reason.lower() for reason in health['reasons'])


def test_store_health_is_critical_for_required_missing_answer(report_setup):
    health = calculate_store_health(
        report_setup['own'],
        report_setup['period'],
    )
    assert health['code'] == 'critical'
    assert any('без ответа' in reason.lower() for reason in health['reasons'])


def test_dashboard_starts_with_attention_and_explains_health(client, report_setup):
    client.force_login(report_setup['director'])
    response = client.get(
        report_url(
            'director_reports',
            date_from=WORK_DATE,
            date_to=WORK_DATE,
        )
    )
    content = response.content.decode()
    assert 'Что требует внимания' in content
    assert 'Состояние магазина: Критично' in content
    assert 'Обязательный вопрос без ответа' in content


def test_overdue_stage_and_task_are_detected(report_setup):
    DailyChecklistStage.objects.filter(
        daily_checklist=report_setup['daily']
    ).update(status=DailyChecklistStage.Status.OVERDUE)
    StoreAdHocTask.objects.create(
        store=report_setup['own'],
        date=WORK_DATE - timedelta(days=1),
        section_code=StoreAdHocTask.SectionCode.MORNING,
        text='Просроченная выкладка',
        status=StoreAdHocTask.Status.ACTIVE,
    )
    facts = collect_store_facts(report_setup['own'], report_setup['period'])
    types = {
        row['type']
        for row in calculate_store_health(
            report_setup['own'],
            report_setup['period'],
            facts,
        )['problems']
    }
    assert 'incomplete_stage' in types
    # Задача вне однодневного периода не должна проникать в отчёт.
    assert 'overdue_task' not in types


def test_shift_employee_without_actions_is_problem(report_setup):
    DailyShiftAssignment.objects.create(
        store=report_setup['own'],
        employee=report_setup['employee'],
        work_date=WORK_DATE,
    )
    types = {
        row['type']
        for row in calculate_store_health(
            report_setup['own'],
            report_setup['period'],
        )['problems']
    }
    assert 'missing_participation' in types


def test_revision_after_deadline_appears_in_revision_report(
    client,
    report_setup,
):
    complete_all(report_setup)
    answer = ChecklistAnswer.objects.filter(
        daily_item__daily_checklist=report_setup['daily']
    ).first()
    AnswerRevision.objects.create(
        answer=answer,
        daily_item=answer.daily_item,
        changed_by_user=report_setup['terminal_user'],
        changed_by_employee=report_setup['employee'],
        previous_status=ChecklistAnswer.Status.FAILED,
        new_status=ChecklistAnswer.Status.COMPLETED,
        change_reason='Повторная проверка выполнена',
    )
    revision = AnswerRevision.objects.get()
    AnswerRevision.objects.filter(pk=revision.pk).update(
        changed_at=NOW + timedelta(hours=2)
    )
    client.force_login(report_setup['director'])
    response = client.get(
        report_url(
            'director_report_revisions',
            date_from=WORK_DATE,
            date_to=WORK_DATE,
            only_after_deadline=1,
        )
    )
    assert response.status_code == 200
    assert 'Повторная проверка выполнена' in response.content.decode()


def test_employee_report_and_detail_show_concrete_reasons(client, report_setup):
    DailyShiftAssignment.objects.create(
        store=report_setup['own'],
        employee=report_setup['employee'],
        work_date=WORK_DATE,
    )
    client.force_login(report_setup['director'])
    list_response = client.get(
        report_url(
            'director_report_employees',
            date_from=WORK_DATE,
            date_to=WORK_DATE,
            only_problems=1,
        )
    )
    assert 'Анна Тестова' in list_response.content.decode()
    detail = client.get(
        reverse(
            'checklists:director_report_employee_detail',
            args=[report_setup['employee'].pk],
        )
    )
    assert detail.status_code == 200
    assert 'не участвовал' in detail.content.decode()


def test_foreign_employee_filter_and_detail_are_neutral_404(client, report_setup):
    client.force_login(report_setup['director'])
    assert client.get(
        report_url(
            'director_report_employees',
            employee=report_setup['foreign_employee'].pk,
        )
    ).status_code == 404
    assert client.get(
        reverse(
            'checklists:director_report_employee_detail',
            args=[report_setup['foreign_employee'].pk],
        )
    ).status_code == 404


def test_recurring_problems_uses_stable_task_key(report_setup):
    for text in ('  Проверить   витрину ', 'проверить витрину'):
        StoreAdHocTask.objects.create(
            store=report_setup['own'],
            date=WORK_DATE,
            section_code=StoreAdHocTask.SectionCode.DAY,
            text=text,
            status=StoreAdHocTask.Status.FAILED,
        )
    result = build_recurring_problems(
        report_setup['own'],
        report_setup['period'],
    )
    task_row = next(
        row for row in result['rows']
        if row['category'] == 'Невыполненная задача'
    )
    assert task_row['count'] == 2


def test_task_report_puts_problem_tasks_first(report_setup):
    StoreAdHocTask.objects.create(
        store=report_setup['own'],
        date=WORK_DATE,
        section_code=StoreAdHocTask.SectionCode.DAY,
        text='Обычная',
        status=StoreAdHocTask.Status.COMPLETED,
    )
    problem = StoreAdHocTask.objects.create(
        store=report_setup['own'],
        date=WORK_DATE,
        section_code=StoreAdHocTask.SectionCode.MORNING,
        text='Проблемная',
        status=StoreAdHocTask.Status.FAILED,
    )
    data = get_task_analytics(
        report_setup['own'],
        report_setup['period'],
    )
    assert data['rows'][0] == problem
    assert data['failed'] == 1


@pytest.mark.parametrize(
    ('name', 'marker'),
    (
        ('director_report_daily', 'Ежедневный отчёт'),
        ('director_report_employees', 'Сотрудники'),
        ('director_report_tasks', 'Задачи'),
        ('director_report_revisions', 'Изменения ответов'),
        ('director_report_recurring', 'Повторяющиеся проблемы'),
    ),
)
def test_csv_exports_use_bom_and_store_scope(
    client,
    report_setup,
    name,
    marker,
):
    StoreAdHocTask.objects.create(
        store=report_setup['foreign'],
        date=WORK_DATE,
        section_code=StoreAdHocTask.SectionCode.DAY,
        text='СЕКРЕТ ЧУЖОГО МАГАЗИНА',
    )
    client.force_login(report_setup['director'])
    response = client.get(
        report_url(
            name,
            date_from=WORK_DATE,
            date_to=WORK_DATE,
            format='csv',
        )
    )
    assert response.status_code == 200
    assert response.content.startswith(b'\xef\xbb\xbf')
    assert 'СЕКРЕТ ЧУЖОГО МАГАЗИНА'.encode() not in response.content


def test_invalid_and_oversized_date_range_does_not_error(client, report_setup):
    client.force_login(report_setup['director'])
    invalid = client.get(
        report_url(
            'director_reports',
            date_from='not-a-date',
            date_to='also-bad',
        )
    )
    assert invalid.status_code == 200
    oversized = client.get(
        report_url(
            'director_reports',
            date_from='2020-01-01',
            date_to='2026-07-16',
        )
    )
    assert oversized.status_code == 200
    assert oversized.context['period'].was_limited


def test_reports_have_breadcrumbs_mobile_markers_and_empty_state(
    client,
    report_setup,
):
    client.force_login(report_setup['director'])
    response = client.get(
        report_url(
            'director_report_tasks',
            date_from=WORK_DATE,
            date_to=WORK_DATE,
        )
    )
    content = response.content.decode()
    assert 'aria-label="Хлебные крошки"' in content
    assert 'table-responsive' in content or 'row g-2' in content
    assert 'Задач по выбранным фильтрам нет' in content


def test_system_report_lists_stores_and_can_drill_down(client, report_setup):
    client.force_login(report_setup['admin'])
    response = client.get(reverse('checklists:system_reports'))
    content = response.content.decode()
    assert response.status_code == 200
    assert report_setup['own'].name in content
    assert 'Открыть отчёты магазина' in content


def test_dashboard_query_count_is_bounded(client, report_setup, django_assert_max_num_queries):
    client.force_login(report_setup['director'])
    with django_assert_max_num_queries(80):
        response = client.get(
            report_url(
                'director_reports',
                date_from=WORK_DATE,
                date_to=WORK_DATE,
            )
        )
    assert response.status_code == 200
