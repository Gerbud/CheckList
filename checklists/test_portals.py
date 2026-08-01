from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from django.contrib.auth.models import User
from django.contrib.sessions.models import Session
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from checklists.access_control import (
    get_portal_home_url,
    get_user_role,
    get_user_store,
    is_store_account,
    is_store_director,
    is_system_admin,
)
from checklists.management_services import (
    bulk_create_shift_assignments,
    create_managed_user,
    create_store_with_defaults,
    deactivate_managed_user,
    get_current_questions,
    get_store_deletion_summary,
    reorder_checklist_questions,
    reset_managed_user_password,
    update_checklist_question,
    update_managed_user,
    update_store_schedule,
)
from checklists.models import (
    AnswerRevision,
    AuditLog,
    ChecklistAnswer,
    ChecklistItem,
    ChecklistSection,
    ChecklistTemplate,
    ChecklistTemplateVersion,
    DailyChecklist,
    DailyChecklistStage,
    DailyShiftAssignment,
    EmployeeProfile,
    Store,
    StoreAdHocTask,
    StoreChecklistSchedule,
    StoreEmployee,
    StoreNotificationSettings,
    StoreTerminalAccount,
)
from checklists.services import (
    complete_checklist_stage,
    create_daily_checklist,
    publish_template_version,
    update_answer,
)


pytestmark = pytest.mark.django_db
MOSCOW = ZoneInfo('Europe/Moscow')
WORK_DATE = date(2026, 7, 16)
NOW = datetime(2026, 7, 16, 10, 0, tzinfo=MOSCOW)


def create_access_user(username, role, store=None, password='Strong-Test-934!'):
    user = User.objects.create_user(username=username, password=password)
    profile = EmployeeProfile.objects.create(
        user=user,
        role=role,
        store=store,
    )
    terminal = None
    if role == EmployeeProfile.Role.STORE_ACCOUNT:
        terminal = StoreTerminalAccount.objects.create(store=store, user=user)
    return user, profile, terminal


def make_template(store, actor):
    template = ChecklistTemplate.objects.create(store=store, name='Основной')
    version = ChecklistTemplateVersion.objects.create(
        template=template,
        version_number=1,
        created_by=actor,
    )
    for order, (code, name) in enumerate(
        (
            ('opening', 'Утро'),
            ('during_day', 'День'),
            ('closing', 'Вечер'),
        ),
        start=1,
    ):
        section = ChecklistSection.objects.create(
            version=version,
            code=code,
            name=name,
            sort_order=order,
        )
        ChecklistItem.objects.create(
            section=section,
            text=f'{name}: исходный вопрос',
            sort_order=1,
        )
    publish_template_version(version, actor)
    return version


@pytest.fixture
def portal_setup(monkeypatch):
    monkeypatch.setattr('django.utils.timezone.now', lambda: NOW)
    first = Store.objects.create(
        name='Первый магазин',
        code='director-first',
        timezone='Europe/Moscow',
    )
    second = Store.objects.create(
        name='Второй магазин',
        code='director-second',
        timezone='Europe/Moscow',
    )
    director, director_profile, _ = create_access_user(
        'first-director',
        EmployeeProfile.Role.STORE_DIRECTOR,
        first,
    )
    other_director, _, _ = create_access_user(
        'second-director',
        EmployeeProfile.Role.STORE_DIRECTOR,
        second,
    )
    admin, admin_profile, _ = create_access_user(
        'system-controller',
        EmployeeProfile.Role.SYSTEM_ADMIN,
    )
    terminal_user, terminal_profile, terminal = create_access_user(
        'first-terminal',
        EmployeeProfile.Role.STORE_ACCOUNT,
        first,
    )
    first_employee = StoreEmployee.objects.create(
        store=first,
        first_name='Алиса',
        last_name='Иванова',
        display_name='Алиса Иванова',
    )
    other_employee = StoreEmployee.objects.create(
        store=second,
        first_name='Борис',
        display_name='Борис из другого магазина',
    )
    make_template(first, director)
    daily = create_daily_checklist(terminal, WORK_DATE)
    return {
        'first': first,
        'second': second,
        'director': director,
        'director_profile': director_profile,
        'other_director': other_director,
        'admin': admin,
        'admin_profile': admin_profile,
        'terminal_user': terminal_user,
        'terminal_profile': terminal_profile,
        'terminal': terminal,
        'employee': first_employee,
        'other_employee': other_employee,
        'daily': daily,
    }


def test_access_roles_are_unified_and_store_employee_user_is_optional(portal_setup):
    assert get_user_role(portal_setup['terminal_user']) == EmployeeProfile.Role.STORE_ACCOUNT
    assert get_user_store(portal_setup['terminal_user']) == portal_setup['first']
    assert is_store_account(portal_setup['terminal_user'])
    assert is_store_director(portal_setup['director'])
    assert is_system_admin(portal_setup['admin'])
    user_field = StoreEmployee._meta.get_field('user')
    assert user_field.null is True
    assert user_field.blank is True
    assert set(EmployeeProfile.Role.values) == {
        'store_account',
        'store_director',
        'system_admin',
    }


def test_invalid_role_store_combinations_are_rejected(portal_setup):
    user_one = User.objects.create_user(username='director-without-store')
    with pytest.raises(ValidationError, match='магазин обязателен'):
        EmployeeProfile.objects.create(
            user=user_one,
            role=EmployeeProfile.Role.STORE_DIRECTOR,
            store=None,
        )
    user_two = User.objects.create_user(username='admin-with-store')
    with pytest.raises(ValidationError, match='не привязывается'):
        EmployeeProfile.objects.create(
            user=user_two,
            role=EmployeeProfile.Role.SYSTEM_ADMIN,
            store=portal_setup['first'],
        )


def test_login_redirect_depends_on_role_and_external_next_is_ignored(
    client,
    portal_setup,
):
    cases = (
        ('first-terminal', '/terminal/'),
        ('first-director', '/director/dashboard/'),
        ('system-controller', '/system-admin/dashboard/'),
    )
    for username, expected in cases:
        response = client.post(
            reverse('login'),
            {
                'username': username,
                'password': 'Strong-Test-934!',
                'next': 'https://attacker.invalid/steal',
            },
        )
        assert response.status_code == 302
        assert response.url == expected
        client.post(reverse('logout'))


def test_portal_role_boundaries(client, portal_setup):
    client.force_login(portal_setup['terminal_user'])
    assert client.get(reverse('checklists:director_dashboard')).status_code == 403
    assert client.get(reverse('checklists:system_admin_dashboard')).status_code == 403

    client.force_login(portal_setup['director'])
    assert client.get(reverse('checklists:director_dashboard')).status_code == 200
    assert client.get(reverse('checklists:system_admin_dashboard')).status_code == 403

    client.force_login(portal_setup['admin'])
    assert client.get(reverse('checklists:system_admin_dashboard')).status_code == 200
    assert client.get(reverse('checklists:director_dashboard')).status_code == 403
    selected = client.post(
        reverse('checklists:system_store_open_director', args=[portal_setup['first'].pk])
    )
    assert selected.status_code == 302
    assert client.get(reverse('checklists:director_dashboard')).status_code == 200


def test_inactive_profile_and_inactive_store_have_no_portal_access(
    client,
    portal_setup,
):
    portal_setup['director_profile'].is_active = False
    portal_setup['director_profile'].save()
    client.force_login(portal_setup['director'])
    assert client.get(reverse('checklists:director_dashboard')).status_code == 403

    portal_setup['director_profile'].is_active = True
    portal_setup['director_profile'].save()
    portal_setup['first'].is_active = False
    portal_setup['first'].save()
    assert client.get(reverse('checklists:director_dashboard')).status_code == 403


def test_director_employee_management_is_store_scoped(client, portal_setup):
    client.force_login(portal_setup['director'])
    assert client.get(
        reverse('checklists:director_employee_edit', args=[portal_setup['other_employee'].pk])
    ).status_code == 404

    response = client.post(
        reverse('checklists:director_employee_add'),
        {
            'first_name': 'Мария',
            'last_name': 'Смирнова',
            'display_name': 'Мария Смирнова',
            'personnel_number': 'M-9',
            'sort_order': 30,
        },
    )
    created = StoreEmployee.objects.get(display_name='Мария Смирнова')
    assert response.status_code == 302
    assert created.store == portal_setup['first']
    assert not hasattr(created, 'password')
    assert AuditLog.objects.filter(
        action=AuditLog.Action.STORE_EMPLOYEE_CREATED,
        object_id=str(created.pk),
    ).exists()


def test_employee_activation_requires_post_and_historical_employee_remains(
    client,
    portal_setup,
):
    answer = portal_setup['daily'].items.first().answer
    update_answer(
        answer,
        ChecklistAnswer.Status.COMPLETED,
        '',
        portal_setup['terminal_user'],
        employee=portal_setup['employee'],
        at=NOW,
    )
    client.force_login(portal_setup['director'])
    url = reverse(
        'checklists:director_employee_deactivate',
        args=[portal_setup['employee'].pk],
    )
    assert client.get(url).status_code == 403
    assert client.post(url).status_code == 302
    portal_setup['employee'].refresh_from_db()
    assert portal_setup['employee'].is_active is False
    assert StoreEmployee.objects.filter(pk=portal_setup['employee'].pk).exists()


def test_shift_creation_and_bulk_planning_do_not_duplicate(client, portal_setup):
    client.force_login(portal_setup['director'])
    response = client.post(
        reverse('checklists:director_shift_add', args=[WORK_DATE.isoformat()]),
        {
            'employee': portal_setup['employee'].pk,
            'is_responsible_for_checklist': 'on',
            'shift_start': '09:00',
            'shift_end': '18:00',
            'comment': 'Основная смена',
        },
    )
    assert response.status_code == 302
    assert DailyShiftAssignment.objects.filter(
        store=portal_setup['first'],
        employee=portal_setup['employee'],
        work_date=WORK_DATE,
    ).count() == 1

    data = {
        'start_date': WORK_DATE,
        'end_date': WORK_DATE + timedelta(days=2),
        'weekdays': [str((WORK_DATE + timedelta(days=i)).weekday()) for i in range(3)],
        'employees': [portal_setup['employee']],
        'shift_start': time(9),
        'shift_end': time(18),
        'is_responsible_for_checklist': True,
        'comment': '',
        'mode': 'create',
    }
    first = bulk_create_shift_assignments(portal_setup['first'], data, portal_setup['director'])
    second = bulk_create_shift_assignments(portal_setup['first'], data, portal_setup['director'])
    assert first['created'] == 2
    assert first['skipped'] == 1
    assert second['created'] == 0
    assert second['skipped'] == 3


def test_director_cannot_assign_employee_from_another_store(client, portal_setup):
    client.force_login(portal_setup['director'])
    response = client.post(
        reverse('checklists:director_shift_add', args=[WORK_DATE.isoformat()]),
        {
            'employee': portal_setup['other_employee'].pk,
            'shift_start': '09:00',
            'shift_end': '18:00',
        },
    )
    assert response.status_code == 200
    assert not DailyShiftAssignment.objects.filter(
        store=portal_setup['first'],
        employee=portal_setup['other_employee'],
    ).exists()


def test_question_update_creates_version_and_keeps_existing_snapshot(portal_setup):
    old_item = get_current_questions(portal_setup['first']).get(section__code='opening')
    snapshot = portal_setup['daily'].items.get(section_code='opening')
    data = {
        'text': 'Новый текст вопроса',
        'description': 'Инструкция',
        'section_code': 'opening',
        'is_required': True,
        'allow_not_applicable': True,
        'comment_required_on_failure': True,
        'sort_order': 7,
        'is_active': True,
        'effective_from': None,
        'effective_until': None,
    }
    new_item = update_checklist_question(
        portal_setup['first'],
        old_item,
        data,
        portal_setup['director'],
    )
    snapshot.refresh_from_db()
    assert snapshot.item_text == 'Утро: исходный вопрос'
    assert new_item.text == 'Новый текст вопроса'
    assert new_item.section.version.version_number == 2
    assert ChecklistTemplateVersion.objects.filter(
        template=new_item.section.version.template,
        status=ChecklistTemplateVersion.Status.PUBLISHED,
    ).count() == 1


def test_reorder_rejects_duplicate_and_foreign_ids(portal_setup):
    ids = list(
        get_current_questions(portal_setup['first'])
        .filter(section__code='opening')
        .values_list('pk', flat=True)
    )
    with pytest.raises(ValidationError, match='дубли'):
        reorder_checklist_questions(
            portal_setup['first'],
            'opening',
            [ids[0], ids[0]],
            portal_setup['director'],
        )
    with pytest.raises(ValidationError, match='не совпадает'):
        reorder_checklist_questions(
            portal_setup['first'],
            'opening',
            [portal_setup['other_employee'].pk],
            portal_setup['director'],
        )


def test_schedule_update_preserves_existing_stages_and_changes_new_daily(portal_setup):
    old_stage = portal_setup['daily'].stages.get(section_code='opening')
    old_bounds = (old_stage.opens_at, old_stage.deadline_at)
    update_store_schedule(
        portal_setup['first'],
        {
            'opening_time': time(8),
            'morning_deadline': time(10),
            'daytime_deadline': time(19),
            'closing_deadline': time(21),
            'warning_minutes_before': 30,
            'notifications_enabled': True,
            'is_active': True,
        },
        portal_setup['director'],
    )
    old_stage.refresh_from_db()
    new_daily = create_daily_checklist(
        portal_setup['terminal'],
        WORK_DATE + timedelta(days=1),
    )
    new_stage = new_daily.stages.get(section_code='opening')
    assert (old_stage.opens_at, old_stage.deadline_at) == old_bounds
    assert new_stage.opens_at.astimezone(MOSCOW).time() == time(8)
    assert new_stage.deadline_at.astimezone(MOSCOW).time() == time(10)


def test_invalid_schedule_is_rejected_in_director_form(client, portal_setup):
    client.force_login(portal_setup['director'])
    response = client.post(
        reverse('checklists:director_schedule'),
        {
            'opening_time': '12:00',
            'morning_deadline': '10:00',
            'daytime_deadline': '19:00',
            'closing_deadline': '21:00',
            'warning_minutes_before': 30,
            'notifications_enabled': 'on',
            'is_active': 'on',
        },
    )
    assert response.status_code == 200
    assert 'строго по порядку' in response.content.decode()


def test_notification_settings_hide_token_and_test_send_is_explicit_mock(
    client,
    portal_setup,
    monkeypatch,
    settings,
):
    settings.TELEGRAM_BOT_TOKEN = 'never-render-this-secret'
    StoreNotificationSettings.objects.create(
        store=portal_setup['first'],
        telegram_chat_id='-100555',
        is_active=True,
    )
    sent = []
    monkeypatch.setattr(
        'checklists.management_services.send_telegram_message',
        lambda chat_id, text: sent.append((chat_id, text)) or 42,
    )
    client.force_login(portal_setup['director'])
    page = client.get(reverse('checklists:director_notifications'))
    assert 'never-render-this-secret' not in page.content.decode()
    assert client.get(reverse('checklists:director_notification_test')).status_code == 403
    response = client.post(
        reverse('checklists:director_notification_test'),
        {'confirm': 'on'},
    )
    assert response.status_code == 302
    assert sent and sent[0][0] == '-100555'
    log = AuditLog.objects.get(action=AuditLog.Action.TELEGRAM_TEST_MESSAGE_SENT)
    assert 'never-render-this-secret' not in str(log.new_value)


def test_director_report_and_checklist_are_store_scoped(client, portal_setup):
    client.force_login(portal_setup['director'])
    assert client.get(reverse('checklists:director_report_daily')).status_code == 200
    assert client.get(
        reverse('checklists:director_checklist_detail', args=[portal_setup['daily'].pk])
    ).status_code == 200
    other_version = make_template(
        portal_setup['second'],
        portal_setup['other_director'],
    )
    other_daily = DailyChecklist.objects.create(
        store=portal_setup['second'],
        employee=EmployeeProfile.objects.get(user=portal_setup['other_director']),
        checklist_date=WORK_DATE,
        template_version=other_version,
    )
    assert client.get(
        reverse('checklists:director_checklist_detail', args=[other_daily.pk])
    ).status_code == 404


def test_reopen_requires_reason_and_preserves_boundaries(client, portal_setup):
    answer = portal_setup['daily'].items.get(section_code='opening').answer
    update_answer(
        answer,
        ChecklistAnswer.Status.COMPLETED,
        '',
        portal_setup['terminal_user'],
        employee=portal_setup['employee'],
        at=NOW,
    )
    stage = portal_setup['daily'].stages.get(section_code='opening')
    complete_checklist_stage(
        stage,
        portal_setup['terminal_user'],
        employee=portal_setup['employee'],
        at=NOW + timedelta(minutes=5),
    )
    bounds = (stage.opens_at, stage.deadline_at)
    client.force_login(portal_setup['director'])
    url = reverse(
        'checklists:director_stage_reopen',
        args=[portal_setup['daily'].pk, 'opening'],
    )
    assert client.post(url, {'reason': 'мало'}).status_code == 302
    stage.refresh_from_db()
    assert stage.reopened_count == 0
    client.post(url, {'reason': 'Нужна повторная проверка'})
    stage.refresh_from_db()
    assert stage.reopened_count == 1
    assert (stage.opens_at, stage.deadline_at) == bounds
    assert AuditLog.objects.filter(
        action=AuditLog.Action.CHECKLIST_STAGE_REOPENED,
        new_value__reason='Нужна повторная проверка',
    ).exists()


def test_system_admin_creates_store_with_defaults(portal_setup):
    store = create_store_with_defaults(
        actor=portal_setup['admin'],
        name='Новый магазин',
        code='created-by-admin',
        timezone_name='Europe/Moscow',
    )
    assert hasattr(store, 'checklist_schedule')
    assert hasattr(store, 'notification_settings')
    assert store.notification_settings.is_active is False
    assert not StoreTerminalAccount.objects.filter(store=store).exists()


def test_managed_users_enforce_roles_and_single_store_account(portal_setup):
    director = create_managed_user(
        actor=portal_setup['admin'],
        username='new-director',
        password='Strong-New-Director-934!',
        role=EmployeeProfile.Role.STORE_DIRECTOR,
        store=portal_setup['second'],
    )
    assert director.employee_profile.store == portal_setup['second']
    with pytest.raises(ValidationError, match='аккаунт'):
        create_managed_user(
            actor=portal_setup['admin'],
            username='second-terminal',
            password='Strong-Second-Terminal-934!',
            role=EmployeeProfile.Role.STORE_ACCOUNT,
            store=portal_setup['first'],
        )
    assert not User.objects.filter(username='second-terminal').exists()


def test_password_reset_is_hashed_audited_without_secret(portal_setup):
    raw_password = 'Temporary-Secret-Password-934!'
    reset_managed_user_password(
        portal_setup['director'],
        raw_password,
        portal_setup['admin'],
    )
    portal_setup['director'].refresh_from_db()
    assert portal_setup['director'].check_password(raw_password)
    log = AuditLog.objects.get(action=AuditLog.Action.USER_PASSWORD_RESET)
    assert raw_password not in str(log.old_value)
    assert raw_password not in str(log.new_value)


def test_last_system_admin_and_self_cannot_be_deactivated(portal_setup):
    with pytest.raises(Exception, match='самого себя'):
        deactivate_managed_user(portal_setup['admin'], portal_setup['admin'])
    other_admin, _, _ = create_access_user(
        'other-system-admin',
        EmployeeProfile.Role.SYSTEM_ADMIN,
    )
    deactivate_managed_user(other_admin, portal_setup['admin'])
    other_admin.refresh_from_db()
    assert other_admin.is_active is False


def test_csrf_is_required_for_director_mutation(portal_setup):
    csrf_client = Client(enforce_csrf_checks=True)
    csrf_client.force_login(portal_setup['director'])
    url = reverse(
        'checklists:director_employee_deactivate',
        args=[portal_setup['employee'].pk],
    )
    assert csrf_client.post(url).status_code == 403
    assert StoreEmployee.objects.get(pk=portal_setup['employee'].pk).is_active


def test_director_question_create_and_deactivate_use_new_versions(
    client,
    portal_setup,
):
    client.force_login(portal_setup['director'])
    response = client.post(
        reverse('checklists:director_question_add'),
        {
            'text': 'Новый контрольный вопрос',
            'description': 'Проверить по инструкции',
            'section_code': 'during_day',
            'is_required': 'on',
            'comment_required_on_failure': 'on',
            'sort_order': 5,
            'is_active': 'on',
        },
    )
    assert response.status_code == 302
    question = get_current_questions(portal_setup['first']).get(
        text='Новый контрольный вопрос'
    )
    response = client.post(
        reverse('checklists:director_question_deactivate', args=[question.pk])
    )
    assert response.status_code == 302
    assert get_current_questions(portal_setup['first']).get(
        text='Новый контрольный вопрос'
    ).is_active is False
    assert AuditLog.objects.filter(
        action=AuditLog.Action.CHECKLIST_QUESTION_DEACTIVATED
    ).exists()


def test_director_updates_notification_settings_without_token(
    client,
    portal_setup,
    settings,
):
    settings.TELEGRAM_BOT_TOKEN = 'environment-secret-must-not-leak'
    client.force_login(portal_setup['director'])
    response = client.post(
        reverse('checklists:director_notifications'),
        {
            'telegram_chat_id': '-100900',
            'warning_enabled': 'on',
            'overdue_enabled': 'on',
            'completed_late_enabled': 'on',
            'is_active': 'on',
        },
    )
    assert response.status_code == 302
    notification_settings = StoreNotificationSettings.objects.get(
        store=portal_setup['first']
    )
    assert notification_settings.telegram_chat_id == '-100900'
    log = AuditLog.objects.get(
        action=AuditLog.Action.STORE_NOTIFICATION_SETTINGS_UPDATED
    )
    assert 'environment-secret-must-not-leak' not in str(log.old_value)
    assert 'environment-secret-must-not-leak' not in str(log.new_value)


def test_system_admin_transfers_director_and_old_store_access_stops(
    client,
    portal_setup,
):
    director = portal_setup['director']
    update_managed_user(
        director,
        {
            'username': director.username,
            'first_name': director.first_name,
            'last_name': director.last_name,
            'email': director.email,
            'role': EmployeeProfile.Role.STORE_DIRECTOR,
            'store': portal_setup['second'],
            'is_active': True,
        },
        portal_setup['admin'],
    )
    assert get_user_store(director) == portal_setup['second']
    assert AuditLog.objects.filter(
        action=AuditLog.Action.USER_STORE_CHANGED,
        actor=portal_setup['admin'],
    ).exists()
    client.force_login(director)
    assert client.get(reverse('checklists:director_dashboard')).status_code == 200
    assert client.get(
        reverse(
            'checklists:director_employee_edit',
            args=[portal_setup['employee'].pk],
        )
    ).status_code == 404


def test_portal_pages_render_without_password_or_telegram_token(
    client,
    portal_setup,
    settings,
):
    settings.TELEGRAM_BOT_TOKEN = 'html-forbidden-telegram-token'
    forbidden_password = 'Strong-Test-934!'
    client.force_login(portal_setup['director'])
    director_urls = (
        reverse('checklists:director_dashboard'),
        reverse('checklists:director_employees'),
        reverse('checklists:director_shifts'),
        reverse('checklists:director_questions'),
        reverse('checklists:director_schedule'),
        reverse('checklists:director_notifications'),
        reverse('checklists:director_reports'),
        reverse('checklists:director_report_daily'),
        reverse('checklists:director_report_employees'),
        reverse('checklists:director_report_revisions'),
        reverse(
            'checklists:director_checklist_detail',
            args=[portal_setup['daily'].pk],
        ),
    )
    for url in director_urls:
        response = client.get(url)
        assert response.status_code in {200, 302}
        assert 'html-forbidden-telegram-token' not in response.content.decode()
        assert forbidden_password not in response.content.decode()

    client.force_login(portal_setup['admin'])
    system_urls = (
        reverse('checklists:system_admin_dashboard'),
        reverse('checklists:system_stores'),
        reverse(
            'checklists:system_store_detail',
            args=[portal_setup['first'].pk],
        ),
        reverse('checklists:system_users'),
        reverse(
            'checklists:system_user_detail',
            args=[portal_setup['director'].pk],
        ),
        reverse('checklists:system_audit'),
    )
    for url in system_urls:
        response = client.get(url)
        assert response.status_code == 200
        assert 'html-forbidden-telegram-token' not in response.content.decode()
        assert forbidden_password not in response.content.decode()


def test_director_sees_delete_action_and_get_only_confirms(client, portal_setup):
    question = get_current_questions(portal_setup['first']).first()
    client.force_login(portal_setup['director'])
    list_page = client.get(reverse('checklists:director_questions'))
    delete_url = reverse(
        'checklists:director_question_delete',
        args=[question.pk],
    )
    assert delete_url in list_page.content.decode()
    count_before = ChecklistItem.objects.count()
    confirmation = client.get(delete_url)
    content = confirmation.content.decode()
    assert confirmation.status_code == 200
    assert question.text in content
    assert 'Историческое использование' in content
    assert 'история сохранится' in content
    assert ChecklistItem.objects.count() == count_before


def test_post_hard_deletes_unused_draft_question_and_normalizes_order(
    client,
    portal_setup,
):
    template = ChecklistTemplate.objects.get(store=portal_setup['first'])
    draft = ChecklistTemplateVersion.objects.create(
        template=template,
        version_number=99,
        created_by=portal_setup['director'],
    )
    section = ChecklistSection.objects.create(
        version=draft,
        code='opening',
        name='Черновик утра',
        sort_order=1,
    )
    questions = [
        ChecklistItem.objects.create(
            section=section,
            text=f'Черновой вопрос {index}',
            sort_order=sort_order,
        )
        for index, sort_order in enumerate((10, 30, 50), start=1)
    ]
    target_id = questions[1].pk
    client.force_login(portal_setup['director'])
    response = client.post(
        reverse('checklists:director_question_delete', args=[target_id])
    )
    assert response.status_code == 302
    assert not ChecklistItem.objects.filter(pk=target_id).exists()
    assert list(
        ChecklistItem.objects.filter(section=section)
        .order_by('sort_order')
        .values_list('sort_order', flat=True)
    ) == [1, 2]
    log = AuditLog.objects.get(
        action=AuditLog.Action.CHECKLIST_QUESTION_DELETED,
        object_id=str(target_id),
    )
    assert log.new_value['method'] == 'hard_delete'
    versions_after_first_post = ChecklistTemplateVersion.objects.count()
    assert client.post(
        reverse('checklists:director_question_delete', args=[target_id])
    ).status_code == 302
    assert ChecklistTemplateVersion.objects.count() == versions_after_first_post


def test_used_question_is_removed_by_versioning_and_history_is_preserved(
    client,
    portal_setup,
):
    question = get_current_questions(portal_setup['first']).get(
        section__code='opening'
    )
    source_version = question.section.version
    snapshot = portal_setup['daily'].items.get(section_code='opening')
    answer = snapshot.answer
    snapshot_id = snapshot.pk
    answer_id = answer.pk
    versions_before = ChecklistTemplateVersion.objects.filter(
        template=source_version.template
    ).count()
    client.force_login(portal_setup['director'])
    delete_url = reverse(
        'checklists:director_question_delete',
        args=[question.pk],
    )
    response = client.post(delete_url, follow=True)
    assert response.status_code == 200
    assert 'Вопрос исключён из новых чек-листов. История сохранена.' in response.content.decode()
    assert ChecklistItem.objects.filter(pk=question.pk).exists()
    source_version.refresh_from_db()
    assert source_version.status == ChecklistTemplateVersion.Status.ARCHIVED
    published = ChecklistTemplateVersion.objects.get(
        template=source_version.template,
        status=ChecklistTemplateVersion.Status.PUBLISHED,
    )
    assert not published.sections.filter(items__text=question.text).exists()
    assert DailyChecklist.objects.filter(items__pk=snapshot_id).exists()
    assert ChecklistAnswer.objects.filter(pk=answer_id).exists()
    new_daily = create_daily_checklist(
        portal_setup['terminal'],
        WORK_DATE + timedelta(days=1),
    )
    assert not new_daily.items.filter(item_text=question.text).exists()
    log = AuditLog.objects.get(
        action=AuditLog.Action.CHECKLIST_QUESTION_REMOVED_FROM_TEMPLATE,
        object_id=str(question.pk),
    )
    assert log.new_value['method'] == 'removed_from_new_version'
    assert log.new_value['section_code'] == 'opening'
    assert ChecklistTemplateVersion.objects.filter(
        template=source_version.template,
        status=ChecklistTemplateVersion.Status.PUBLISHED,
    ).count() == 1
    assert ChecklistTemplateVersion.objects.filter(
        template=source_version.template
    ).count() == versions_before + 1

    versions_after_first_post = ChecklistTemplateVersion.objects.filter(
        template=source_version.template
    ).count()
    assert client.post(delete_url).status_code == 302
    assert ChecklistTemplateVersion.objects.filter(
        template=source_version.template
    ).count() == versions_after_first_post


def test_director_cannot_delete_foreign_question(client, portal_setup):
    make_template(portal_setup['second'], portal_setup['other_director'])
    foreign = get_current_questions(portal_setup['second']).first()
    client.force_login(portal_setup['director'])
    url = reverse('checklists:director_question_delete', args=[foreign.pk])
    assert client.get(url).status_code == 404
    assert client.post(url).status_code == 404
    assert ChecklistItem.objects.filter(pk=foreign.pk).exists()


def test_store_account_cannot_access_question_delete(client, portal_setup):
    question = get_current_questions(portal_setup['first']).first()
    client.force_login(portal_setup['terminal_user'])
    url = reverse('checklists:director_question_delete', args=[question.pk])
    assert client.get(url).status_code == 403
    assert client.post(url).status_code == 403


def test_question_delete_post_requires_csrf(portal_setup):
    question = get_current_questions(portal_setup['first']).first()
    csrf_client = Client(enforce_csrf_checks=True)
    csrf_client.force_login(portal_setup['director'])
    response = csrf_client.post(
        reverse('checklists:director_question_delete', args=[question.pk])
    )
    assert response.status_code == 403
    assert ChecklistItem.objects.filter(pk=question.pk).exists()


def test_system_admin_sees_store_delete_and_other_roles_are_denied(
    client,
    portal_setup,
):
    delete_url = reverse(
        'checklists:system_admin_store_delete',
        args=[portal_setup['first'].pk],
    )
    client.force_login(portal_setup['admin'])
    page = client.get(reverse('checklists:system_stores'))
    assert page.status_code == 200
    assert delete_url in page.content.decode()

    client.force_login(portal_setup['director'])
    denied = client.get(reverse('checklists:system_stores'))
    assert denied.status_code == 403
    assert delete_url not in denied.content.decode()
    assert client.post(delete_url).status_code == 403

    client.force_login(portal_setup['terminal_user'])
    assert client.get(delete_url).status_code == 403
    assert client.post(delete_url).status_code == 403


def test_empty_store_with_technical_defaults_is_hard_deleted_and_audited(
    client,
    portal_setup,
):
    store = create_store_with_defaults(
        actor=portal_setup['admin'],
        name='Пустой магазин',
        code='empty-store-delete',
        timezone_name='Europe/Moscow',
    )
    store_id = store.pk
    summary = get_store_deletion_summary(store)
    assert summary.can_hard_delete is True
    assert summary.blocking_reasons == ()
    assert hasattr(store, 'checklist_schedule')
    assert hasattr(store, 'notification_settings')

    client.force_login(portal_setup['admin'])
    url = reverse('checklists:system_admin_store_delete', args=[store_id])
    confirmation = client.get(url)
    assert confirmation.status_code == 200
    assert 'Магазин будет удалён без возможности восстановления' in confirmation.content.decode()
    assert Store.objects.filter(pk=store_id).exists()
    store_audit_ids = list(
        AuditLog.objects.filter(store_id=store_id).values_list('pk', flat=True)
    )
    assert store_audit_ids

    response = client.post(url, follow=True)
    assert response.status_code == 200
    assert not Store.objects.filter(pk=store_id).exists()
    assert not StoreChecklistSchedule.objects.filter(store_id=store_id).exists()
    assert not StoreNotificationSettings.objects.filter(store_id=store_id).exists()
    deleted_log = AuditLog.objects.get(
        action=AuditLog.Action.STORE_DELETED,
        object_id=str(store_id),
    )
    assert deleted_log.store is None
    assert deleted_log.new_value['deleted_store_id'] == store_id
    assert deleted_log.new_value['deleted_store_name'] == 'Пустой магазин'
    assert deleted_log.new_value['deleted_store_code'] == 'empty-store-delete'
    assert (
        deleted_log.new_value['deleted_audit_entries_count']
        == len(store_audit_ids)
    )
    assert (
        deleted_log.new_value['method']
        == 'hard_delete_with_audit_cleanup'
    )
    assert not AuditLog.objects.filter(pk__in=store_audit_ids).exists()
    assert not AuditLog.objects.filter(store_id=store_id).exists()
    connection.check_constraints()

    deletion_logs = AuditLog.objects.filter(
        action=AuditLog.Action.STORE_DELETED,
        object_id=str(store_id),
    ).count()
    assert client.post(url).status_code == 302
    assert AuditLog.objects.filter(
        action=AuditLog.Action.STORE_DELETED,
        object_id=str(store_id),
    ).count() == deletion_logs


@pytest.mark.parametrize(
    'blocker',
    ('director', 'store_account', 'employee', 'template', 'configured_schedule'),
)
def test_each_business_relation_blocks_physical_store_deletion(
    client,
    portal_setup,
    blocker,
):
    store = create_store_with_defaults(
        actor=portal_setup['admin'],
        name=f'Блокирующий магазин {blocker}',
        code=f'blocked-{blocker.replace("_", "-")}',
        timezone_name='Europe/Moscow',
    )
    if blocker == 'director':
        create_access_user(
            f'blocking-{blocker}',
            EmployeeProfile.Role.STORE_DIRECTOR,
            store,
        )
    elif blocker == 'store_account':
        create_access_user(
            f'blocking-{blocker}',
            EmployeeProfile.Role.STORE_ACCOUNT,
            store,
        )
    elif blocker == 'employee':
        StoreEmployee.objects.create(
            store=store,
            first_name='Исторический',
            display_name='Исторический сотрудник',
        )
    elif blocker == 'template':
        ChecklistTemplate.objects.create(store=store, name='Пустой шаблон')
    else:
        schedule = store.checklist_schedule
        schedule.morning_deadline = time(12)
        schedule.save()

    summary = get_store_deletion_summary(store)
    assert summary.can_hard_delete is False
    assert summary.blocking_reasons
    client.force_login(portal_setup['admin'])
    response = client.post(
        reverse('checklists:system_admin_store_delete', args=[store.pk])
    )
    assert response.status_code == 302
    store.refresh_from_db()
    assert store.is_active is False
    assert AuditLog.objects.filter(
        store=store,
        action=AuditLog.Action.STORE_DEACTIVATED_WITH_HISTORY,
    ).exists()


def test_store_with_history_deactivates_access_and_preserves_all_history(
    client,
    portal_setup,
):
    store = portal_setup['first']
    answer = portal_setup['daily'].items.get(section_code='opening').answer
    update_answer(
        answer,
        ChecklistAnswer.Status.COMPLETED,
        '',
        portal_setup['terminal_user'],
        employee=portal_setup['employee'],
        at=NOW,
    )
    update_answer(
        answer,
        ChecklistAnswer.Status.FAILED,
        'Найдена проблема',
        portal_setup['terminal_user'],
        employee=portal_setup['employee'],
        change_reason='Повторная проверка',
        at=NOW + timedelta(minutes=1),
    )
    revision = AnswerRevision.objects.get(answer=answer)
    shift = DailyShiftAssignment.objects.create(
        store=store,
        employee=portal_setup['employee'],
        work_date=WORK_DATE,
        created_by=portal_setup['director'],
    )
    summary = get_store_deletion_summary(store)
    assert summary.directors_count == 1
    assert summary.store_accounts_count == 1
    assert summary.employees_count == 1
    assert summary.shifts_count == 1
    assert summary.templates_count == 1
    assert summary.versions_count >= 1
    assert summary.daily_checklists_count == 1
    assert summary.answers_count >= 1
    assert summary.revisions_count == 1
    assert summary.can_hard_delete is False

    director_browser = Client()
    director_browser.force_login(portal_setup['director'])
    director_session_key = director_browser.session.session_key
    terminal_browser = Client()
    terminal_browser.force_login(portal_setup['terminal_user'])
    terminal_session_key = terminal_browser.session.session_key
    assert Session.objects.filter(
        session_key__in=(director_session_key, terminal_session_key)
    ).count() == 2

    user_ids = (portal_setup['director'].pk, portal_setup['terminal_user'].pk)
    answer_id = answer.pk
    revision_id = revision.pk
    daily_id = portal_setup['daily'].pk
    employee_id = portal_setup['employee'].pk
    template_id = portal_setup['daily'].template_version.template_id
    client.force_login(portal_setup['admin'])
    url = reverse('checklists:system_admin_store_delete', args=[store.pk])
    response = client.post(url, follow=True)
    assert response.status_code == 200
    assert (
        'Магазин деактивирован. Пользовательский доступ отключён, история сохранена'
        in response.content.decode()
    )
    store.refresh_from_db()
    assert store.is_active is False
    assert not EmployeeProfile.objects.filter(store=store, is_active=True).exists()
    assert not User.objects.filter(pk__in=user_ids, is_active=True).exists()
    assert User.objects.filter(pk__in=user_ids).count() == 2
    assert not StoreTerminalAccount.objects.get(store=store).is_active
    assert not Session.objects.filter(
        session_key__in=(director_session_key, terminal_session_key)
    ).exists()
    assert DailyChecklist.objects.filter(pk=daily_id).exists()
    assert ChecklistAnswer.objects.filter(pk=answer_id).exists()
    assert AnswerRevision.objects.filter(pk=revision_id).exists()
    assert StoreEmployee.objects.filter(pk=employee_id).exists()
    assert DailyShiftAssignment.objects.filter(pk=shift.pk).exists()
    assert ChecklistTemplate.objects.filter(pk=template_id).exists()
    assert portal_setup['admin'].is_active
    log = AuditLog.objects.get(
        store=store,
        action=AuditLog.Action.STORE_DEACTIVATED_WITH_HISTORY,
    )
    assert log.new_value['method'] == 'deactivated_with_history'
    assert log.new_value['directors_count'] == 1
    assert log.new_value['employees_count'] == 1
    assert log.new_value['templates_count'] == 1
    assert log.new_value['daily_checklists_count'] == 1

    counts_before_repeat = {
        'answers': ChecklistAnswer.objects.filter(pk=answer_id).count(),
        'revisions': AnswerRevision.objects.filter(pk=revision_id).count(),
        'audit': AuditLog.objects.filter(
            store=store,
            action=AuditLog.Action.STORE_DEACTIVATED_WITH_HISTORY,
        ).count(),
    }
    repeated = client.post(url, follow=True)
    assert repeated.status_code == 200
    assert 'Магазин уже деактивирован' in repeated.content.decode()
    assert ChecklistAnswer.objects.filter(pk=answer_id).count() == counts_before_repeat['answers']
    assert AnswerRevision.objects.filter(pk=revision_id).count() == counts_before_repeat['revisions']
    assert AuditLog.objects.filter(
        store=store,
        action=AuditLog.Action.STORE_DEACTIVATED_WITH_HISTORY,
    ).count() == counts_before_repeat['audit']


def test_store_delete_post_requires_csrf(portal_setup):
    store = create_store_with_defaults(
        actor=portal_setup['admin'],
        name='CSRF магазин',
        code='csrf-store-delete',
        timezone_name='Europe/Moscow',
    )
    csrf_client = Client(enforce_csrf_checks=True)
    csrf_client.force_login(portal_setup['admin'])
    response = csrf_client.post(
        reverse('checklists:system_admin_store_delete', args=[store.pk])
    )
    assert response.status_code == 403
    assert Store.objects.filter(pk=store.pk).exists()


def test_audit_only_never_blocks_store_deletion_and_is_shown_separately(
    client,
    portal_setup,
):
    store = Store.objects.create(name='Только аудит', code='audit-only')
    for action in (
        AuditLog.Action.STORE_CREATED,
        AuditLog.Action.STORE_DEACTIVATED,
        AuditLog.Action.TEMPLATE_VERSION_PUBLISHED,
    ):
        AuditLog.objects.create(
            actor=portal_setup['admin'],
            store=store,
            object_type=Store._meta.label_lower,
            object_id=str(store.pk),
            action=action,
        )

    summary = get_store_deletion_summary(store)
    assert summary.can_hard_delete is True
    assert summary.audit_count == 3
    assert summary.audit_will_be_deleted is True
    assert summary.blocking_reasons == ()

    client.force_login(portal_setup['admin'])
    page = client.get(
        reverse('checklists:system_admin_store_delete', args=[store.pk])
    )
    content = page.content.decode()
    assert page.status_code == 200
    assert 'Удалить магазин и журнал' in content
    assert '3 записей' in content


def test_hard_delete_removes_every_store_audit_entry(client, portal_setup):
    store = create_store_with_defaults(
        actor=portal_setup['admin'],
        name='Аудит удаляется',
        code='delete-all-audit',
        timezone_name='Europe/Moscow',
    )
    AuditLog.objects.create(
        actor=portal_setup['admin'],
        store=store,
        object_type=Store._meta.label_lower,
        object_id=str(store.pk),
        action=AuditLog.Action.STORE_UPDATED,
    )
    store_id = store.pk
    assert AuditLog.objects.filter(store=store).count() == 2

    client.force_login(portal_setup['admin'])
    response = client.post(
        reverse('checklists:system_admin_store_delete', args=[store_id]),
        follow=True,
    )
    assert response.status_code == 200
    assert not Store.objects.filter(pk=store_id).exists()
    assert not AuditLog.objects.filter(store_id=store_id).exists()
    deletion_log = AuditLog.objects.get(
        action=AuditLog.Action.STORE_DELETED,
        object_id=str(store_id),
    )
    assert deletion_log.store is None
    assert deletion_log.new_value['deleted_audit_entries_count'] == 2
    assert 'Удалено записей журнала: 2' in response.content.decode()


def test_audit_clear_controls_are_system_admin_only(client, portal_setup):
    audit_url = reverse('checklists:system_audit')
    store_clear_url = reverse(
        'checklists:system_admin_store_audit_clear',
        args=[portal_setup['first'].pk],
    )
    clear_all_url = reverse('checklists:system_admin_audit_clear_all')

    client.force_login(portal_setup['admin'])
    all_page = client.get(audit_url)
    filtered_page = client.get(
        audit_url,
        {'store': portal_setup['first'].pk},
    )
    assert clear_all_url in all_page.content.decode()
    assert store_clear_url in filtered_page.content.decode()

    for user in (portal_setup['director'], portal_setup['terminal_user']):
        client.force_login(user)
        assert client.get(audit_url).status_code == 403
        assert client.get(store_clear_url).status_code == 403
        assert client.post(
            store_clear_url,
            {'confirmation': 'ОЧИСТИТЬ'},
        ).status_code == 403
        assert client.post(
            clear_all_url,
            {'confirmation': 'ОЧИСТИТЬ ВЕСЬ ЖУРНАЛ'},
        ).status_code == 403


def test_clear_store_audit_requires_phrase_and_preserves_other_scopes(
    client,
    portal_setup,
):
    first = portal_setup['first']
    second = portal_setup['second']
    AuditLog.objects.create(
        actor=portal_setup['admin'],
        store=second,
        object_type=Store._meta.label_lower,
        object_id=str(second.pk),
        action=AuditLog.Action.STORE_UPDATED,
    )
    global_log = AuditLog.objects.create(
        actor=portal_setup['admin'],
        store=None,
        object_type=Store._meta.label_lower,
        object_id='global-test',
        action=AuditLog.Action.STORE_DELETED,
    )
    first_count = AuditLog.objects.filter(store=first).count()
    second_ids = set(
        AuditLog.objects.filter(store=second).values_list('pk', flat=True)
    )
    url = reverse(
        'checklists:system_admin_store_audit_clear',
        args=[first.pk],
    )
    client.force_login(portal_setup['admin'])

    get_response = client.get(url)
    assert get_response.status_code == 200
    assert AuditLog.objects.filter(store=first).count() == first_count
    invalid = client.post(url, {'confirmation': 'неверно'})
    assert invalid.status_code == 200
    assert 'Введите точную фразу' in invalid.content.decode()
    assert AuditLog.objects.filter(store=first).count() == first_count

    response = client.post(url, {'confirmation': '  очистить  '}, follow=True)
    assert response.status_code == 200
    assert not AuditLog.objects.filter(store=first).exists()
    assert set(
        AuditLog.objects.filter(store=second).values_list('pk', flat=True)
    ) == second_ids
    assert AuditLog.objects.filter(pk=global_log.pk).exists()
    cleanup = AuditLog.objects.get(
        action=AuditLog.Action.AUDIT_LOG_CLEARED,
        new_value__scope='store',
    )
    assert cleanup.store is None
    assert cleanup.new_value['deleted_entries_count'] == first_count
    assert cleanup.new_value['cleared_store_id'] == first.pk
    assert cleanup.new_value['cleared_store_name'] == first.name
    assert cleanup.new_value['cleared_store_code'] == first.code


def test_clearing_empty_store_audit_is_idempotent(client, portal_setup):
    store = Store.objects.create(name='Пустой журнал', code='empty-audit')
    url = reverse(
        'checklists:system_admin_store_audit_clear',
        args=[store.pk],
    )
    client.force_login(portal_setup['admin'])
    before = AuditLog.objects.filter(
        action=AuditLog.Action.AUDIT_LOG_CLEARED
    ).count()
    response = client.post(url, {'confirmation': 'ОЧИСТИТЬ'}, follow=True)
    assert response.status_code == 200
    assert 'Журнал магазина уже пуст' in response.content.decode()
    assert AuditLog.objects.filter(
        action=AuditLog.Action.AUDIT_LOG_CLEARED
    ).count() == before


def test_clear_all_audit_leaves_one_global_entry_and_business_data(
    client,
    portal_setup,
):
    stores_before = Store.objects.count()
    users_before = User.objects.count()
    employees_before = StoreEmployee.objects.count()
    checklists_before = DailyChecklist.objects.count()
    answers_before = ChecklistAnswer.objects.count()
    old_count = AuditLog.objects.count()
    url = reverse('checklists:system_admin_audit_clear_all')
    client.force_login(portal_setup['admin'])

    get_response = client.get(url)
    assert get_response.status_code == 200
    assert AuditLog.objects.count() == old_count
    invalid = client.post(url, {'confirmation': 'ОЧИСТИТЬ'})
    assert invalid.status_code == 200
    assert AuditLog.objects.count() == old_count

    response = client.post(
        url,
        {'confirmation': ' очистить весь журнал '},
        follow=True,
    )
    assert response.status_code == 200
    assert AuditLog.objects.count() == 1
    cleanup = AuditLog.objects.get()
    assert cleanup.action == AuditLog.Action.AUDIT_LOG_CLEARED
    assert cleanup.store is None
    assert cleanup.new_value['scope'] == 'all'
    assert cleanup.new_value['deleted_entries_count'] == old_count
    assert cleanup.new_value['affected_stores_count'] >= 1
    assert cleanup.new_value['deleted_global_entries_count'] >= 0
    assert Store.objects.count() == stores_before
    assert User.objects.count() == users_before
    assert StoreEmployee.objects.count() == employees_before
    assert DailyChecklist.objects.count() == checklists_before
    assert ChecklistAnswer.objects.count() == answers_before

    repeated = client.post(
        url,
        {'confirmation': 'ОЧИСТИТЬ ВЕСЬ ЖУРНАЛ'},
    )
    assert repeated.status_code == 302
    assert AuditLog.objects.count() == 1
    assert AuditLog.objects.get().new_value['deleted_entries_count'] == 1


@pytest.mark.parametrize('route_name,route_args,phrase', (
    ('checklists:system_admin_store_audit_clear', 'store', 'ОЧИСТИТЬ'),
    ('checklists:system_admin_audit_clear_all', None, 'ОЧИСТИТЬ ВЕСЬ ЖУРНАЛ'),
))
def test_audit_clear_post_requires_csrf(
    portal_setup,
    route_name,
    route_args,
    phrase,
):
    csrf_client = Client(enforce_csrf_checks=True)
    csrf_client.force_login(portal_setup['admin'])
    args = [portal_setup['first'].pk] if route_args == 'store' else None
    response = csrf_client.post(
        reverse(route_name, args=args),
        {'confirmation': phrase},
    )
    assert response.status_code == 403


def select_admin_store(client, portal_setup):
    client.force_login(portal_setup['admin'])
    response = client.post(
        reverse('checklists:system_select_managed_store'),
        {'store': portal_setup['first'].pk},
    )
    assert response.status_code == 302


def test_system_admin_has_director_capabilities_for_selected_store(
    client,
    portal_setup,
):
    client.force_login(portal_setup['admin'])
    assert client.get(reverse('checklists:director_questions')).status_code == 403
    select_admin_store(client, portal_setup)
    for name in (
        'director_dashboard',
        'director_employees',
        'director_shifts',
        'director_questions',
        'director_schedule',
        'director_notifications',
        'director_reports',
        'director_tasks',
    ):
        assert client.get(reverse(f'checklists:{name}')).status_code in {200, 302}

    response = client.post(
        reverse('checklists:director_question_add'),
        {
            'text': 'Вопрос системного администратора',
            'description': 'Создан через общий store context',
            'section_code': 'during_day',
            'is_required': 'on',
            'answer_type': 'status',
            'comment_required_on_failure': 'on',
            'sort_order': 10,
            'is_active': 'on',
        },
    )
    assert response.status_code == 302
    question = get_current_questions(portal_setup['first']).get(
        text='Вопрос системного администратора'
    )
    assert client.post(
        reverse('checklists:director_question_deactivate', args=[question.pk])
    ).status_code == 302
    assert AuditLog.objects.filter(
        actor=portal_setup['admin'],
        store=portal_setup['first'],
    ).exists()


def test_system_admin_manages_web_tasks_and_cannot_spoof_store(
    client,
    portal_setup,
):
    select_admin_store(client, portal_setup)
    create_url = reverse('checklists:director_task_create')
    response = client.post(
        create_url,
        {
            'store': portal_setup['second'].pk,
            'date': (WORK_DATE + timedelta(days=1)).isoformat(),
            'section_code': StoreAdHocTask.SectionCode.MORNING,
            'text': 'Задача из кабинета администратора',
            'description': 'Проверка scoping',
            'is_required': 'on',
            'confirmation': 'on',
        },
    )
    assert response.status_code == 302
    task = StoreAdHocTask.objects.get(
        text='Задача из кабинета администратора'
    )
    assert task.store == portal_setup['first']
    assert task.created_by == portal_setup['admin']
    assert task.source == StoreAdHocTask.Source.WEB
    assert client.get(
        reverse('checklists:director_task_detail', args=[task.pk])
    ).status_code == 200
    assert client.post(
        reverse('checklists:director_task_cancel', args=[task.pk])
    ).status_code == 302
    task.refresh_from_db()
    assert task.status == StoreAdHocTask.Status.CANCELLED
    assert AuditLog.objects.filter(
        action=AuditLog.Action.STORE_TASK_CANCELLED,
        actor=portal_setup['admin'],
    ).exists()


def test_director_task_scope_and_store_account_denied(client, portal_setup):
    foreign = StoreAdHocTask.objects.create(
        store=portal_setup['second'],
        date=WORK_DATE + timedelta(days=1),
        section_code=StoreAdHocTask.SectionCode.MORNING,
        text='Чужая задача',
    )
    client.force_login(portal_setup['director'])
    assert client.get(reverse('checklists:director_tasks')).status_code == 200
    assert client.get(
        reverse('checklists:director_task_detail', args=[foreign.pk])
    ).status_code == 404
    client.force_login(portal_setup['terminal_user'])
    assert client.get(reverse('checklists:director_tasks')).status_code == 403


@pytest.mark.parametrize(
    'name,args',
    (
        ('system_admin_dashboard', ()),
        ('system_stores', ()),
        ('system_store_detail', ('first',)),
        ('system_users', ()),
        ('system_audit', ()),
        ('director_dashboard', ()),
        ('director_employees', ()),
        ('director_questions', ()),
        ('director_schedule', ()),
        ('director_tasks', ()),
        ('director_reports', ()),
        ('telegram_settings', ()),
    ),
)
def test_common_breadcrumbs_are_rendered(
    client,
    portal_setup,
    name,
    args,
):
    client.force_login(portal_setup['admin'])
    if name.startswith('director_') or name.startswith('telegram_'):
        client.post(
            reverse('checklists:system_select_managed_store'),
            {'store': portal_setup['first'].pk},
        )
    resolved_args = (
        [portal_setup['first'].pk]
        if args == ('first',)
        else list(args)
    )
    response = client.get(reverse(f'checklists:{name}', args=resolved_args))
    assert response.status_code in {200, 302}
    if response.status_code == 200:
        content = response.content.decode()
        assert 'aria-label="Хлебные крошки"' in content
        assert 'aria-current="page"' in content


def test_breadcrumb_home_uses_role_specific_portal(client, portal_setup):
    cases = (
        (
            portal_setup['admin'],
            reverse('checklists:system_stores'),
            '/system-admin/dashboard/',
        ),
        (
            portal_setup['director'],
            reverse('checklists:director_employees'),
            '/director/dashboard/',
        ),
        (
            portal_setup['terminal_user'],
            reverse('checklists:dashboard'),
            '/terminal/',
        ),
    )
    for user, page_url, expected_home in cases:
        client.force_login(user)
        response = client.get(page_url)
        assert response.status_code == 200
        assert f'href="{expected_home}"' in response.content.decode()
        assert get_portal_home_url(user) == expected_home
