from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.urls import reverse

from checklists.exceptions import ChecklistLockedError, OperationNotAllowedError
from checklists.management_services import update_store_schedule
from checklists.models import (
    AuditLog,
    ChecklistAnswer,
    ChecklistItem,
    ChecklistNotification,
    ChecklistSection,
    ChecklistTemplate,
    ChecklistTemplateVersion,
    DailyChecklistStage,
    EmployeeProfile,
    Store,
    StoreChecklistSchedule,
    StoreNotificationSettings,
)
from checklists.notifications import schedule_stage_notifications, send_notification
from checklists.reporting import get_stage_daily_summary
from checklists.services import (
    build_stage_schedule,
    can_complete_stage,
    complete_checklist_stage,
    create_daily_checklist,
    publish_template_version,
    update_answer,
)


pytestmark = pytest.mark.django_db
MOSCOW = ZoneInfo('Europe/Moscow')
WORK_DATE = date(2026, 7, 20)


def at(hour, minute=0, *, next_day=False):
    work_date = WORK_DATE + (timedelta(days=1) if next_day else timedelta())
    return datetime(
        work_date.year,
        work_date.month,
        work_date.day,
        hour,
        minute,
        tzinfo=MOSCOW,
    )


def make_profile(store, username, role=EmployeeProfile.Role.EMPLOYEE):
    user = User.objects.create_user(username=username, password='Safe-Test-934!')
    return EmployeeProfile.objects.create(user=user, store=store, role=role)


def make_template(store, actor):
    template = ChecklistTemplate.objects.create(
        store=store,
        name='Окна завершения',
    )
    version = ChecklistTemplateVersion.objects.create(
        template=template,
        version_number=1,
        created_by=actor,
    )
    for order, (code, label) in enumerate(
        (
            ('opening', 'Утренний вопрос'),
            ('during_day', 'Дневной вопрос'),
            ('closing', 'Вечерний вопрос'),
        ),
        start=1,
    ):
        section = ChecklistSection.objects.create(
            version=version,
            name=label,
            code=code,
            sort_order=order,
        )
        ChecklistItem.objects.create(
            section=section,
            text=label,
            sort_order=1,
            is_required=True,
        )
        if code == 'during_day':
            ChecklistItem.objects.create(
                section=section,
                text='Числовой дневной вопрос',
                sort_order=2,
                is_required=False,
                answer_type=ChecklistItem.AnswerType.INTEGER,
            )
            ChecklistItem.objects.create(
                section=section,
                text='Неактивный дневной вопрос',
                sort_order=3,
                is_required=False,
                is_active=False,
            )
    publish_template_version(version, actor)


@pytest.fixture
def window_setup(monkeypatch):
    monkeypatch.setattr('django.utils.timezone.now', lambda: at(10))
    store = Store.objects.create(
        name='Магазин окон',
        code='completion-window-store',
        timezone='Europe/Moscow',
    )
    director = make_profile(
        store,
        'window-director',
        EmployeeProfile.Role.MANAGER,
    )
    employee = make_profile(store, 'window-employee')
    schedule = StoreChecklistSchedule.objects.create(store=store)
    make_template(store, director.user)
    daily = create_daily_checklist(employee, WORK_DATE)
    return {
        'store': store,
        'director': director,
        'employee': employee,
        'schedule': schedule,
        'daily': daily,
    }


def answer_for(setup, section_code):
    return (
        setup['daily'].items.filter(section_code=section_code)
        .order_by('item_sort_order')
        .first()
        .answer
    )


def save_completed_answer(setup, section_code, operation_at=at(10)):
    return update_answer(
        answer_for(setup, section_code),
        ChecklistAnswer.Status.COMPLETED,
        '',
        setup['employee'].user,
        at=operation_at,
    )


def test_default_windows_are_120_minutes_and_snapshotted(window_setup):
    schedule = window_setup['schedule']
    assert schedule.morning_completion_window_minutes == 120
    assert schedule.day_completion_window_minutes == 120
    assert schedule.evening_completion_window_minutes == 120

    stages = {
        stage.section_code: stage
        for stage in window_setup['daily'].stages.all()
    }
    assert stages['opening'].completion_available_at == at(9)
    assert stages['during_day'].completion_available_at == at(18)
    assert stages['closing'].completion_available_at == at(20)


@pytest.mark.parametrize('value', (-15, 17, 735))
def test_window_must_be_in_valid_15_minute_steps(window_setup, value):
    schedule = window_setup['schedule']
    schedule.day_completion_window_minutes = value
    with pytest.raises(ValidationError):
        schedule.full_clean()


def test_database_rejects_window_outside_15_minute_steps(window_setup):
    with pytest.raises(IntegrityError), transaction.atomic():
        StoreChecklistSchedule.objects.filter(
            pk=window_setup['schedule'].pk,
        ).update(day_completion_window_minutes=17)


def test_morning_user_can_open_and_save_day_and_evening_drafts(
    client,
    window_setup,
):
    client.force_login(window_setup['employee'].user)
    day_response = client.get(reverse('checklists:during_day'))
    closing_response = client.get(reverse('checklists:closing'))
    answer = answer_for(window_setup, 'closing')
    save_response = client.post(
        reverse('checklists:closing'),
        {
            'section_code': 'closing',
            f'answer_{answer.pk}_status': ChecklistAnswer.Status.COMPLETED,
            f'answer_{answer.pk}_comment': '',
            'action': 'save',
        },
    )

    answer.refresh_from_db()
    closing_stage = window_setup['daily'].stages.get(section_code='closing')
    assert day_response.status_code == 200
    assert 'Дневной вопрос' in day_response.content.decode()
    assert closing_response.status_code == 200
    assert 'Вечерний вопрос' in closing_response.content.decode()
    assert save_response.status_code == 302
    assert answer.status == ChecklistAnswer.Status.COMPLETED
    assert closing_stage.completed_at is None
    assert closing_stage.completed_by_employee is None

    reloaded = client.get(reverse('checklists:closing')).content.decode()
    assert f'name="answer_{answer.pk}_status"' in reloaded
    assert 'value="completed" checked' in reloaded
    dashboard = client.get(reverse('checklists:dashboard')).content.decode()
    assert 'Готов к завершению' in dashboard


def test_dashboard_shows_all_stages_as_early_fillable(client, window_setup):
    client.force_login(window_setup['employee'].user)
    response = client.get(reverse('checklists:dashboard'))
    content = response.content.decode()

    assert response.status_code == 200
    assert reverse('checklists:opening') in content
    assert reverse('checklists:during_day') in content
    assert reverse('checklists:closing') in content
    assert 'Можно открыть и заполнить заранее' in content
    assert 'Завершение доступно с' in content
    assert '18:00' in content


def test_early_complete_post_saves_answers_but_does_not_complete(
    client,
    window_setup,
):
    client.force_login(window_setup['employee'].user)
    answer = answer_for(window_setup, 'during_day')
    response = client.post(
        reverse('checklists:during_day'),
        {
            'section_code': 'during_day',
            f'answer_{answer.pk}_status': ChecklistAnswer.Status.COMPLETED,
            f'answer_{answer.pk}_comment': '',
            'action': 'complete_stage',
        },
    )

    answer.refresh_from_db()
    stage = window_setup['daily'].stages.get(section_code='during_day')
    assert response.status_code == 200
    assert (
        'Ответы сохранены, но завершить этап можно только после 18:00.'
        in response.content.decode()
    )
    assert answer.status == ChecklistAnswer.Status.COMPLETED
    assert stage.completed_at is None
    assert stage.completed_by_employee is None
    assert not AuditLog.objects.filter(
        action=AuditLog.Action.CHECKLIST_STAGE_COMPLETED,
        object_id=str(stage.pk),
    ).exists()


def test_before_window_is_rejected_by_backend(window_setup):
    stage = window_setup['daily'].stages.get(section_code='during_day')
    save_completed_answer(window_setup, 'during_day')

    with pytest.raises(ChecklistLockedError, match='после 18:00'):
        complete_checklist_stage(
            stage,
            window_setup['employee'].user,
            at=at(17, 59),
        )
    stage.refresh_from_db()
    assert stage.completed_at is None


@pytest.mark.parametrize('moment', (at(18), at(19, 30)))
def test_at_window_boundary_and_inside_window_completion_is_allowed(
    window_setup,
    moment,
):
    stage = window_setup['daily'].stages.get(section_code='during_day')
    save_completed_answer(window_setup, 'during_day')

    completed = complete_checklist_stage(
        stage,
        window_setup['employee'].user,
        at=moment,
    )
    assert completed.status == DailyChecklistStage.Status.COMPLETED
    assert completed.completed_at == moment


def test_after_deadline_preserves_completed_late_behavior(window_setup):
    stage = window_setup['daily'].stages.get(section_code='during_day')
    save_completed_answer(window_setup, 'during_day')

    completed = complete_checklist_stage(
        stage,
        window_setup['employee'].user,
        at=at(20, 1),
    )
    assert completed.status == DailyChecklistStage.Status.COMPLETED_LATE


def test_zero_window_allows_completion_from_stage_opening(window_setup):
    schedule = window_setup['schedule']
    schedule.day_completion_window_minutes = 0
    schedule.save()
    second_daily = create_daily_checklist(
        window_setup['employee'],
        WORK_DATE + timedelta(days=1),
    )
    stage = second_daily.stages.get(section_code='during_day')
    answer = (
        second_daily.items.filter(
            section_code='during_day',
            answer_type_snapshot=ChecklistItem.AnswerType.STATUS,
        )
        .first()
        .answer
    )
    update_answer(
        answer,
        ChecklistAnswer.Status.COMPLETED,
        '',
        window_setup['employee'].user,
        at=at(10, next_day=True),
    )

    with pytest.raises(ChecklistLockedError):
        complete_checklist_stage(
            stage,
            window_setup['employee'].user,
            at=at(10, 59, next_day=True),
        )
    completed = complete_checklist_stage(
        stage,
        window_setup['employee'].user,
        at=at(11, next_day=True),
    )
    assert completed.status == DailyChecklistStage.Status.COMPLETED


def test_zero_window_direct_post_before_opening_saves_draft_but_does_not_complete(
    client,
    window_setup,
    monkeypatch,
):
    window_setup['schedule'].morning_completion_window_minutes = 0
    window_setup['schedule'].save()
    future_daily = create_daily_checklist(
        window_setup['employee'],
        WORK_DATE + timedelta(days=1),
    )
    future_stage = future_daily.stages.get(section_code='opening')
    assert future_stage.completion_available_at == at(9, next_day=True)

    monkeypatch.setattr(
        'django.utils.timezone.now',
        lambda: at(8, 59),
    )
    client.force_login(window_setup['employee'].user)
    answer = answer_for(window_setup, 'opening')
    response = client.post(
        reverse('checklists:opening'),
        {
            'section_code': 'opening',
            f'answer_{answer.pk}_status': ChecklistAnswer.Status.COMPLETED,
            f'answer_{answer.pk}_comment': '',
            'action': 'complete_stage',
        },
    )

    answer.refresh_from_db()
    current_stage = window_setup['daily'].stages.get(section_code='opening')
    assert response.status_code == 200
    assert answer.status == ChecklistAnswer.Status.COMPLETED
    assert current_stage.completed_at is None
    assert 'только после 09:00' in response.content.decode()


@pytest.mark.parametrize(
    ('window_minutes', 'expected'),
    (
        (15, at(19, 45, next_day=True)),
        (30, at(19, 30, next_day=True)),
        (45, at(19, 15, next_day=True)),
        (90, at(18, 30, next_day=True)),
        (720, at(11, next_day=True)),
    ),
)
def test_minute_windows_and_long_window_clamping(
    window_setup,
    window_minutes,
    expected,
):
    window_setup['schedule'].day_completion_window_minutes = window_minutes
    window_setup['schedule'].save()
    daily = create_daily_checklist(
        window_setup['employee'],
        WORK_DATE + timedelta(days=1),
    )
    stage = daily.stages.get(section_code='during_day')
    assert stage.completion_available_at == expected


def test_draft_resave_does_not_duplicate_answer(client, window_setup):
    client.force_login(window_setup['employee'].user)
    answer = answer_for(window_setup, 'during_day')
    payload = {
        'section_code': 'during_day',
        f'answer_{answer.pk}_status': ChecklistAnswer.Status.COMPLETED,
        f'answer_{answer.pk}_comment': '',
        'action': 'save',
    }
    assert client.post(reverse('checklists:during_day'), payload).status_code == 302
    assert client.post(reverse('checklists:during_day'), payload).status_code == 302

    assert ChecklistAnswer.objects.filter(daily_item=answer.daily_item).count() == 1
    stage = window_setup['daily'].stages.get(section_code='during_day')
    assert stage.completed_at is None


def test_dashboard_progress_counts_active_status_and_integer_answers(
    client,
    window_setup,
):
    client.force_login(window_setup['employee'].user)
    dashboard = client.get(reverse('checklists:dashboard')).content.decode()
    assert 'Заполнено 0 из 2 вопросов — 0%' in dashboard

    status_answer = answer_for(window_setup, 'during_day')
    update_answer(
        status_answer,
        ChecklistAnswer.Status.COMPLETED,
        '',
        window_setup['employee'].user,
        at=at(10),
    )
    dashboard = client.get(reverse('checklists:dashboard')).content.decode()
    assert 'Заполнено 1 из 2 вопросов — 50%' in dashboard
    assert 'Заполнено частично' in dashboard

    integer_answer = ChecklistAnswer.objects.get(
        daily_item__daily_checklist=window_setup['daily'],
        daily_item__section_code='during_day',
        daily_item__answer_type_snapshot=ChecklistItem.AnswerType.INTEGER,
    )
    update_answer(
        integer_answer,
        None,
        '',
        window_setup['employee'].user,
        integer_value=7,
        at=at(10),
    )
    dashboard = client.get(reverse('checklists:dashboard')).content.decode()
    stage = window_setup['daily'].stages.get(section_code='during_day')
    assert 'Заполнено 2 из 2 вопросов — 100%' in dashboard
    assert 'Готов к завершению' in dashboard
    assert stage.completed_at is None
    assert not window_setup['daily'].items.filter(
        item_text='Неактивный дневной вопрос',
    ).exists()


def test_saved_pending_comment_is_draft_but_not_answered(
    client,
    window_setup,
):
    client.force_login(window_setup['employee'].user)
    answer = answer_for(window_setup, 'opening')
    response = client.post(
        reverse('checklists:opening'),
        {
            'section_code': 'opening',
            f'answer_{answer.pk}_status': ChecklistAnswer.Status.PENDING,
            f'answer_{answer.pk}_comment': 'Подготовлен черновик',
            'action': 'save',
        },
    )
    assert response.status_code == 302
    dashboard = client.get(reverse('checklists:dashboard')).content.decode()
    assert 'Есть черновик' in dashboard
    assert 'Заполнено 0 из 1 вопросов — 0%' in dashboard


def test_completed_stage_is_read_only_in_ui_post_and_service(
    client,
    window_setup,
):
    answer = answer_for(window_setup, 'opening')
    save_completed_answer(window_setup, 'opening')
    stage = window_setup['daily'].stages.get(section_code='opening')
    complete_checklist_stage(
        stage,
        window_setup['employee'].user,
        at=at(10),
    )
    client.force_login(window_setup['employee'].user)

    page = client.get(reverse('checklists:opening'))
    content = page.content.decode()
    assert page.status_code == 200
    assert 'Сохранить ответы' not in content
    assert f'name="answer_{answer.pk}_status"' not in content

    post = client.post(
        reverse('checklists:opening'),
        {
            'section_code': 'opening',
            f'answer_{answer.pk}_status': ChecklistAnswer.Status.FAILED,
            f'answer_{answer.pk}_comment': 'Попытка изменения',
            'action': 'save',
        },
    )
    assert post.status_code == 403
    with pytest.raises(ChecklistLockedError, match='только для чтения'):
        update_answer(
            answer,
            ChecklistAnswer.Status.FAILED,
            'Попытка изменения',
            window_setup['employee'].user,
            at=at(10, 1),
        )


def test_draft_keeps_notifications_and_completion_excludes_stage(
    settings,
    window_setup,
):
    settings.TELEGRAM_NOTIFICATIONS_ENABLED = True
    settings.TELEGRAM_BOT_TOKEN = 'test-token'
    StoreNotificationSettings.objects.create(
        store=window_setup['store'],
        telegram_chat_id='-1001234567890',
    )
    stage = window_setup['daily'].stages.get(section_code='during_day')
    assert schedule_stage_notifications(stage) == 2
    notification_ids = list(
        stage.notifications.values_list('pk', flat=True)
    )

    save_completed_answer(window_setup, 'during_day')
    stage.refresh_from_db()
    assert stage.status not in {
        DailyChecklistStage.Status.COMPLETED,
        DailyChecklistStage.Status.COMPLETED_LATE,
    }
    assert stage.notifications.count() == 2

    complete_checklist_stage(
        stage,
        window_setup['employee'].user,
        at=at(18),
    )
    assert schedule_stage_notifications(stage) == 0
    for notification_id in notification_ids:
        notification = ChecklistNotification.objects.get(pk=notification_id)
        assert send_notification(notification) == 'skipped'
    assert stage.notifications.count() == 0


def test_different_stores_keep_different_windows_and_snapshot_history(
    client,
    window_setup,
):
    other_store = Store.objects.create(
        name='Другой магазин',
        code='other-window-store',
    )
    other_director = make_profile(
        other_store,
        'other-window-director',
        EmployeeProfile.Role.MANAGER,
    )
    other_employee = make_profile(other_store, 'other-window-employee')
    StoreChecklistSchedule.objects.create(
        store=other_store,
        day_completion_window_minutes=180,
    )
    make_template(other_store, other_director.user)
    other_daily = create_daily_checklist(other_employee, WORK_DATE)

    first_stage = window_setup['daily'].stages.get(section_code='during_day')
    other_stage = other_daily.stages.get(section_code='during_day')
    assert first_stage.completion_available_at == at(18)
    assert other_stage.completion_available_at == at(17)

    window_setup['schedule'].day_completion_window_minutes = 30
    window_setup['schedule'].save()
    first_stage.refresh_from_db()
    assert first_stage.completion_available_at == at(18)
    assert not can_complete_stage(first_stage, at=at(17, 59))
    assert can_complete_stage(first_stage, at=at(18))
    client.force_login(window_setup['employee'].user)
    dashboard = client.get(reverse('checklists:dashboard'))
    day_context = next(
        item
        for item in dashboard.context['stages']
        if item['stage'].section_code == 'during_day'
    )
    assert day_context['completion_available_time'] == '18:00'

    newer_daily = create_daily_checklist(
        window_setup['employee'],
        WORK_DATE + timedelta(days=1),
    )
    newer_stage = newer_daily.stages.get(section_code='during_day')
    assert newer_stage.completion_available_at == at(
        19,
        30,
        next_day=True,
    )


def test_director_cannot_change_other_store_but_system_admin_can(
    window_setup,
):
    other_store = Store.objects.create(
        name='Защищённый магазин',
        code='protected-window-store',
    )
    StoreChecklistSchedule.objects.create(store=other_store)
    admin_user = User.objects.create_user('window-system-admin')
    EmployeeProfile.objects.create(
        user=admin_user,
        role=EmployeeProfile.Role.SYSTEM_ADMIN,
        store=None,
    )
    data = {
        'day_completion_window_minutes': 240,
    }

    with pytest.raises(OperationNotAllowedError):
        update_store_schedule(
            other_store,
            data,
            window_setup['director'].user,
        )
    updated = update_store_schedule(other_store, data, admin_user)
    assert updated.day_completion_window_minutes == 240


def test_director_schedule_page_exposes_and_saves_completion_windows(
    client,
    window_setup,
):
    client.force_login(window_setup['director'].user)
    page = client.get(reverse('checklists:director_schedule'))
    content = page.content.decode()
    assert page.status_code == 200
    assert (
        'За сколько времени до окончания разрешать завершение этапа'
        in content
    )
    assert 'name="day_completion_window_minutes"' in content
    assert 'Сразу после открытия' in content
    assert '1 час 30 минут' in content

    response = client.post(
        reverse('checklists:director_schedule'),
        {
            'action': 'schedule',
            'opening_time': '09:00',
            'morning_deadline': '11:00',
            'daytime_deadline': '20:00',
            'closing_deadline': '22:00',
            'morning_completion_window_minutes': '60',
            'day_completion_window_minutes': '90',
            'evening_completion_window_minutes': '240',
            'warning_minutes_before': '30',
            'notifications_enabled': 'on',
            'working_weekdays': ['0', '1', '2', '3', '4', '5', '6'],
            'is_active': 'on',
        },
    )
    window_setup['schedule'].refresh_from_db()
    assert response.status_code == 302
    assert window_setup['schedule'].morning_completion_window_minutes == 60
    assert window_setup['schedule'].day_completion_window_minutes == 90
    assert window_setup['schedule'].evening_completion_window_minutes == 240
    audit = AuditLog.objects.get(
        action=AuditLog.Action.STORE_SCHEDULE_UPDATED,
        store=window_setup['store'],
    )
    assert audit.actor == window_setup['director'].user
    assert audit.old_value['day_completion_window_minutes'] == 120
    assert audit.new_value['day_completion_window_minutes'] == 90
    assert {
        change['stage']
        for change in audit.new_value['completion_window_changes']
    } == {'opening', 'during_day', 'closing'}


def test_director_schedule_form_rejects_non_step_minutes(
    client,
    window_setup,
):
    client.force_login(window_setup['director'].user)
    response = client.post(
        reverse('checklists:director_schedule'),
        {
            'action': 'schedule',
            'opening_time': '09:00',
            'morning_deadline': '11:00',
            'daytime_deadline': '20:00',
            'closing_deadline': '22:00',
            'morning_completion_window_minutes': '120',
            'day_completion_window_minutes': '17',
            'evening_completion_window_minutes': '120',
            'warning_minutes_before': '30',
            'working_weekdays': ['0', '1', '2', '3', '4', '5', '6'],
            'is_active': 'on',
        },
    )
    window_setup['schedule'].refresh_from_db()
    assert response.status_code == 200
    assert window_setup['schedule'].day_completion_window_minutes == 120
    assert 'шагом 15 минут' in response.content.decode()


def test_director_stage_summary_is_exclusive_store_scoped_and_one_query(
    client,
    window_setup,
    django_assert_num_queries,
):
    completed_daily = window_setup['daily']
    completed_stage = completed_daily.stages.get(section_code='opening')
    save_completed_answer(window_setup, 'opening')
    complete_checklist_stage(
        completed_stage,
        window_setup['employee'].user,
        at=at(10),
    )

    draft_employee = make_profile(
        window_setup['store'],
        'summary-draft-employee',
    )
    draft_daily = create_daily_checklist(draft_employee, WORK_DATE)
    draft_answer = draft_daily.items.get(
        section_code='opening',
    ).answer
    update_answer(
        draft_answer,
        ChecklistAnswer.Status.COMPLETED,
        '',
        draft_employee.user,
        at=at(10),
    )

    untouched_employee = make_profile(
        window_setup['store'],
        'summary-untouched-employee',
    )
    create_daily_checklist(untouched_employee, WORK_DATE)

    overdue_employee = make_profile(
        window_setup['store'],
        'summary-overdue-employee',
    )
    overdue_daily = create_daily_checklist(overdue_employee, WORK_DATE)
    overdue_daily.stages.filter(section_code='opening').update(
        deadline_at=at(9, 30),
    )

    with django_assert_num_queries(1):
        summary = get_stage_daily_summary(
            window_setup['store'],
            WORK_DATE,
            at=at(10),
        )
    opening = next(
        item for item in summary if item['section_code'] == 'opening'
    )
    assert opening == {
        'section_code': 'opening',
        'label': 'Утренние задачи',
        'completed': 1,
        'drafts': 1,
        'not_started': 1,
        'overdue': 1,
    }
    assert sum(
        opening[key]
        for key in ('completed', 'drafts', 'not_started', 'overdue')
    ) == 4

    other_store = Store.objects.create(
        name='Магазин вне сводки',
        code='summary-other-store',
    )
    StoreChecklistSchedule.objects.create(store=other_store)
    other_director = make_profile(
        other_store,
        'summary-other-director',
        EmployeeProfile.Role.MANAGER,
    )
    other_employee = make_profile(other_store, 'summary-other-employee')
    make_template(other_store, other_director.user)
    create_daily_checklist(other_employee, WORK_DATE)
    unchanged_summary = get_stage_daily_summary(
        window_setup['store'],
        WORK_DATE,
        at=at(10),
    )
    unchanged_opening = next(
        item
        for item in unchanged_summary
        if item['section_code'] == 'opening'
    )
    assert unchanged_opening == opening

    client.force_login(window_setup['director'].user)
    director_page = client.get(
        reverse('checklists:director_dashboard')
    ).content.decode()
    assert 'Этапы сегодня' in director_page
    assert 'Есть черновик' in director_page
    assert other_store.name not in director_page


def test_employee_cannot_complete_other_store_stage(window_setup):
    other_store = Store.objects.create(
        name='Чужой этап',
        code='foreign-stage-store',
    )
    other_director = make_profile(
        other_store,
        'foreign-stage-director',
        EmployeeProfile.Role.MANAGER,
    )
    other_employee = make_profile(other_store, 'foreign-stage-employee')
    StoreChecklistSchedule.objects.create(store=other_store)
    make_template(other_store, other_director.user)
    other_daily = create_daily_checklist(other_employee, WORK_DATE)
    other_stage = other_daily.stages.get(section_code='opening')

    with pytest.raises(OperationNotAllowedError):
        complete_checklist_stage(
            other_stage,
            window_setup['employee'].user,
            at=at(10),
        )


def test_completed_stage_cannot_be_completed_twice(window_setup):
    stage = window_setup['daily'].stages.get(section_code='opening')
    save_completed_answer(window_setup, 'opening')
    complete_checklist_stage(
        stage,
        window_setup['employee'].user,
        at=at(10),
    )

    with pytest.raises(ChecklistLockedError, match='уже завершён'):
        complete_checklist_stage(
            stage,
            window_setup['employee'].user,
            at=at(10, 1),
        )


def test_overnight_closing_window_uses_next_day():
    store = Store.objects.create(
        name='Ночной магазин',
        code='overnight-window-store',
        timezone='Europe/Moscow',
    )
    StoreChecklistSchedule.objects.create(
        store=store,
        closing_deadline=time(1),
        evening_completion_window_minutes=120,
    )

    schedule = build_stage_schedule(store, WORK_DATE)
    closing = schedule['closing']
    assert closing['opens_at'] == at(20)
    assert closing['completion_available_at'] == at(23)
    assert closing['deadline_at'] == at(1, next_day=True)
