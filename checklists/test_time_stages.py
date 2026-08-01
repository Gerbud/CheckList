from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.urls import reverse

from checklists.exceptions import ChecklistLockedError
from checklists.models import (
    AuditLog,
    ChecklistAnswer,
    ChecklistItem,
    ChecklistSection,
    ChecklistTemplate,
    ChecklistTemplateVersion,
    DailyChecklist,
    DailyChecklistStage,
    EmployeeProfile,
    Store,
    StoreChecklistSchedule,
)
from checklists.services import (
    _display_order,
    build_stage_schedule,
    complete_checklist_stage,
    create_daily_checklist,
    get_current_stage,
    get_stage_state,
    publish_template_version,
    reopen_daily_checklist,
    update_answer,
)


pytestmark = pytest.mark.django_db
MOSCOW = ZoneInfo('Europe/Moscow')
CHECKLIST_DATE = date(2026, 7, 16)


def at(hour, minute=0, *, next_day=False):
    target_date = CHECKLIST_DATE + (timedelta(days=1) if next_day else timedelta())
    return datetime.combine(target_date, datetime.min.time(), tzinfo=MOSCOW).replace(
        hour=hour,
        minute=minute,
    )


def make_profile(store, username, role=EmployeeProfile.Role.EMPLOYEE):
    user = User.objects.create_user(username=username, password='Safe-Test-934!')
    return EmployeeProfile.objects.create(user=user, store=store, role=role)


def make_template(store, manager, item_count=10):
    template = ChecklistTemplate.objects.create(store=store, name='Этапный шаблон')
    version = ChecklistTemplateVersion.objects.create(
        template=template,
        version_number=1,
        created_by=manager.user,
    )
    for sort_order, (code, name) in enumerate(
        (
            ('opening', 'Открытие магазина'),
            ('during_day', 'В течение дня'),
            ('closing', 'Закрытие смены'),
        ),
        start=1,
    ):
        section = ChecklistSection.objects.create(
            version=version,
            name=name,
            code=code,
            sort_order=sort_order,
        )
        for item_number in range(item_count):
            ChecklistItem.objects.create(
                section=section,
                text=f'{code}: пункт {item_number}',
                sort_order=item_number,
            )
    publish_template_version(version, manager.user)
    return version


@pytest.fixture
def stage_setup():
    store = Store.objects.create(
        name='Магазин с этапами',
        code='stage-store',
        timezone='Europe/Moscow',
    )
    manager = make_profile(store, 'stage-manager', EmployeeProfile.Role.MANAGER)
    employee = make_profile(store, 'stage-employee')
    StoreChecklistSchedule.objects.create(
        store=store,
        morning_completion_window_minutes=720,
        day_completion_window_minutes=720,
        evening_completion_window_minutes=720,
    )
    make_template(store, manager)
    daily = create_daily_checklist(employee, CHECKLIST_DATE)
    return {
        'store': store,
        'manager': manager,
        'employee': employee,
        'daily': daily,
    }


def answer_stage(daily, section_code, actor, operation_at):
    answers = ChecklistAnswer.objects.filter(
        daily_item__daily_checklist=daily,
        daily_item__section_code=section_code,
    )
    for answer in answers:
        update_answer(
            answer,
            ChecklistAnswer.Status.COMPLETED,
            '',
            actor,
            at=operation_at,
        )


def test_schedule_uses_store_timezone_and_exact_boundaries(stage_setup):
    schedule = build_stage_schedule(stage_setup['store'], CHECKLIST_DATE)

    assert schedule['opening']['opens_at'] == at(9)
    assert schedule['opening']['deadline_at'] == at(11)
    assert schedule['during_day']['opens_at'] == at(11)
    assert schedule['during_day']['deadline_at'] == at(20)
    assert schedule['closing']['opens_at'] == at(20)
    assert schedule['closing']['deadline_at'] == at(22)


def test_daily_creation_builds_three_unique_stages(stage_setup):
    stages = stage_setup['daily'].stages.order_by('opens_at')

    assert list(stages.values_list('section_code', flat=True)) == [
        'opening',
        'during_day',
        'closing',
    ]
    assert stages.count() == 3


@pytest.mark.parametrize(
    ('section_code', 'moment', 'expected'),
    (
        ('opening', at(8, 59), DailyChecklistStage.Status.LOCKED),
        ('opening', at(9), DailyChecklistStage.Status.AVAILABLE),
        ('opening', at(10, 59), DailyChecklistStage.Status.AVAILABLE),
        ('opening', at(11), DailyChecklistStage.Status.OVERDUE),
        ('during_day', at(10, 59), DailyChecklistStage.Status.LOCKED),
        ('during_day', at(11), DailyChecklistStage.Status.AVAILABLE),
        ('during_day', at(20), DailyChecklistStage.Status.OVERDUE),
        ('closing', at(19, 59), DailyChecklistStage.Status.LOCKED),
        ('closing', at(20), DailyChecklistStage.Status.AVAILABLE),
        ('closing', at(22), DailyChecklistStage.Status.OVERDUE),
    ),
)
def test_stage_state_at_schedule_boundaries(
    stage_setup,
    section_code,
    moment,
    expected,
):
    stage = stage_setup['daily'].stages.get(section_code=section_code)
    assert get_stage_state(stage, moment) == expected


def test_current_stage_prefers_available_over_older_overdue(stage_setup):
    daily = stage_setup['daily']

    assert get_current_stage(daily, at(12)).section_code == 'during_day'
    assert get_current_stage(daily, at(21)).section_code == 'closing'


def test_future_stage_can_be_edited_but_not_completed(stage_setup):
    daily = stage_setup['daily']
    employee = stage_setup['employee']
    stage = daily.stages.get(section_code='closing')
    answer = daily.items.filter(section_code='closing').first().answer

    updated = update_answer(
        answer,
        ChecklistAnswer.Status.COMPLETED,
        '',
        employee.user,
        at=at(12),
    )
    assert updated.status == ChecklistAnswer.Status.COMPLETED
    with pytest.raises(ChecklistLockedError, match='только после 20:00'):
        complete_checklist_stage(stage, employee.user, at=at(12))


def test_overdue_stage_remains_editable_and_completes_late(stage_setup):
    daily = stage_setup['daily']
    employee = stage_setup['employee']
    completion_time = at(12)
    answer_stage(daily, 'opening', employee.user, completion_time)
    stage = daily.stages.get(section_code='opening')

    completed = complete_checklist_stage(
        stage,
        employee.user,
        at=completion_time,
    )

    assert completed.status == DailyChecklistStage.Status.COMPLETED_LATE
    assert completed.completed_at == completion_time


def test_completion_exactly_at_deadline_counts_as_on_time(stage_setup):
    daily = stage_setup['daily']
    employee = stage_setup['employee']
    deadline = at(11)
    answer_stage(daily, 'opening', employee.user, at(10, 59))
    stage = daily.stages.get(section_code='opening')

    completed = complete_checklist_stage(stage, employee.user, at=deadline)

    assert completed.status == DailyChecklistStage.Status.COMPLETED


def test_stage_completion_audit_contains_deadline_result_and_metadata(stage_setup):
    daily = stage_setup['daily']
    employee = stage_setup['employee']
    answer_stage(daily, 'opening', employee.user, at(9, 30))
    stage = daily.stages.get(section_code='opening')

    complete_checklist_stage(
        stage,
        employee.user,
        {'ip_address': '192.0.2.40', 'user_agent': 'StageTest/1.0'},
        at=at(10),
    )
    log = AuditLog.objects.get(
        action=AuditLog.Action.CHECKLIST_STAGE_COMPLETED,
        object_id=str(stage.pk),
    )

    assert log.new_value['section_code'] == 'opening'
    assert log.new_value['deadline_at'] == stage.deadline_at.isoformat()
    assert log.new_value['completed_at'] == at(10).isoformat()
    assert log.new_value['result'] == DailyChecklistStage.Status.COMPLETED
    assert log.ip_address == '192.0.2.40'


def test_last_stage_automatically_completes_daily_checklist(stage_setup):
    daily = stage_setup['daily']
    employee = stage_setup['employee']
    completion_times = {
        'opening': at(10),
        'during_day': at(12),
        'closing': at(21),
    }
    for stage in daily.stages.order_by('opens_at'):
        answer_stage(daily, stage.section_code, employee.user, completion_times[stage.section_code])
        complete_checklist_stage(
            stage,
            employee.user,
            at=completion_times[stage.section_code],
        )
        daily.refresh_from_db()
        if stage.section_code != 'closing':
            assert daily.status != DailyChecklist.Status.COMPLETED

    daily.refresh_from_db()
    assert daily.status == DailyChecklist.Status.COMPLETED
    assert daily.completed_at == at(21)
    assert AuditLog.objects.filter(
        action=AuditLog.Action.DAILY_CHECKLIST_COMPLETED,
        object_id=str(daily.pk),
    ).count() == 1


def test_manager_can_reopen_only_selected_stage(stage_setup):
    daily = stage_setup['daily']
    employee = stage_setup['employee']
    manager = stage_setup['manager']
    for stage in daily.stages.order_by('opens_at'):
        operation_at = stage.opens_at + timedelta(minutes=30)
        answer_stage(daily, stage.section_code, employee.user, operation_at)
        complete_checklist_stage(stage, employee.user, at=operation_at)

    reopen_daily_checklist(
        daily,
        manager.user,
        section_code='opening',
        at=at(12),
    )
    daily.refresh_from_db()
    stages = {stage.section_code: stage for stage in daily.stages.all()}

    assert daily.status == DailyChecklist.Status.REOPENED
    assert daily.completed_at is None
    assert stages['opening'].status == DailyChecklistStage.Status.OVERDUE
    assert stages['opening'].completed_at is None
    assert stages['during_day'].status == DailyChecklistStage.Status.COMPLETED
    assert stages['closing'].status == DailyChecklistStage.Status.COMPLETED
    assert stages['opening'].opens_at == at(9)
    assert stages['opening'].deadline_at == at(11)


def test_schedule_change_only_affects_new_daily_checklists(stage_setup):
    old_daily = stage_setup['daily']
    old_boundaries = list(
        old_daily.stages.order_by('opens_at').values_list(
            'opens_at',
            'deadline_at',
        )
    )
    schedule = stage_setup['store'].checklist_schedule
    schedule.opening_time = datetime.min.time().replace(hour=8)
    schedule.morning_deadline = datetime.min.time().replace(hour=10)
    schedule.save()

    new_daily = create_daily_checklist(
        stage_setup['employee'],
        CHECKLIST_DATE + timedelta(days=1),
    )

    assert old_boundaries == list(
        old_daily.stages.order_by('opens_at').values_list(
            'opens_at',
            'deadline_at',
        )
    )
    assert new_daily.stages.get(section_code='opening').opens_at.astimezone(
        MOSCOW
    ).time() == datetime.min.time().replace(hour=8)


def test_invalid_schedule_order_and_warning_are_rejected(stage_setup):
    schedule = stage_setup['store'].checklist_schedule
    schedule.opening_time = datetime.min.time().replace(hour=12)
    with pytest.raises(ValidationError):
        schedule.full_clean()

    schedule.opening_time = datetime.min.time().replace(hour=9)
    schedule.warning_minutes_before = 121
    with pytest.raises(ValidationError, match='короткого этапа'):
        schedule.full_clean()


def test_warning_minutes_are_exposed_to_timer(client, stage_setup, monkeypatch):
    schedule = stage_setup['store'].checklist_schedule
    schedule.warning_minutes_before = 45
    schedule.save()
    monkeypatch.setattr('django.utils.timezone.now', lambda: at(10, 20))
    client.force_login(stage_setup['employee'].user)

    response = client.get(reverse('checklists:opening'))

    assert response.status_code == 200
    assert response.context['stage_context']['warning_at'] == at(10, 15)
    assert 'data-warning-at=' in response.content.decode()


def test_display_order_is_stable_and_input_specific(stage_setup, settings):
    settings.RANDOMIZATION_SECRET = 'stable-test-secret'
    employee = stage_setup['employee']

    same = _display_order(employee.pk, CHECKLIST_DATE, 'opening', 42)
    assert same == _display_order(employee.pk, CHECKLIST_DATE, 'opening', 42)
    assert same != _display_order(employee.pk, CHECKLIST_DATE + timedelta(days=1), 'opening', 42)
    assert same != _display_order(employee.pk + 1, CHECKLIST_DATE, 'opening', 42)


def test_snapshot_query_uses_persisted_display_order(stage_setup):
    daily = stage_setup['daily']
    expected = list(
        daily.items.filter(section_code='opening')
        .order_by('display_order', 'id')
        .values_list('pk', flat=True)
    )

    assert list(
        daily.items.filter(section_code='opening').values_list('pk', flat=True)
    ) == expected
    assert all(
        value > 0
        for value in daily.items.values_list('display_order', flat=True)
    )


def test_persisted_question_order_changes_by_date_and_employee(stage_setup):
    first_daily = stage_setup['daily']
    employee = stage_setup['employee']
    second_employee = make_profile(stage_setup['store'], 'second-stage-employee')
    next_day = create_daily_checklist(
        employee,
        CHECKLIST_DATE + timedelta(days=1),
    )
    other_employee_day = create_daily_checklist(second_employee, CHECKLIST_DATE)

    def source_order(daily):
        return list(
            daily.items.filter(section_code='opening')
            .order_by('display_order', 'id')
            .values_list('source_item_id', flat=True)
        )

    original_order = source_order(first_daily)
    assert original_order == source_order(first_daily)
    assert original_order != source_order(next_day)
    assert original_order != source_order(other_employee_day)


def test_new_template_version_does_not_change_old_daily_order(stage_setup):
    daily = stage_setup['daily']
    manager = stage_setup['manager']
    before = list(
        daily.items.order_by('section_sort_order', 'display_order', 'id')
        .values_list('pk', 'display_order')
    )
    template = daily.template_version.template
    version = ChecklistTemplateVersion.objects.create(
        template=template,
        version_number=2,
        created_by=manager.user,
    )
    section = ChecklistSection.objects.create(
        version=version,
        name='Новое открытие',
        code='opening',
        sort_order=1,
    )
    ChecklistItem.objects.create(section=section, text='Совсем новый пункт')
    publish_template_version(version, manager.user)

    assert before == list(
        daily.items.order_by('section_sort_order', 'display_order', 'id')
        .values_list('pk', 'display_order')
    )


def test_today_redirects_to_current_stage(client, stage_setup, monkeypatch):
    monkeypatch.setattr('django.utils.timezone.now', lambda: at(12))
    client.force_login(stage_setup['employee'].user)

    response = client.get(reverse('checklists:today'))

    assert response.status_code == 302
    assert response.url == reverse('checklists:during_day')


def test_future_stage_url_and_draft_save_are_available(
    client,
    stage_setup,
    monkeypatch,
):
    monkeypatch.setattr('django.utils.timezone.now', lambda: at(12))
    client.force_login(stage_setup['employee'].user)

    get_response = client.get(reverse('checklists:closing'))
    post_response = client.post(
        reverse('checklists:closing'),
        {'action': 'save'},
    )

    assert get_response.status_code == 200
    assert 'closing: пункт 0' in get_response.content.decode()
    assert post_response.status_code == 302


def test_server_rejects_section_code_substitution(client, stage_setup, monkeypatch):
    monkeypatch.setattr('django.utils.timezone.now', lambda: at(12))
    client.force_login(stage_setup['employee'].user)
    answer = stage_setup['daily'].items.filter(section_code='opening').first().answer

    response = client.post(
        reverse('checklists:opening'),
        {
            'section_code': 'closing',
            f'answer_{answer.pk}_status': ChecklistAnswer.Status.COMPLETED,
            f'answer_{answer.pk}_comment': '',
            'action': 'save',
        },
    )

    answer.refresh_from_db()
    assert response.status_code == 403
    assert answer.status == ChecklistAnswer.Status.PENDING


@pytest.mark.parametrize(
    ('moment', 'expected_url'),
    (
        (at(8, 59), 'checklists:opening'),
        (at(9), 'checklists:opening'),
        (at(10, 59), 'checklists:opening'),
        (at(11), 'checklists:during_day'),
        (at(15), 'checklists:during_day'),
        (at(20), 'checklists:closing'),
        (at(21), 'checklists:closing'),
        (at(22), 'checklists:closing'),
    ),
)
def test_today_route_at_time_boundaries(
    client,
    stage_setup,
    monkeypatch,
    moment,
    expected_url,
):
    monkeypatch.setattr('django.utils.timezone.now', lambda: moment)
    client.force_login(stage_setup['employee'].user)

    response = client.get(reverse('checklists:today'))

    assert response.status_code == 302
    assert response.url == reverse(expected_url)


def test_overdue_stage_page_is_editable_and_has_server_timer(
    client,
    stage_setup,
    monkeypatch,
):
    monkeypatch.setattr('django.utils.timezone.now', lambda: at(12))
    client.force_login(stage_setup['employee'].user)

    response = client.get(reverse('checklists:opening'))
    content = response.content.decode()

    assert response.status_code == 200
    assert 'Этап просрочен' in content
    assert 'name="action"' in content
    assert 'data-server-now=' in content
    assert 'data-deadline-at=' in content
    assert 'deadline_timer.js' in content


@pytest.mark.django_db(transaction=True)
def test_data_migration_preserves_completed_history_without_invented_times():
    executor = MigrationExecutor(connection)
    executor.migrate([('checklists', '0002_remove_checklisttemplateversion_one_published_version_per_template')])
    old_apps = executor.loader.project_state(
        [('checklists', '0002_remove_checklisttemplateversion_one_published_version_per_template')]
    ).apps
    OldStore = old_apps.get_model('checklists', 'Store')
    OldEmployee = old_apps.get_model('checklists', 'EmployeeProfile')
    OldTemplate = old_apps.get_model('checklists', 'ChecklistTemplate')
    OldVersion = old_apps.get_model('checklists', 'ChecklistTemplateVersion')
    OldDaily = old_apps.get_model('checklists', 'DailyChecklist')
    OldItem = old_apps.get_model('checklists', 'DailyChecklistItem')
    OldAnswer = old_apps.get_model('checklists', 'ChecklistAnswer')

    user = User.objects.create_user(username='migration-user')
    store = OldStore.objects.create(
        name='Исторический магазин',
        code='history-store',
        timezone='Europe/Moscow',
    )
    employee = OldEmployee.objects.create(user_id=user.pk, store=store)
    template = OldTemplate.objects.create(store=store, name='История')
    version = OldVersion.objects.create(
        template=template,
        version_number=1,
        status='published',
        published_at=at(1),
    )
    completed_at = at(22)
    daily = OldDaily.objects.create(
        store=store,
        employee=employee,
        checklist_date=CHECKLIST_DATE,
        template_version=version,
        status='completed',
        completed_at=completed_at,
    )
    item = OldItem.objects.create(
        daily_checklist=daily,
        section_code='opening',
        section_name='Открытие',
        section_sort_order=1,
        item_text='Исторический пункт',
        item_sort_order=1,
    )
    answer = OldAnswer.objects.create(
        daily_item=item,
        status='failed',
        comment='Исторический комментарий',
        answered_by_id=user.pk,
        answered_at=at(21),
    )

    executor = MigrationExecutor(connection)
    executor.migrate([('checklists', '0003_dailycheckliststage_alter_dailychecklistitem_options_and_more')])
    new_apps = executor.loader.project_state(
        [('checklists', '0003_dailycheckliststage_alter_dailychecklistitem_options_and_more')]
    ).apps
    NewStage = new_apps.get_model('checklists', 'DailyChecklistStage')
    NewItem = new_apps.get_model('checklists', 'DailyChecklistItem')
    NewAnswer = new_apps.get_model('checklists', 'ChecklistAnswer')

    stages = NewStage.objects.filter(daily_checklist_id=daily.pk)
    assert stages.count() == 3
    assert set(stages.values_list('status', flat=True)) == {'completed'}
    assert set(stages.values_list('completed_at', flat=True)) == {completed_at}
    assert NewItem.objects.get(pk=item.pk).display_order > 0
    migrated_answer = NewAnswer.objects.get(pk=answer.pk)
    assert migrated_answer.status == 'failed'
    assert migrated_answer.comment == 'Исторический комментарий'
    executor = MigrationExecutor(connection)
    leaf_nodes = executor.loader.graph.leaf_nodes()
    executor.migrate(leaf_nodes)
    latest_apps = executor.loader.project_state(leaf_nodes).apps
    LatestStoreEmployee = latest_apps.get_model(
        'checklists',
        'StoreEmployee',
    )
    LatestAnswer = latest_apps.get_model('checklists', 'ChecklistAnswer')

    migrated_employee = LatestStoreEmployee.objects.get(
        store_id=store.pk,
        display_name='migration-user',
    )
    migrated_answer = LatestAnswer.objects.get(pk=answer.pk)
    assert migrated_answer.status == 'failed'
    assert migrated_answer.comment == 'Исторический комментарий'
    assert migrated_answer.answered_by_employee_id == migrated_employee.pk
    assert migrated_answer.last_edited_by_employee_id == migrated_employee.pk
