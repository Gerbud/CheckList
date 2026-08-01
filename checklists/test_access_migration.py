from datetime import date

import pytest
from django.contrib.auth.hashers import make_password
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone


@pytest.mark.django_db(transaction=True)
def test_access_role_data_migration_preserves_history_and_passwords():
    migrate_from = [('checklists', '0006_dailycheckliststage_first_completed_at_and_more')]
    migrate_to = [('checklists', '0007_checklistitem_description_and_more')]
    executor = MigrationExecutor(connection)
    executor.migrate(migrate_from)
    old_apps = executor.loader.project_state(migrate_from).apps

    OldUser = old_apps.get_model('auth', 'User')
    OldStore = old_apps.get_model('checklists', 'Store')
    OldProfile = old_apps.get_model('checklists', 'EmployeeProfile')
    OldTerminal = old_apps.get_model('checklists', 'StoreTerminalAccount')
    OldEmployee = old_apps.get_model('checklists', 'StoreEmployee')
    OldTemplate = old_apps.get_model('checklists', 'ChecklistTemplate')
    OldVersion = old_apps.get_model('checklists', 'ChecklistTemplateVersion')
    OldSection = old_apps.get_model('checklists', 'ChecklistSection')
    OldItem = old_apps.get_model('checklists', 'ChecklistItem')
    OldDaily = old_apps.get_model('checklists', 'DailyChecklist')
    OldDailyItem = old_apps.get_model('checklists', 'DailyChecklistItem')
    OldAnswer = old_apps.get_model('checklists', 'ChecklistAnswer')
    OldAudit = old_apps.get_model('checklists', 'AuditLog')

    store = OldStore.objects.create(
        name='Магазин миграции',
        code='access-migration',
        timezone='Europe/Moscow',
    )
    terminal_store = OldStore.objects.create(
        name='Терминальный магазин',
        code='terminal-migration',
        timezone='Europe/Moscow',
    )
    password_hashes = {
        'legacy-manager': make_password('Manager-Password-934!'),
        'legacy-administrator': make_password('Admin-Password-934!'),
        'legacy-employee': make_password('Employee-Password-934!'),
        'legacy-terminal': make_password('Terminal-Password-934!'),
        'legacy-superuser': make_password('Root-Password-934!'),
    }
    users = {
        username: OldUser.objects.create(
            username=username,
            password=password_hash,
            is_active=True,
            is_superuser=username == 'legacy-superuser',
            is_staff=username == 'legacy-superuser',
        )
        for username, password_hash in password_hashes.items()
    }
    profiles = {
        'manager': OldProfile.objects.create(
            user=users['legacy-manager'], store=store, role='manager'
        ),
        'administrator': OldProfile.objects.create(
            user=users['legacy-administrator'], store=store, role='administrator'
        ),
        'employee': OldProfile.objects.create(
            user=users['legacy-employee'], store=store, role='employee'
        ),
        'terminal': OldProfile.objects.create(
            user=users['legacy-terminal'], store=terminal_store, role='employee'
        ),
    }
    OldTerminal.objects.create(
        store=terminal_store,
        user=users['legacy-terminal'],
        is_active=True,
    )
    store_employee = OldEmployee.objects.create(
        store=store,
        first_name='Исторический',
        last_name='Сотрудник',
        display_name='Исторический Сотрудник',
        personnel_number='H-39',
    )
    template = OldTemplate.objects.create(store=store, name='Исторический шаблон')
    version = OldVersion.objects.create(
        template=template,
        version_number=1,
        status='published',
        published_at=timezone.now(),
        created_by=users['legacy-manager'],
    )
    section = OldSection.objects.create(
        version=version,
        name='Утро',
        code='opening',
        sort_order=1,
    )
    OldItem.objects.create(section=section, text='Исторический вопрос', sort_order=1)
    daily = OldDaily.objects.create(
        store=store,
        employee=profiles['employee'],
        checklist_date=date(2026, 7, 15),
        template_version=version,
    )
    daily_item = OldDailyItem.objects.create(
        daily_checklist=daily,
        section_code='opening',
        section_name='Утро',
        section_sort_order=1,
        item_text='Исторический вопрос',
        item_sort_order=1,
        display_order=1,
    )
    answer = OldAnswer.objects.create(
        daily_item=daily_item,
        status='failed',
        comment='Исторический комментарий',
        answered_by=users['legacy-employee'],
        answered_by_employee=store_employee,
        last_edited_by_employee=store_employee,
    )
    audit = OldAudit.objects.create(
        store=store,
        actor=users['legacy-manager'],
        employee=store_employee,
        object_type='ChecklistAnswer',
        object_id=str(answer.pk),
        action='answer_status_changed',
        new_value={'status': 'failed'},
    )

    try:
        executor = MigrationExecutor(connection)
        executor.migrate(migrate_to)
        new_apps = executor.loader.project_state(migrate_to).apps
        NewUser = new_apps.get_model('auth', 'User')
        NewProfile = new_apps.get_model('checklists', 'EmployeeProfile')
        NewEmployee = new_apps.get_model('checklists', 'StoreEmployee')
        NewAnswer = new_apps.get_model('checklists', 'ChecklistAnswer')
        NewAudit = new_apps.get_model('checklists', 'AuditLog')

        assert NewProfile.objects.get(pk=profiles['manager'].pk).role == 'store_director'
        migrated_admin = NewProfile.objects.get(pk=profiles['administrator'].pk)
        assert (migrated_admin.role, migrated_admin.store_id) == ('system_admin', None)
        migrated_employee = NewProfile.objects.get(pk=profiles['employee'].pk)
        assert (migrated_employee.role, migrated_employee.is_active) == (None, False)
        migrated_terminal = NewProfile.objects.get(pk=profiles['terminal'].pk)
        assert (migrated_terminal.role, migrated_terminal.store_id) == (
            'store_account',
            terminal_store.pk,
        )
        emergency_admin = NewProfile.objects.get(
            user_id=users['legacy-superuser'].pk
        )
        assert (emergency_admin.role, emergency_admin.store_id) == (
            'system_admin',
            None,
        )
        for username, password_hash in password_hashes.items():
            assert NewUser.objects.get(username=username).password == password_hash

        migrated_answer = NewAnswer.objects.get(pk=answer.pk)
        assert (migrated_answer.status, migrated_answer.comment) == (
            'failed',
            'Исторический комментарий',
        )
        migrated_audit = NewAudit.objects.get(pk=audit.pk)
        assert migrated_audit.actor_id == users['legacy-manager'].pk
        assert migrated_audit.employee_id == store_employee.pk
        assert NewEmployee.objects.filter(pk=store_employee.pk).exists()
        assert 'user' not in {field.name for field in NewEmployee._meta.fields}
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
