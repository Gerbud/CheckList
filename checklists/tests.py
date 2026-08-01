from datetime import date, timedelta

import pytest
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.utils import timezone

from checklists.exceptions import (
    AnswerValidationError,
    ChecklistLockedError,
    DuplicateDailyChecklistError,
    OperationNotAllowedError,
)
from checklists.models import (
    AuditLog,
    ChecklistAnswer,
    ChecklistItem,
    ChecklistSection,
    ChecklistTemplate,
    ChecklistTemplateVersion,
    DailyChecklist,
    DailyChecklistItem,
    DailyChecklistStage,
    EmployeeProfile,
    Store,
    StoreChecklistSchedule,
)
from checklists.services import (
    complete_checklist_stage,
    complete_daily_checklist,
    create_daily_checklist,
    publish_template_version,
    reopen_daily_checklist,
    update_answer,
)


pytestmark = pytest.mark.django_db


@pytest.fixture
def store():
    return Store.objects.create(
        name='Тестовый магазин',
        code='test-store',
        timezone='Europe/Moscow',
    )


def create_profile(store, username, role=EmployeeProfile.Role.EMPLOYEE):
    user = User.objects.create_user(username=username, password='test-password')
    return EmployeeProfile.objects.create(user=user, store=store, role=role)


@pytest.fixture
def employee(store):
    return create_profile(store, 'employee')


@pytest.fixture
def manager(store):
    return create_profile(store, 'manager', EmployeeProfile.Role.MANAGER)


def create_version(
    store,
    actor,
    *,
    template=None,
    version_number=1,
    item_text='Исходный текст пункта',
    allow_not_applicable=False,
    publish=True,
):
    if template is None:
        template = ChecklistTemplate.objects.create(
            store=store,
            name='Ежедневный чек-лист',
        )
    version = ChecklistTemplateVersion.objects.create(
        template=template,
        version_number=version_number,
        created_by=actor,
    )
    section = ChecklistSection.objects.create(
        version=version,
        name='Открытие магазина',
        code='opening',
        sort_order=1,
    )
    ChecklistItem.objects.create(
        section=section,
        text=item_text,
        sort_order=1,
        comment_required_on_failure=True,
        allow_not_applicable=allow_not_applicable,
    )
    if publish:
        version = publish_template_version(version, actor)
    return template, version


@pytest.fixture
def published_version(store, manager):
    _, version = create_version(store, manager.user)
    return version


@pytest.fixture
def daily_checklist(employee, published_version):
    return create_daily_checklist(employee, date(2026, 7, 16))


def complete_all_stages(daily, actor):
    for stage in daily.stages.order_by('opens_at'):
        complete_checklist_stage(
            stage,
            actor,
            at=stage.completion_available_at + timedelta(minutes=1),
        )
    daily.refresh_from_db()
    return daily


def test_checklists_app_is_installed():
    from django.apps import apps

    assert apps.get_app_config('checklists').name == 'checklists'


def test_create_daily_checklist_from_published_version(
    employee,
    published_version,
):
    daily = create_daily_checklist(employee, date(2026, 7, 16))

    assert daily.template_version == published_version
    assert daily.status == DailyChecklist.Status.DRAFT
    assert daily.items.count() == 1
    assert daily.stages.count() == 3
    snapshot = daily.items.get()
    assert snapshot.item_text == 'Исходный текст пункта'
    assert snapshot.answer.status == ChecklistAnswer.Status.PENDING
    assert AuditLog.objects.filter(
        action=AuditLog.Action.DAILY_CHECKLIST_CREATED,
        object_id=str(daily.pk),
    ).exists()


def test_cannot_create_duplicate_daily_checklist(
    employee,
    published_version,
):
    checklist_date = date(2026, 7, 16)
    create_daily_checklist(employee, checklist_date)

    with pytest.raises(DuplicateDailyChecklistError):
        create_daily_checklist(employee, checklist_date)

    assert DailyChecklist.objects.count() == 1


def test_snapshot_text_survives_new_template_version(
    employee,
    manager,
    published_version,
):
    daily = create_daily_checklist(employee, date(2026, 7, 16))
    template = published_version.template
    create_version(
        employee.store,
        manager.user,
        template=template,
        version_number=2,
        item_text='Новый текст пункта',
    )

    daily.refresh_from_db()
    assert daily.items.get().item_text == 'Исходный текст пункта'
    assert daily.template_version_id == published_version.pk


def test_failed_answer_requires_comment(daily_checklist, employee):
    answer = daily_checklist.items.get().answer

    with pytest.raises(AnswerValidationError):
        update_answer(answer, ChecklistAnswer.Status.FAILED, '   ', employee.user)


def test_not_applicable_is_rejected_when_item_disallows_it(
    daily_checklist,
    employee,
):
    answer = daily_checklist.items.get().answer

    with pytest.raises(AnswerValidationError):
        update_answer(
            answer,
            ChecklistAnswer.Status.NOT_APPLICABLE,
            '',
            employee.user,
        )


def test_employee_cannot_change_answer_after_completion(
    daily_checklist,
    employee,
):
    answer = daily_checklist.items.get().answer
    update_answer(answer, ChecklistAnswer.Status.COMPLETED, '', employee.user)
    complete_all_stages(daily_checklist, employee.user)

    with pytest.raises(ChecklistLockedError):
        update_answer(answer, ChecklistAnswer.Status.FAILED, 'Ошибка', employee.user)


def test_manager_can_reopen_completed_checklist(
    daily_checklist,
    employee,
    manager,
):
    answer = daily_checklist.items.get().answer
    update_answer(answer, ChecklistAnswer.Status.COMPLETED, '', employee.user)
    completed = complete_all_stages(daily_checklist, employee.user)

    reopened = reopen_daily_checklist(completed, manager.user)

    assert reopened.status == DailyChecklist.Status.REOPENED
    assert reopened.reopened_by == manager.user
    assert reopened.reopened_at is not None


def test_employee_cannot_reopen_completed_checklist(
    daily_checklist,
    employee,
):
    answer = daily_checklist.items.get().answer
    update_answer(answer, ChecklistAnswer.Status.COMPLETED, '', employee.user)
    completed = complete_all_stages(daily_checklist, employee.user)

    with pytest.raises(OperationNotAllowedError):
        reopen_daily_checklist(completed, employee.user)


def test_audit_log_records_required_service_operations(
    daily_checklist,
    employee,
    manager,
    published_version,
):
    answer = daily_checklist.items.get().answer
    update_answer(answer, ChecklistAnswer.Status.COMPLETED, 'Готово', employee.user)
    completed = complete_all_stages(daily_checklist, employee.user)
    reopen_daily_checklist(completed, manager.user)
    create_version(
        employee.store,
        manager.user,
        template=published_version.template,
        version_number=2,
        item_text='Новая версия',
    )

    actions = set(AuditLog.objects.values_list('action', flat=True))
    assert {
        AuditLog.Action.DAILY_CHECKLIST_CREATED,
        AuditLog.Action.ANSWER_STATUS_CHANGED,
        AuditLog.Action.ANSWER_COMMENT_CHANGED,
        AuditLog.Action.DAILY_CHECKLIST_COMPLETED,
        AuditLog.Action.CHECKLIST_STAGE_COMPLETED,
        AuditLog.Action.DAILY_CHECKLIST_REOPENED,
        AuditLog.Action.TEMPLATE_VERSION_PUBLISHED,
    }.issubset(actions)


def test_template_publication_is_atomic(
    store,
    manager,
    published_version,
    monkeypatch,
):
    _, draft = create_version(
        store,
        manager.user,
        template=published_version.template,
        version_number=2,
        publish=False,
    )

    def fail_audit_write(*args, **kwargs):
        raise RuntimeError('audit storage unavailable')

    monkeypatch.setattr(AuditLog.objects, 'create', fail_audit_write)

    with pytest.raises(RuntimeError, match='audit storage unavailable'):
        publish_template_version(draft, manager.user)

    published_version.refresh_from_db()
    draft.refresh_from_db()
    assert published_version.status == ChecklistTemplateVersion.Status.PUBLISHED
    assert draft.status == ChecklistTemplateVersion.Status.DRAFT


def test_only_one_published_version_per_template(
    store,
    manager,
    published_version,
):
    _, second = create_version(
        store,
        manager.user,
        template=published_version.template,
        version_number=2,
        publish=False,
    )
    second.status = ChecklistTemplateVersion.Status.PUBLISHED
    second.published_at = timezone.now()

    with pytest.raises(ValidationError):
        second.save()

    assert ChecklistTemplateVersion.objects.filter(
        template=published_version.template,
        status=ChecklistTemplateVersion.Status.PUBLISHED,
    ).count() == 1


def test_published_version_cannot_be_edited_by_ordinary_save(published_version):
    published_version.version_number = 99

    with pytest.raises(ValidationError):
        published_version.save()


def test_daily_snapshot_is_immutable(daily_checklist):
    snapshot = daily_checklist.items.get()
    snapshot.item_text = 'Изменённый исторический текст'

    with pytest.raises(ValidationError):
        snapshot.save()


def test_seed_checklist_is_idempotent():
    call_command('seed_checklist')
    call_command('seed_checklist')

    assert Store.objects.filter(code='5-planets').count() == 1
    assert ChecklistTemplate.objects.filter(
        name='Ежедневный чек-лист сотрудника',
    ).count() == 1
    assert ChecklistTemplateVersion.objects.filter(
        version_number=1,
        status=ChecklistTemplateVersion.Status.PUBLISHED,
    ).count() == 1
    assert ChecklistSection.objects.count() == 3
    assert ChecklistItem.objects.count() == 18
    assert AuditLog.objects.filter(
        action=AuditLog.Action.TEMPLATE_VERSION_PUBLISHED,
    ).count() == 1
    schedule = StoreChecklistSchedule.objects.get(store__code='5-planets')
    assert str(schedule.opening_time) == '09:00:00'
    assert str(schedule.closing_deadline) == '22:00:00'


def test_seed_checklist_does_not_overwrite_admin_schedule():
    call_command('seed_checklist')
    schedule = StoreChecklistSchedule.objects.get(store__code='5-planets')
    schedule.warning_minutes_before = 45
    schedule.save()

    call_command('seed_checklist')

    schedule.refresh_from_db()
    assert schedule.warning_minutes_before == 45
