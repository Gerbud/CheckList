from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.urls import reverse
from django.utils import timezone

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
    complete_checklist_stage,
    complete_daily_checklist,
    create_daily_checklist,
    publish_template_version,
    update_answer,
)


pytestmark = pytest.mark.django_db
WEB_NOW = datetime(2026, 7, 16, 10, 0, tzinfo=ZoneInfo('Europe/Moscow'))


def store_today(store):
    return timezone.now().astimezone(ZoneInfo(store.timezone)).date()


def create_profile(store, username, role=EmployeeProfile.Role.EMPLOYEE):
    user = User.objects.create_user(username=username, password='Safe-Test-934!')
    return EmployeeProfile.objects.create(user=user, store=store, role=role)


@pytest.fixture
def web_setup(monkeypatch):
    monkeypatch.setattr('django.utils.timezone.now', lambda: WEB_NOW)
    store = Store.objects.create(
        name='Мобильный магазин',
        code='mobile-store',
        timezone='Europe/Moscow',
    )
    manager = create_profile(store, 'web-manager', EmployeeProfile.Role.MANAGER)
    employee = create_profile(store, 'web-employee')
    template = ChecklistTemplate.objects.create(store=store, name='Основной')
    version = ChecklistTemplateVersion.objects.create(
        template=template,
        version_number=1,
        created_by=manager.user,
    )
    section = ChecklistSection.objects.create(
        version=version,
        name='Открытие магазина',
        code='opening',
        sort_order=1,
    )
    ChecklistItem.objects.create(
        section=section,
        text='Проверить готовность магазина',
        sort_order=1,
        allow_not_applicable=False,
    )
    publish_template_version(version, manager.user)
    return {'store': store, 'manager': manager, 'employee': employee}


def open_daily(client, setup):
    client.force_login(setup['employee'].user)
    client.get(reverse('checklists:today'))
    response = client.get(reverse('checklists:opening'))
    daily = DailyChecklist.objects.get(employee=setup['employee'])
    return response, daily


def complete_all_stages(daily, actor):
    for stage in daily.stages.order_by('opens_at'):
        complete_checklist_stage(
            stage,
            actor,
            at=stage.completion_available_at + timedelta(minutes=1),
        )
    daily.refresh_from_db()
    return daily


def answer_post_data(answer, *, status=None, comment='', action='save'):
    return {
        f'answer_{answer.pk}_status': status or answer.status,
        f'answer_{answer.pk}_comment': comment,
        'action': action,
    }


def test_anonymous_user_is_redirected_to_login(client):
    response = client.get(reverse('checklists:dashboard'))

    assert response.status_code == 302
    assert response.url == f"{reverse('login')}?next=/"


def test_user_without_active_profile_sees_profile_error(client):
    user = User.objects.create_user(username='without-profile', password='Safe-934!')
    client.force_login(user)

    response = client.get(reverse('checklists:dashboard'))

    assert response.status_code == 403
    assert 'Для пользователя не настроен профиль сотрудника' in response.content.decode()


def test_header_has_checklist_and_price_tag_buttons_and_warns_at_deadline(
    client,
    web_setup,
):
    schedule = StoreChecklistSchedule.objects.create(
        store=web_setup['store'],
        warning_minutes_before=90,
    )
    _, daily = open_daily(client, web_setup)

    response = client.get(reverse('checklists:dashboard'))
    content = response.content.decode()

    assert 'id="header-checklist-button"' in content
    assert '>Чек-лист</a>' in content
    assert '>Ценники</a>' in content
    assert (
        'class="btn btn-warning fw-semibold header-checklist-urgent"'
        in content
    )

    daily.stages.filter(
        section_code=DailyChecklistStage.SectionCode.OPENING,
    ).update(status=DailyChecklistStage.Status.COMPLETED, completed_at=WEB_NOW)
    completed_content = client.get(
        reverse('checklists:dashboard'),
    ).content.decode()
    assert (
        'class="btn btn-warning fw-semibold header-checklist-urgent"'
        not in completed_content
    )


def test_employee_page_contains_only_own_checklist(client, web_setup):
    _, own_daily = open_daily(client, web_setup)
    other = create_profile(web_setup['store'], 'other-employee')
    other_daily = create_daily_checklist(other, store_today(web_setup['store']))

    response = client.get(reverse('checklists:opening'))
    content = response.content.decode()

    assert f'answer_{own_daily.items.get().answer.pk}_status' in content
    assert f'answer_{other_daily.items.get().answer.pk}_status' not in content


def test_today_checklist_is_created_only_once(client, web_setup):
    client.force_login(web_setup['employee'].user)

    first = client.get(reverse('checklists:today'))
    second = client.get(reverse('checklists:today'))

    assert first.status_code == 302
    assert second.status_code == 302
    assert DailyChecklist.objects.filter(employee=web_setup['employee']).count() == 1


def test_intermediate_save_completed_answer(client, web_setup):
    _, daily = open_daily(client, web_setup)
    answer = daily.items.get().answer

    response = client.post(
        reverse('checklists:opening'),
        answer_post_data(answer, status=ChecklistAnswer.Status.COMPLETED),
    )

    answer.refresh_from_db()
    daily.refresh_from_db()
    assert response.status_code == 302
    assert answer.status == ChecklistAnswer.Status.COMPLETED
    assert daily.status == DailyChecklist.Status.DRAFT


def test_intermediate_save_failed_with_comment(client, web_setup):
    _, daily = open_daily(client, web_setup)
    answer = daily.items.get().answer

    response = client.post(
        reverse('checklists:opening'),
        answer_post_data(
            answer,
            status=ChecklistAnswer.Status.FAILED,
            comment='Не работает освещение',
        ),
    )

    answer.refresh_from_db()
    assert response.status_code == 302
    assert answer.status == ChecklistAnswer.Status.FAILED
    assert answer.comment == 'Не работает освещение'


def test_failed_without_comment_is_rejected(client, web_setup):
    _, daily = open_daily(client, web_setup)
    answer = daily.items.get().answer

    response = client.post(
        reverse('checklists:opening'),
        answer_post_data(answer, status=ChecklistAnswer.Status.FAILED),
    )

    answer.refresh_from_db()
    assert response.status_code == 200
    assert 'Для невыполненного пункта обязателен комментарий' in response.content.decode()
    assert answer.status == ChecklistAnswer.Status.PENDING


def test_not_applicable_is_rejected_by_form(client, web_setup):
    _, daily = open_daily(client, web_setup)
    answer = daily.items.get().answer

    response = client.post(
        reverse('checklists:opening'),
        answer_post_data(answer, status=ChecklistAnswer.Status.NOT_APPLICABLE),
    )

    answer.refresh_from_db()
    assert response.status_code == 200
    assert 'Для этого пункта нельзя выбрать «Не применимо»' in response.content.decode()
    assert answer.status == ChecklistAnswer.Status.PENDING


def test_intermediate_save_allows_pending(client, web_setup):
    _, daily = open_daily(client, web_setup)
    answer = daily.items.get().answer

    response = client.post(
        reverse('checklists:opening'),
        answer_post_data(answer, status=ChecklistAnswer.Status.PENDING),
    )

    answer.refresh_from_db()
    assert response.status_code == 302
    assert answer.status == ChecklistAnswer.Status.PENDING


def test_completion_with_pending_is_rejected(client, web_setup):
    _, daily = open_daily(client, web_setup)
    answer = daily.items.get().answer

    response = client.post(
        reverse('checklists:opening'),
        answer_post_data(
            answer,
            status=ChecklistAnswer.Status.PENDING,
            action='complete_stage',
        ),
    )

    daily.refresh_from_db()
    assert response.status_code == 200
    assert 'Чтобы завершить чек-лист, ответьте на все пункты' in response.content.decode()
    assert daily.status == DailyChecklist.Status.DRAFT


def test_complete_action_finishes_stage(client, web_setup):
    _, daily = open_daily(client, web_setup)
    answer = daily.items.get().answer

    response = client.post(
        reverse('checklists:opening'),
        answer_post_data(
            answer,
            status=ChecklistAnswer.Status.COMPLETED,
            action='complete_stage',
        ),
    )

    daily.refresh_from_db()
    assert response.status_code == 302
    assert daily.status == DailyChecklist.Status.DRAFT
    stage = daily.stages.get(section_code=DailyChecklistStage.SectionCode.OPENING)
    assert stage.status in {
        DailyChecklistStage.Status.COMPLETED,
        DailyChecklistStage.Status.COMPLETED_LATE,
    }
    assert stage.completed_at is not None


def test_completed_checklist_is_read_only(client, web_setup):
    _, daily = open_daily(client, web_setup)
    answer = daily.items.get().answer
    update_answer(answer, ChecklistAnswer.Status.COMPLETED, '', web_setup['employee'].user)
    complete_all_stages(daily, web_setup['employee'].user)

    get_response = client.get(reverse('checklists:opening'))
    post_response = client.post(
        reverse('checklists:opening'),
        answer_post_data(answer, status=ChecklistAnswer.Status.FAILED, comment='Нет'),
    )

    answer.refresh_from_db()
    assert get_response.status_code == 200
    assert 'Этап завершён' in get_response.content.decode()
    assert 'name="action"' not in get_response.content.decode()
    assert post_response.status_code == 403
    assert answer.status == ChecklistAnswer.Status.COMPLETED


def test_foreign_answer_id_in_post_is_rejected(client, web_setup):
    _, own_daily = open_daily(client, web_setup)
    own_answer = own_daily.items.get().answer
    other = create_profile(web_setup['store'], 'foreign-employee')
    other_daily = create_daily_checklist(other, store_today(web_setup['store']))
    foreign_answer = other_daily.items.get().answer
    data = answer_post_data(own_answer, status=ChecklistAnswer.Status.COMPLETED)
    data[f'answer_{foreign_answer.pk}_status'] = ChecklistAnswer.Status.COMPLETED

    response = client.post(reverse('checklists:opening'), data)

    own_answer.refresh_from_db()
    foreign_answer.refresh_from_db()
    assert response.status_code == 200
    assert 'Форма содержит пункт чужого чек-листа' in response.content.decode()
    assert own_answer.status == ChecklistAnswer.Status.PENDING
    assert foreign_answer.status == ChecklistAnswer.Status.PENDING


def test_answer_update_writes_audit_ip_and_user_agent(client, web_setup):
    _, daily = open_daily(client, web_setup)
    answer = daily.items.get().answer

    response = client.post(
        reverse('checklists:opening'),
        answer_post_data(answer, status=ChecklistAnswer.Status.COMPLETED),
        REMOTE_ADDR='192.0.2.25',
        HTTP_USER_AGENT='StoreChecklistTest/1.0',
    )

    log = AuditLog.objects.get(
        action=AuditLog.Action.ANSWER_STATUS_CHANGED,
        object_id=str(answer.pk),
    )
    assert response.status_code == 302
    assert log.ip_address == '192.0.2.25'
    assert log.user_agent == 'StoreChecklistTest/1.0'


def test_seed_demo_users_is_idempotent(monkeypatch):
    call_command('seed_checklist')
    monkeypatch.setenv('DEMO_MANAGER_PASSWORD', 'River!Glass9-Orbit#732')
    monkeypatch.setenv('DEMO_EMPLOYEE_PASSWORD', 'Copper!Sky8-Anchor#541')

    call_command('seed_demo_users')
    call_command('seed_demo_users')

    assert User.objects.filter(username='manager').count() == 1
    assert User.objects.filter(username='employee').count() == 1
    assert EmployeeProfile.objects.filter(user__username='manager').count() == 1
    assert EmployeeProfile.objects.filter(user__username='employee').count() == 1
    assert User.objects.get(username='manager').check_password(
        'River!Glass9-Orbit#732'
    )


def test_seed_demo_users_requires_password_environment(monkeypatch):
    call_command('seed_checklist')
    monkeypatch.delenv('DEMO_MANAGER_PASSWORD', raising=False)
    monkeypatch.delenv('DEMO_EMPLOYEE_PASSWORD', raising=False)

    with pytest.raises(CommandError, match='DEMO_MANAGER_PASSWORD'):
        call_command('seed_demo_users')

    assert not User.objects.filter(username__in=('manager', 'employee')).exists()


def test_daily_interface_contains_three_seeded_sections(client):
    call_command('seed_checklist')
    store = Store.objects.get(code='5-planets')
    employee = create_profile(store, 'seeded-web-employee')
    client.force_login(employee.user)

    client.get(reverse('checklists:today'))
    response = client.get(reverse('checklists:dashboard'))
    content = response.content.decode()

    assert response.status_code == 200
    assert 'Утренние задачи' in content
    assert 'Дневные задачи' in content
    assert 'Вечерние задачи' in content


def test_mobile_interface_uses_large_status_buttons(client, web_setup):
    response, _ = open_daily(client, web_setup)
    content = response.content.decode()

    assert 'btn-check' in content
    assert 'status-button' in content
    assert 'Выполнено' in content
    assert 'Не выполнено' in content
    assert 'Не применимо' not in content


def test_login_redirects_to_dashboard(client, web_setup):
    response = client.post(
        reverse('login'),
        {'username': 'web-employee', 'password': 'Safe-Test-934!'},
    )

    assert response.status_code == 302
    assert response.url == reverse('checklists:terminal_home')
