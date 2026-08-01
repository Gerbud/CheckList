from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, transaction
from django.test import Client
from django.urls import reverse

from checklists.exceptions import (
    AnswerValidationError,
    ChecklistLockedError,
    OperationNotAllowedError,
)
from checklists.models import (
    AnswerRevision,
    AuditLog,
    ChecklistAnswer,
    ChecklistItem,
    ChecklistSection,
    ChecklistTemplate,
    ChecklistTemplateVersion,
    DailyChecklistStage,
    DailyShiftAssignment,
    EmployeeProfile,
    Store,
    StoreEmployee,
    StoreTerminalAccount,
)
from checklists.services import (
    complete_checklist_stage,
    create_daily_checklist,
    get_missing_employee_actions,
    get_shift_completion_report,
    publish_template_version,
    reopen_daily_checklist,
    update_answer,
)
from checklists.views import SELECTED_EMPLOYEE_SESSION_KEY


pytestmark = pytest.mark.django_db
MOSCOW = ZoneInfo('Europe/Moscow')
WORK_DATE = date(2026, 7, 16)
NOW = datetime(2026, 7, 16, 10, 0, tzinfo=MOSCOW)


def make_template(store, manager):
    template = ChecklistTemplate.objects.create(
        store=store,
        name='Терминальный чек-лист',
    )
    version = ChecklistTemplateVersion.objects.create(
        template=template,
        version_number=1,
        created_by=manager,
    )
    for sort_order, (code, name, item_count) in enumerate(
        (
            ('opening', 'Открытие', 2),
            ('during_day', 'День', 1),
            ('closing', 'Закрытие', 1),
        ),
        start=1,
    ):
        section = ChecklistSection.objects.create(
            version=version,
            code=code,
            name=name,
            sort_order=sort_order,
        )
        for item_number in range(item_count):
            ChecklistItem.objects.create(
                section=section,
                text=f'{name}: пункт {item_number + 1}',
                sort_order=item_number,
            )
    publish_template_version(version, manager)


@pytest.fixture
def terminal_setup(monkeypatch):
    monkeypatch.setattr('django.utils.timezone.now', lambda: NOW)
    store = Store.objects.create(
        name='Магазин терминала',
        code='terminal-store',
        timezone='Europe/Moscow',
    )
    manager_user = User.objects.create_user(
        username='terminal-manager',
        password='Safe-Test-934!',
    )
    manager = EmployeeProfile.objects.create(
        user=manager_user,
        store=store,
        role=EmployeeProfile.Role.MANAGER,
    )
    make_template(store, manager_user)
    terminal_user = User.objects.create_user(
        username='store-terminal',
        password='Safe-Test-934!',
    )
    terminal_profile = EmployeeProfile.objects.create(
        user=terminal_user,
        store=store,
        role=EmployeeProfile.Role.STORE_ACCOUNT,
    )
    terminal = StoreTerminalAccount.objects.create(
        store=store,
        user=terminal_user,
    )
    alice = StoreEmployee.objects.create(
        store=store,
        first_name='Алиса',
        last_name='Иванова',
        display_name='Алиса Иванова',
        personnel_number='A-001',
        sort_order=10,
    )
    bob = StoreEmployee.objects.create(
        store=store,
        first_name='Борис',
        last_name='Петров',
        display_name='Борис Петров',
        personnel_number='B-002',
        sort_order=20,
    )
    inactive = StoreEmployee.objects.create(
        store=store,
        first_name='Неактивный',
        display_name='Неактивный сотрудник',
        is_active=False,
    )
    other_store = Store.objects.create(
        name='Чужой магазин',
        code='other-terminal-store',
    )
    outsider = StoreEmployee.objects.create(
        store=other_store,
        first_name='Чужой',
        display_name='Чужой сотрудник',
    )
    DailyShiftAssignment.objects.create(
        store=store,
        employee=alice,
        work_date=WORK_DATE,
        is_responsible_for_checklist=True,
        created_by=manager_user,
    )
    DailyShiftAssignment.objects.create(
        store=store,
        employee=bob,
        work_date=WORK_DATE,
        is_responsible_for_checklist=False,
        created_by=manager_user,
    )
    daily = create_daily_checklist(terminal, WORK_DATE)
    return {
        'store': store,
        'manager': manager,
        'terminal': terminal,
        'terminal_profile': terminal_profile,
        'alice': alice,
        'bob': bob,
        'inactive': inactive,
        'outsider': outsider,
        'daily': daily,
    }


def opening_answers(setup):
    return list(
        ChecklistAnswer.objects.filter(
            daily_item__daily_checklist=setup['daily'],
            daily_item__section_code='opening',
        ).order_by('pk')
    )


def select_employee(client, employee, next_url=None):
    payload = {'employee_id': employee.pk}
    if next_url:
        payload['next'] = next_url
    return client.post(reverse('checklists:select_employee'), payload)


def test_terminal_is_technical_account_and_store_employee_has_exact_fields(
    terminal_setup,
):
    terminal = terminal_setup['terminal']
    field_names = {field.name for field in StoreEmployee._meta.fields}

    assert EmployeeProfile.objects.get(user=terminal.user).role == (
        EmployeeProfile.Role.STORE_ACCOUNT
    )
    assert not terminal.user.is_staff
    assert not terminal.user.is_superuser
    assert set(field_names) == {
        'id',
        'store',
        'first_name',
        'last_name',
        'display_name',
        'position',
        'department',
        'personnel_number',
        'user',
        'is_active',
        'sort_order',
        'created_at',
        'updated_at',
    }
    with pytest.raises(ValidationError, match='Активный терминал'):
        StoreTerminalAccount.objects.create(
            user=terminal_setup['manager'].user,
            store=Store.objects.create(
                name='Магазин для проверки',
                code='terminal-role-check',
            ),
        )


def test_employee_selection_lists_only_active_employees_of_terminal_store(
    client,
    terminal_setup,
):
    client.force_login(terminal_setup['terminal'].user)

    response = client.get(reverse('checklists:select_employee'))
    content = response.content.decode().lower()

    assert response.status_code == 200
    assert terminal_setup['alice'].display_name.lower() in content
    assert terminal_setup['bob'].display_name.lower() in content
    assert terminal_setup['inactive'].display_name.lower() not in content
    assert terminal_setup['outsider'].display_name.lower() not in content
    assert 'csrfmiddlewaretoken' in content


def test_selection_is_stored_in_session_and_can_be_changed(
    client,
    terminal_setup,
):
    client.force_login(terminal_setup['terminal'].user)

    response = select_employee(client, terminal_setup['alice'])
    assert response.status_code == 302
    assert (
        client.session[SELECTED_EMPLOYEE_SESSION_KEY]
        == terminal_setup['alice'].pk
    )

    response = client.post(
        reverse('checklists:change_employee'),
        {'next': reverse('checklists:opening')},
    )
    assert response.status_code == 302
    assert SELECTED_EMPLOYEE_SESSION_KEY not in client.session

    select_employee(client, terminal_setup['bob'])
    response = client.get(reverse('checklists:opening'))
    assert 'Сейчас заполняет: Борис Петров' in response.content.decode()


def test_employee_selection_requires_post_with_csrf(terminal_setup):
    csrf_client = Client(enforce_csrf_checks=True)
    csrf_client.force_login(terminal_setup['terminal'].user)
    page = csrf_client.get(reverse('checklists:select_employee'))
    csrf_token = page.cookies['csrftoken'].value

    rejected = csrf_client.post(
        reverse('checklists:select_employee'),
        {'employee_id': terminal_setup['alice'].pk},
    )
    accepted = csrf_client.post(
        reverse('checklists:select_employee'),
        {
            'employee_id': terminal_setup['alice'].pk,
            'csrfmiddlewaretoken': csrf_token,
        },
    )

    assert rejected.status_code == 403
    assert accepted.status_code == 302
    assert (
        csrf_client.session[SELECTED_EMPLOYEE_SESSION_KEY]
        == terminal_setup['alice'].pk
    )


@pytest.mark.parametrize('employee_key', ('inactive', 'outsider'))
def test_inactive_or_foreign_employee_cannot_be_selected(
    client,
    terminal_setup,
    employee_key,
):
    client.force_login(terminal_setup['terminal'].user)

    response = select_employee(client, terminal_setup[employee_key])

    assert response.status_code == 403
    assert SELECTED_EMPLOYEE_SESSION_KEY not in client.session


def test_individual_account_cannot_use_terminal_employee_selection(
    client,
    terminal_setup,
):
    client.force_login(terminal_setup['manager'].user)

    assert client.get(reverse('checklists:select_employee')).status_code == 403
    assert (
        client.post(
            reverse('checklists:change_employee'),
            {'next': reverse('checklists:dashboard')},
        ).status_code
        == 403
    )


def test_terminal_stage_requires_employee_selection(client, terminal_setup):
    client.force_login(terminal_setup['terminal'].user)

    get_response = client.get(reverse('checklists:opening'))
    post_response = client.post(
        reverse('checklists:opening'),
        {'section_code': 'opening', 'action': 'save'},
    )

    assert get_response.status_code == 302
    assert reverse('checklists:select_employee') in get_response.url
    assert post_response.status_code == 403


def test_employee_id_substitution_in_stage_post_is_forbidden(
    client,
    terminal_setup,
):
    client.force_login(terminal_setup['terminal'].user)
    select_employee(client, terminal_setup['alice'])
    answer = opening_answers(terminal_setup)[0]

    response = client.post(
        reverse('checklists:opening'),
        {
            'employee_id': terminal_setup['bob'].pk,
            'section_code': 'opening',
            'action': 'save',
            f'answer_{answer.pk}_status': ChecklistAnswer.Status.COMPLETED,
            f'answer_{answer.pk}_comment': '',
        },
    )

    answer.refresh_from_db()
    assert response.status_code == 403
    assert answer.status == ChecklistAnswer.Status.PENDING


def test_service_rejects_missing_inactive_and_foreign_terminal_employee(
    terminal_setup,
):
    answer = opening_answers(terminal_setup)[0]
    actor = terminal_setup['terminal'].user

    with pytest.raises(OperationNotAllowedError, match='выберите'):
        update_answer(
            answer,
            ChecklistAnswer.Status.COMPLETED,
            '',
            actor,
            at=NOW,
        )
    for employee in (terminal_setup['inactive'], terminal_setup['outsider']):
        with pytest.raises(OperationNotAllowedError, match='другому магазину'):
            update_answer(
                answer,
                ChecklistAnswer.Status.COMPLETED,
                '',
                actor,
                employee=employee,
                at=NOW,
            )


def test_selected_employee_is_saved_in_answer_stage_and_audit(
    client,
    terminal_setup,
):
    client.force_login(terminal_setup['terminal'].user)
    select_employee(client, terminal_setup['alice'])
    answer = opening_answers(terminal_setup)[0]

    response = client.post(
        reverse('checklists:opening'),
        {
            'section_code': 'opening',
            'action': 'save',
            f'answer_{answer.pk}_status': ChecklistAnswer.Status.COMPLETED,
            f'answer_{answer.pk}_comment': '',
        },
        REMOTE_ADDR='192.0.2.81',
        HTTP_USER_AGENT='TerminalTest/1.0',
    )

    answer.refresh_from_db()
    stage = terminal_setup['daily'].stages.get(section_code='opening')
    log = AuditLog.objects.get(
        action=AuditLog.Action.ANSWER_STATUS_CHANGED,
        object_id=str(answer.pk),
    )
    assert response.status_code == 302
    assert answer.answered_by == terminal_setup['terminal'].user
    assert answer.answered_by_employee == terminal_setup['alice']
    assert answer.last_edited_by_employee == terminal_setup['alice']
    assert stage.last_edited_by_employee == terminal_setup['alice']
    assert log.actor == terminal_setup['terminal'].user
    assert log.employee == terminal_setup['alice']
    assert log.ip_address == '192.0.2.81'


def test_different_answers_in_one_stage_can_have_different_employees(
    terminal_setup,
):
    first, second = opening_answers(terminal_setup)
    actor = terminal_setup['terminal'].user

    update_answer(
        first,
        ChecklistAnswer.Status.COMPLETED,
        '',
        actor,
        employee=terminal_setup['alice'],
        at=NOW,
    )
    update_answer(
        second,
        ChecklistAnswer.Status.COMPLETED,
        '',
        actor,
        employee=terminal_setup['bob'],
        at=NOW,
    )

    first.refresh_from_db()
    second.refresh_from_db()
    assert first.answered_by_employee == terminal_setup['alice']
    assert second.answered_by_employee == terminal_setup['bob']


def test_first_answer_needs_no_reason_but_saved_answer_change_does(
    terminal_setup,
):
    answer = opening_answers(terminal_setup)[0]
    actor = terminal_setup['terminal'].user
    update_answer(
        answer,
        ChecklistAnswer.Status.COMPLETED,
        '',
        actor,
        employee=terminal_setup['alice'],
        at=NOW,
    )

    for reason in (None, 'мало'):
        with pytest.raises(AnswerValidationError, match='5 символов'):
            update_answer(
                answer,
                ChecklistAnswer.Status.FAILED,
                'Найдена проблема',
                actor,
                employee=terminal_setup['bob'],
                change_reason=reason,
                at=NOW + timedelta(minutes=2),
            )
    assert not AnswerRevision.objects.exists()


def test_saved_answer_change_creates_immutable_revision_and_audit(
    terminal_setup,
):
    answer = opening_answers(terminal_setup)[0]
    actor = terminal_setup['terminal'].user
    update_answer(
        answer,
        ChecklistAnswer.Status.COMPLETED,
        'Исходный комментарий',
        actor,
        employee=terminal_setup['alice'],
        at=NOW,
    )

    update_answer(
        answer,
        ChecklistAnswer.Status.FAILED,
        'Обнаружен дефект',
        actor,
        employee=terminal_setup['bob'],
        change_reason='Повторная проверка',
        request_metadata={
            'ip_address': '192.0.2.90',
            'user_agent': 'SensitiveTerminalAgent/9.0',
        },
        at=NOW + timedelta(minutes=3),
    )

    revision = AnswerRevision.objects.get(answer=answer)
    answer.refresh_from_db()
    assert revision.previous_status == ChecklistAnswer.Status.COMPLETED
    assert revision.new_status == ChecklistAnswer.Status.FAILED
    assert revision.previous_comment == 'Исходный комментарий'
    assert revision.new_comment == 'Обнаружен дефект'
    assert revision.change_reason == 'Повторная проверка'
    assert revision.changed_by_user == actor
    assert revision.changed_by_employee == terminal_setup['bob']
    assert answer.answered_by_employee == terminal_setup['alice']
    assert answer.last_edited_by_employee == terminal_setup['bob']
    assert AuditLog.objects.filter(
        action=AuditLog.Action.ANSWER_REVISED,
        employee=terminal_setup['bob'],
        actor=actor,
    ).exists()

    revision.change_reason = 'Подмена'
    with pytest.raises(ValidationError, match='нельзя изменять'):
        revision.save()
    with pytest.raises(ValidationError, match='нельзя удалить'):
        revision.delete()


def test_changed_answer_page_shows_safe_history_only(client, terminal_setup):
    client.force_login(terminal_setup['terminal'].user)
    select_employee(client, terminal_setup['alice'])
    answer = opening_answers(terminal_setup)[0]
    update_answer(
        answer,
        ChecklistAnswer.Status.COMPLETED,
        'Всё в порядке',
        terminal_setup['terminal'].user,
        employee=terminal_setup['alice'],
        at=NOW,
    )
    update_answer(
        answer,
        ChecklistAnswer.Status.FAILED,
        'Найден дефект',
        terminal_setup['terminal'].user,
        employee=terminal_setup['bob'],
        change_reason='Нужна коррекция',
        request_metadata={
            'ip_address': '192.0.2.99',
            'user_agent': 'PrivateUserAgent/8.0',
        },
        at=NOW + timedelta(minutes=5),
    )

    response = client.get(reverse('checklists:opening'))
    content = response.content.decode()

    assert response.status_code == 200
    assert 'Ответ изменён' in content
    assert 'История изменений' in content
    assert 'Нужна коррекция' in content
    assert terminal_setup['bob'].display_name in content
    assert '192.0.2.99' not in content
    assert 'PrivateUserAgent/8.0' not in content


def test_stage_completion_records_employee_and_clears_selection(
    client,
    terminal_setup,
):
    client.force_login(terminal_setup['terminal'].user)
    select_employee(client, terminal_setup['alice'])
    answers = opening_answers(terminal_setup)
    payload = {'section_code': 'opening', 'action': 'complete_stage'}
    for answer in answers:
        payload[f'answer_{answer.pk}_status'] = ChecklistAnswer.Status.COMPLETED
        payload[f'answer_{answer.pk}_comment'] = ''

    response = client.post(reverse('checklists:opening'), payload)

    stage = terminal_setup['daily'].stages.get(section_code='opening')
    assert response.status_code == 302
    assert stage.completed_by_employee == terminal_setup['alice']
    assert stage.first_completed_by_employee == terminal_setup['alice']
    assert stage.last_edited_by_employee == terminal_setup['alice']
    assert SELECTED_EMPLOYEE_SESSION_KEY not in client.session
    assert AuditLog.objects.filter(
        action=AuditLog.Action.CHECKLIST_STAGE_COMPLETED,
        object_id=str(stage.pk),
        employee=terminal_setup['alice'],
    ).exists()


def test_completed_stage_requires_manager_reopen_and_preserves_first_completion(
    terminal_setup,
):
    answers = opening_answers(terminal_setup)
    actor = terminal_setup['terminal'].user
    for answer in answers:
        update_answer(
            answer,
            ChecklistAnswer.Status.COMPLETED,
            '',
            actor,
            employee=terminal_setup['alice'],
            at=NOW,
        )
    stage = terminal_setup['daily'].stages.get(section_code='opening')
    complete_checklist_stage(
        stage,
        actor,
        employee=terminal_setup['alice'],
        at=NOW + timedelta(minutes=10),
    )

    with pytest.raises(ChecklistLockedError, match='только для чтения'):
        update_answer(
            answers[0],
            ChecklistAnswer.Status.FAILED,
            'Дефект',
            actor,
            employee=terminal_setup['bob'],
            change_reason='Перепроверка',
            at=NOW + timedelta(minutes=15),
        )

    reopen_daily_checklist(
        terminal_setup['daily'],
        terminal_setup['manager'].user,
        section_code='opening',
        at=NOW + timedelta(minutes=20),
    )
    update_answer(
        answers[0],
        ChecklistAnswer.Status.FAILED,
        'Дефект',
        actor,
        employee=terminal_setup['bob'],
        change_reason='Перепроверка',
        at=NOW + timedelta(minutes=25),
    )
    complete_checklist_stage(
        stage,
        actor,
        employee=terminal_setup['bob'],
        at=NOW + timedelta(minutes=30),
    )

    stage.refresh_from_db()
    assert stage.completed_by_employee == terminal_setup['bob']
    assert stage.first_completed_by_employee == terminal_setup['alice']
    assert stage.first_completed_at == NOW + timedelta(minutes=10)
    assert stage.reopened_count == 1
    assert AnswerRevision.objects.filter(
        answer=answers[0],
        changed_by_employee=terminal_setup['bob'],
    ).exists()


def test_shift_assignment_validation_uniqueness_and_completion_report(
    terminal_setup,
):
    with pytest.raises(ValidationError, match='неактивного'):
        DailyShiftAssignment.objects.create(
            store=terminal_setup['store'],
            employee=terminal_setup['inactive'],
            work_date=WORK_DATE,
        )
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            DailyShiftAssignment.objects.bulk_create(
                [
                    DailyShiftAssignment(
                        store=terminal_setup['store'],
                        employee=terminal_setup['alice'],
                        work_date=WORK_DATE,
                    )
                ]
            )

    answer = opening_answers(terminal_setup)[0]
    update_answer(
        answer,
        ChecklistAnswer.Status.COMPLETED,
        '',
        terminal_setup['terminal'].user,
        employee=terminal_setup['alice'],
        at=NOW,
    )
    update_answer(
        answer,
        ChecklistAnswer.Status.FAILED,
        'Найден дефект',
        terminal_setup['terminal'].user,
        employee=terminal_setup['alice'],
        change_reason='Повторная проверка',
        at=NOW + timedelta(minutes=1),
    )

    report = get_shift_completion_report(terminal_setup['store'], WORK_DATE)
    rows = {row['employee'].pk: row for row in report['employees']}
    missing = get_missing_employee_actions(terminal_setup['store'], WORK_DATE)

    assert rows[terminal_setup['alice'].pk]['opening_participated'] is True
    assert rows[terminal_setup['alice'].pk]['answers_filled'] == 1
    assert rows[terminal_setup['alice'].pk]['answers_changed'] == 1
    assert rows[terminal_setup['alice'].pk]['no_participation'] is False
    assert rows[terminal_setup['bob'].pk]['no_participation'] is True
    assert terminal_setup['bob'] in missing['employees_without_actions']
    assert terminal_setup['bob'] not in missing['responsible_without_participation']
    assert {'during_day', 'closing'} <= set(missing['stages_without_actions'])


def test_existing_individual_user_still_works_without_selected_employee(
    terminal_setup,
):
    individual_user = User.objects.create_user(
        username='legacy-individual',
        password='Safe-Test-934!',
    )
    individual = EmployeeProfile.objects.create(
        user=individual_user,
        store=terminal_setup['store'],
    )
    daily = create_daily_checklist(individual, WORK_DATE)
    answer = daily.items.filter(section_code='opening').first().answer

    update_answer(
        answer,
        ChecklistAnswer.Status.COMPLETED,
        '',
        individual_user,
        at=NOW,
    )

    answer.refresh_from_db()
    assert answer.answered_by == individual_user
    assert answer.answered_by_employee is None


def test_seed_store_terminal_is_idempotent_and_has_no_personal_credentials(
    monkeypatch,
    settings,
    capsys,
):
    settings.DEBUG = True
    Store.objects.create(
        name='5 Планет',
        code='5-planets',
        timezone='Europe/Moscow',
    )
    monkeypatch.setenv('STORE_TERMINAL_USERNAME', 'seeded-terminal')
    monkeypatch.setenv(
        'STORE_TERMINAL_PASSWORD',
        'Strong-Terminal-Password-934!',
    )

    call_command('seed_store_terminal', '--with-demo-employees')
    call_command('seed_store_terminal', '--with-demo-employees')
    output = capsys.readouterr().out.lower()

    terminal = StoreTerminalAccount.objects.get(
        user__username='seeded-terminal',
    )
    assert terminal.store.code == '5-planets'
    assert StoreTerminalAccount.objects.count() == 1
    assert StoreEmployee.objects.filter(store=terminal.store).count() == 3


def test_seed_store_terminal_requires_environment_credentials(monkeypatch):
    monkeypatch.delenv('STORE_TERMINAL_USERNAME', raising=False)
    monkeypatch.delenv('STORE_TERMINAL_PASSWORD', raising=False)

    with pytest.raises(CommandError, match='STORE_TERMINAL_USERNAME'):
        call_command('seed_store_terminal')
