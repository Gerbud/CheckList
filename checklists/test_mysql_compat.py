from pathlib import Path

import pytest
from django.contrib.auth.models import User
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.db import models
from django.db.models.query import QuerySet
from django.utils import timezone

from checklists.models import (
    AuditLog,
    ChecklistItem,
    ChecklistNotification,
    ChecklistSection,
    ChecklistTemplate,
    ChecklistTemplateVersion,
    DailyChecklist,
    DailyShiftAssignment,
    EmployeeProfile,
    Store,
    StoreChecklistSchedule,
    StoreEmployee,
)
from checklists.services import publish_template_version
from config.database import MYSQL_REQUIRED_VARIABLES, build_database_config


pytestmark = pytest.mark.django_db


def create_manager_and_template():
    store = Store.objects.create(name='MySQL магазин', code='mysql-store')
    user = User.objects.create_user(username='mysql-manager', password='Safe-934!')
    EmployeeProfile.objects.create(
        user=user,
        store=store,
        role=EmployeeProfile.Role.MANAGER,
    )
    template = ChecklistTemplate.objects.create(store=store, name='MySQL шаблон')
    return store, user, template


def create_draft_version(template, user, number):
    version = ChecklistTemplateVersion.objects.create(
        template=template,
        version_number=number,
        created_by=user,
    )
    section = ChecklistSection.objects.create(
        version=version,
        name='Раздел',
        code=f'section-{number}',
        sort_order=1,
    )
    ChecklistItem.objects.create(
        section=section,
        text=f'Пункт версии {number}',
        sort_order=1,
    )
    return version


def test_mysql_database_config_requires_all_environment_variables():
    with pytest.raises(ImproperlyConfigured) as exc_info:
        build_database_config(Path('/tmp/project'), {'DATABASE_ENGINE': 'mysql'})

    message = str(exc_info.value)
    for variable in MYSQL_REQUIRED_VARIABLES:
        assert variable in message


def test_mysql_database_config_uses_utf8mb4_and_persistent_connections():
    config = build_database_config(
        Path('/tmp/project'),
        {
            'DATABASE_ENGINE': 'mysql',
            'MYSQL_DATABASE': 'store_checklist',
            'MYSQL_USER': 'store_user',
            'MYSQL_PASSWORD': 'secret',
            'MYSQL_HOST': 'mysql',
            'MYSQL_PORT': '3306',
            'MYSQL_TEST_DATABASE': 'store_checklist_test',
        },
    )['default']

    assert config['ENGINE'] == 'django.db.backends.mysql'
    assert config['OPTIONS']['charset'] == 'utf8mb4'
    assert config['OPTIONS']['init_command'] == "SET sql_mode='STRICT_TRANS_TABLES'"
    assert config['CONN_MAX_AGE'] == 60
    assert config['TEST']['NAME'] == 'store_checklist_test'


def test_schema_contains_only_portable_unique_constraints():
    version_constraints = ChecklistTemplateVersion._meta.constraints
    assert all(
        not isinstance(constraint, models.UniqueConstraint)
        or constraint.condition is None
        for constraint in version_constraints
    )
    assert 'one_published_version_per_template' not in {
        constraint.name for constraint in version_constraints
    }

    daily_constraint = next(
        constraint
        for constraint in DailyChecklist._meta.constraints
        if constraint.name == 'unique_employee_daily_checklist'
    )
    assert tuple(daily_constraint.fields) == (
        'store',
        'employee',
        'checklist_date',
    )
    assert daily_constraint.condition is None
    terminal_daily_constraint = next(
        constraint
        for constraint in DailyChecklist._meta.constraints
        if constraint.name == 'unique_terminal_daily_checklist'
    )
    assert tuple(terminal_daily_constraint.fields) == (
        'store',
        'terminal_account',
        'checklist_date',
    )
    assert terminal_daily_constraint.condition is None
    employee_number_constraint = next(
        constraint
        for constraint in StoreEmployee._meta.constraints
        if constraint.name == 'unique_store_personnel_number'
    )
    assert tuple(employee_number_constraint.fields) == (
        'store',
        'personnel_number',
    )
    assert employee_number_constraint.condition is None
    shift_constraint = next(
        constraint
        for constraint in DailyShiftAssignment._meta.constraints
        if constraint.name == 'unique_store_employee_work_date'
    )
    assert tuple(shift_constraint.fields) == (
        'store',
        'employee',
        'work_date',
    )
    assert shift_constraint.condition is None
    notification_constraint = next(
        constraint
        for constraint in ChecklistNotification._meta.constraints
        if constraint.name == 'unique_notification_type_stage'
    )
    assert notification_constraint.condition is None
    assert tuple(notification_constraint.fields) == ('stage', 'notification_type')

    assert {
        constraint.name for constraint in StoreChecklistSchedule._meta.constraints
    } == {
        'schedule_warning_minutes_positive',
        'schedule_morning_completion_window_valid',
        'schedule_day_completion_window_valid',
        'schedule_evening_completion_window_valid',
    }
    schedule_fields = {
        field.name for field in StoreChecklistSchedule._meta.fields
    }
    assert {
        'morning_completion_window_minutes',
        'day_completion_window_minutes',
        'evening_completion_window_minutes',
    } <= schedule_fields
    assert not {
        'morning_completion_window_hours',
        'day_completion_window_hours',
        'evening_completion_window_hours',
    } & schedule_fields


def test_publish_service_locks_template_and_versions_then_archives_previous(
    monkeypatch,
):
    _, user, template = create_manager_and_template()
    first = create_draft_version(template, user, 1)
    second = create_draft_version(template, user, 2)
    first = publish_template_version(first, user)
    locked_models = []
    original_select_for_update = QuerySet.select_for_update

    def tracked_select_for_update(queryset, *args, **kwargs):
        locked_models.append(queryset.model)
        return original_select_for_update(queryset, *args, **kwargs)

    monkeypatch.setattr(QuerySet, 'select_for_update', tracked_select_for_update)
    second = publish_template_version(second, user)

    first.refresh_from_db()
    assert ChecklistTemplate in locked_models
    assert ChecklistTemplateVersion in locked_models
    assert first.status == ChecklistTemplateVersion.Status.ARCHIVED
    assert second.status == ChecklistTemplateVersion.Status.PUBLISHED
    assert ChecklistTemplateVersion.objects.filter(
        template=template,
        status=ChecklistTemplateVersion.Status.PUBLISHED,
    ).count() == 1


def test_direct_publish_is_rejected_without_database_partial_index():
    _, user, template = create_manager_and_template()
    version = create_draft_version(template, user, 1)
    version.status = ChecklistTemplateVersion.Status.PUBLISHED
    version.published_at = timezone.now()

    with pytest.raises(ValidationError, match='сервисный слой'):
        version.save()


def test_json_and_nullable_audit_values_round_trip():
    store, user, _ = create_manager_and_template()
    log = AuditLog.objects.create(
        actor=user,
        store=store,
        object_type='checklists.test',
        object_id='mysql-json',
        action=AuditLog.Action.ANSWER_COMMENT_CHANGED,
        old_value=None,
        new_value={'текст': 'Юникод', 'flag': True, 'count': 1},
        ip_address=None,
        user_agent=None,
    )

    log.refresh_from_db()
    assert log.old_value is None
    assert log.new_value == {'текст': 'Юникод', 'flag': True, 'count': 1}
    assert log.ip_address is None


def test_indexed_character_fields_fit_mysql_utf8mb4_limits():
    indexed_fields = [
        Store._meta.get_field('code'),
        ChecklistSection._meta.get_field('code'),
        ChecklistNotification._meta.get_field('notification_type'),
        StoreEmployee._meta.get_field('personnel_number'),
    ]

    assert all(field.max_length <= 255 for field in indexed_fields)
