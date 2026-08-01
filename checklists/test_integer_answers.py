from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import Client
from django.urls import reverse

from checklists.exceptions import AnswerValidationError, ChecklistCompletionError
from checklists.management_services import (
    get_current_questions,
    update_checklist_question,
)
from checklists.models import (
    AnswerRevision,
    ChecklistAnswer,
    ChecklistItem,
    ChecklistSection,
    ChecklistTemplate,
    ChecklistTemplateVersion,
    DailyChecklistItem,
    DailyChecklistStage,
    EmployeeProfile,
    Store,
    StoreEmployee,
    StoreTerminalAccount,
)
from checklists.services import (
    complete_checklist_stage,
    create_daily_checklist,
    publish_template_version,
    update_answer,
)
from checklists.views import SELECTED_EMPLOYEE_SESSION_KEY


pytestmark = pytest.mark.django_db
WORK_DATE = date(2026, 7, 16)
NOW = datetime(2026, 7, 16, 10, 0, tzinfo=ZoneInfo('Europe/Moscow'))
QUESTION = 'Сколько заказов находится в статусе „Готов к отгрузке“?'


def make_published_template(store, actor, *, integer=True):
    template = ChecklistTemplate.objects.create(store=store, name='Основной')
    version = ChecklistTemplateVersion.objects.create(
        template=template,
        version_number=1,
        created_by=actor,
    )
    for order, (code, name) in enumerate(
        (('opening', 'Утро'), ('during_day', 'День'), ('closing', 'Вечер')),
        start=1,
    ):
        section = ChecklistSection.objects.create(
            version=version,
            code=code,
            name=name,
            sort_order=order,
        )
        if code == 'opening':
            ChecklistItem.objects.create(
                section=section,
                text=QUESTION,
                description='Введите текущее количество заказов',
                sort_order=1,
                answer_type=(
                    ChecklistItem.AnswerType.INTEGER
                    if integer
                    else ChecklistItem.AnswerType.STATUS
                ),
                is_required=True,
            )
    publish_template_version(version, actor)
    return version


@pytest.fixture
def integer_setup(monkeypatch):
    monkeypatch.setattr('django.utils.timezone.now', lambda: NOW)
    store = Store.objects.create(
        name='5 Планет',
        code='5',
        timezone='Europe/Moscow',
    )
    director = User.objects.create_user('integer-director', password='Safe-934!')
    EmployeeProfile.objects.create(
        user=director,
        store=store,
        role=EmployeeProfile.Role.STORE_DIRECTOR,
    )
    terminal_user = User.objects.create_user(
        'integer-terminal', password='Safe-934!'
    )
    EmployeeProfile.objects.create(
        user=terminal_user,
        store=store,
        role=EmployeeProfile.Role.STORE_ACCOUNT,
    )
    terminal = StoreTerminalAccount.objects.create(
        store=store,
        user=terminal_user,
    )
    employee = StoreEmployee.objects.create(
        store=store,
        first_name='Анна',
        display_name='Анна',
    )
    version = make_published_template(store, director)
    daily = create_daily_checklist(terminal, WORK_DATE)
    return {
        'store': store,
        'director': director,
        'terminal_user': terminal_user,
        'terminal': terminal,
        'employee': employee,
        'version': version,
        'daily': daily,
        'answer': daily.items.get(section_code='opening').answer,
    }


def login_terminal(client, setup):
    client.force_login(setup['terminal_user'])
    session = client.session
    session[SELECTED_EMPLOYEE_SESSION_KEY] = setup['employee'].pk
    session.save()


def test_default_type_and_integer_snapshot(integer_setup):
    section = integer_setup['version'].sections.get(code='closing')
    draft = ChecklistTemplateVersion.objects.create(
        template=integer_setup['version'].template,
        version_number=2,
        created_by=integer_setup['director'],
    )
    draft_section = ChecklistSection.objects.create(
        version=draft,
        code=section.code,
        name=section.name,
        sort_order=section.sort_order,
    )
    old_style = ChecklistItem.objects.create(
        section=draft_section,
        text='Обычный вопрос',
    )

    assert old_style.answer_type == ChecklistItem.AnswerType.STATUS
    assert (
        integer_setup['answer'].daily_item.answer_type_snapshot
        == ChecklistItem.AnswerType.INTEGER
    )
    assert integer_setup['answer'].status is None


def test_integer_page_uses_number_input_without_status_buttons(client, integer_setup):
    login_terminal(client, integer_setup)
    response = client.get(reverse('checklists:opening'))
    content = response.content.decode()

    assert response.status_code == 200
    assert 'type="number"' in content
    assert 'min="0"' in content
    assert 'step="1"' in content
    assert 'inputmode="numeric"' in content
    assert 'Укажите количество' in content
    assert 'Выполнено' not in content
    assert 'Не выполнено' not in content
    assert 'Не применяется' not in content


@pytest.mark.parametrize('value', [0, 12])
def test_integer_value_can_be_saved(client, integer_setup, value):
    login_terminal(client, integer_setup)
    answer = integer_setup['answer']
    response = client.post(
        reverse('checklists:opening'),
        {
            f'answer_{answer.pk}_integer_value': str(value),
            'action': 'save',
        },
    )

    answer.refresh_from_db()
    assert response.status_code == 302
    assert answer.integer_value == value
    assert answer.status is None


@pytest.mark.parametrize('value', ['-1', '1.5'])
def test_invalid_integer_is_rejected_by_web_form(client, integer_setup, value):
    login_terminal(client, integer_setup)
    answer = integer_setup['answer']
    response = client.post(
        reverse('checklists:opening'),
        {f'answer_{answer.pk}_integer_value': value, 'action': 'save'},
    )

    answer.refresh_from_db()
    assert response.status_code == 200
    assert answer.integer_value is None


def test_required_integer_can_remain_empty_in_draft(client, integer_setup):
    login_terminal(client, integer_setup)
    answer = integer_setup['answer']
    response = client.post(
        reverse('checklists:opening'),
        {f'answer_{answer.pk}_integer_value': '', 'action': 'save'},
    )

    answer.refresh_from_db()
    assert response.status_code == 302
    assert answer.integer_value is None


@pytest.mark.parametrize('value', [-1, Decimal('1.5'), '12'])
def test_model_rejects_negative_fraction_and_string(integer_setup, value):
    answer = integer_setup['answer']
    answer.integer_value = value
    with pytest.raises(ValidationError):
        answer.full_clean()


def test_answer_types_reject_incompatible_payloads(integer_setup):
    answer = integer_setup['answer']
    with pytest.raises(AnswerValidationError, match='статус'):
        update_answer(
            answer,
            ChecklistAnswer.Status.COMPLETED,
            '',
            integer_setup['terminal_user'],
            employee=integer_setup['employee'],
            integer_value=3,
        )

    status_item = DailyChecklistItem.objects.create(
        daily_checklist=integer_setup['daily'],
        section_code='opening',
        section_name='Утро',
        section_sort_order=1,
        item_text='Статусный вопрос',
        item_sort_order=2,
        display_order=2,
        answer_type_snapshot=ChecklistItem.AnswerType.STATUS,
    )
    status_answer = ChecklistAnswer.objects.create(daily_item=status_item)
    with pytest.raises(AnswerValidationError, match='статусного'):
        update_answer(
            status_answer,
            ChecklistAnswer.Status.COMPLETED,
            '',
            integer_setup['terminal_user'],
            employee=integer_setup['employee'],
            integer_value=3,
        )


def test_required_integer_blocks_completion_and_zero_allows_it(integer_setup):
    stage = integer_setup['daily'].stages.get(section_code='opening')
    with pytest.raises(ChecklistCompletionError):
        complete_checklist_stage(
            stage,
            integer_setup['terminal_user'],
            employee=integer_setup['employee'],
            at=NOW,
        )

    update_answer(
        integer_setup['answer'],
        None,
        '',
        integer_setup['terminal_user'],
        employee=integer_setup['employee'],
        integer_value=0,
        at=NOW,
    )
    complete_checklist_stage(
        stage,
        integer_setup['terminal_user'],
        employee=integer_setup['employee'],
        at=NOW + timedelta(minutes=1),
    )
    stage.refresh_from_db()
    assert stage.status == DailyChecklistStage.Status.COMPLETED


def test_integer_change_creates_complete_revision(integer_setup):
    answer = integer_setup['answer']
    update_answer(
        answer,
        None,
        '',
        integer_setup['terminal_user'],
        employee=integer_setup['employee'],
        integer_value=4,
        at=NOW,
    )
    update_answer(
        answer,
        None,
        '',
        integer_setup['terminal_user'],
        employee=integer_setup['employee'],
        integer_value=7,
        change_reason='Уточнили данные',
        at=NOW + timedelta(minutes=1),
    )

    revision = AnswerRevision.objects.get(answer=answer)
    assert revision.daily_item == answer.daily_item
    assert revision.previous_integer_value == 4
    assert revision.new_integer_value == 7
    assert revision.changed_by_user == integer_setup['terminal_user']
    assert revision.changed_by_employee == integer_setup['employee']


def test_old_daily_keeps_integer_type_after_template_type_change(integer_setup):
    question = get_current_questions(integer_setup['store']).get(
        section__code='opening'
    )
    update_checklist_question(
        integer_setup['store'],
        question,
        {
            'text': question.text,
            'description': question.description,
            'section_code': 'opening',
            'is_required': True,
            'answer_type': ChecklistItem.AnswerType.STATUS,
            'allow_not_applicable': False,
            'comment_required_on_failure': True,
            'sort_order': question.sort_order,
            'is_active': True,
            'effective_from': None,
            'effective_until': None,
        },
        integer_setup['director'],
    )

    integer_setup['answer'].daily_item.refresh_from_db()
    assert (
        integer_setup['answer'].daily_item.answer_type_snapshot
        == ChecklistItem.AnswerType.INTEGER
    )


def test_director_detail_shows_integer_and_revision(client, integer_setup):
    answer = integer_setup['answer']
    update_answer(
        answer,
        None,
        '',
        integer_setup['terminal_user'],
        employee=integer_setup['employee'],
        integer_value=12,
        at=NOW,
    )
    client.force_login(integer_setup['director'])
    response = client.get(
        reverse(
            'checklists:director_checklist_detail',
            args=[integer_setup['daily'].pk],
        )
    )
    content = response.content.decode()
    assert response.status_code == 200
    assert 'Ответ: <strong>12</strong>' in content
    assert 'Анна' in content


def test_post_without_csrf_is_rejected(integer_setup):
    client = Client(enforce_csrf_checks=True)
    login_terminal(client, integer_setup)
    answer = integer_setup['answer']
    response = client.post(
        reverse('checklists:opening'),
        {f'answer_{answer.pk}_integer_value': '9', 'action': 'save'},
    )
    answer.refresh_from_db()
    assert response.status_code == 403
    assert answer.integer_value is None


def test_foreign_store_cannot_substitute_answer_id(client, integer_setup):
    foreign_store = Store.objects.create(name='Чужой', code='foreign-integer')
    foreign_director = User.objects.create_user('foreign-director')
    EmployeeProfile.objects.create(
        user=foreign_director,
        store=foreign_store,
        role=EmployeeProfile.Role.STORE_DIRECTOR,
    )
    foreign_user = User.objects.create_user('foreign-account')
    foreign_profile = EmployeeProfile.objects.create(
        user=foreign_user,
        store=foreign_store,
        role=EmployeeProfile.Role.STORE_ACCOUNT,
    )
    make_published_template(foreign_store, foreign_director)
    foreign_daily = create_daily_checklist(foreign_profile, WORK_DATE)
    foreign_answer = foreign_daily.items.get(section_code='opening').answer
    own_answer = integer_setup['answer']
    login_terminal(client, integer_setup)

    response = client.post(
        reverse('checklists:opening'),
        {
            f'answer_{own_answer.pk}_integer_value': '3',
            f'answer_{foreign_answer.pk}_integer_value': '99',
            'action': 'save',
        },
    )

    own_answer.refresh_from_db()
    foreign_answer.refresh_from_db()
    assert response.status_code == 200
    assert 'чужого чек-листа' in response.content.decode()
    assert own_answer.integer_value is None
    assert foreign_answer.integer_value is None


def test_seed_command_creates_four_questions_once(integer_setup):
    # Replace the fixture's one numeric question with a status-only current
    # version so the command must add all four requested questions.
    store = Store.objects.create(name='Seed store', code='seed-5')
    actor = User.objects.create_user('seed-director')
    EmployeeProfile.objects.create(
        user=actor,
        store=store,
        role=EmployeeProfile.Role.STORE_DIRECTOR,
    )
    make_published_template(store, actor, integer=False)

    call_command('seed_order_count_questions', store_code='seed-5')
    call_command('seed_order_count_questions', store_code='seed-5')

    questions = ChecklistItem.objects.filter(
        section__version__template__store=store,
        section__version__status=ChecklistTemplateVersion.Status.PUBLISHED,
        answer_type=ChecklistItem.AnswerType.INTEGER,
    )
    assert questions.count() == 4
    assert set(questions.values_list('section__code', flat=True)) == {
        'opening',
        'closing',
    }
    assert ChecklistTemplateVersion.objects.filter(
        template__store=store,
        status=ChecklistTemplateVersion.Status.PUBLISHED,
    ).count() == 1
