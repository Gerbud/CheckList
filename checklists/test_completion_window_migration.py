import pytest
from django.core.exceptions import FieldDoesNotExist
from django.db import connection
from django.db.migrations.executor import MigrationExecutor


pytestmark = pytest.mark.django_db(transaction=True)


def test_completion_window_migration_converts_hours_to_minutes():
    executor = MigrationExecutor(connection)
    old_target = (
        'checklists',
        '0024_dailycheckliststage_completion_available_at_and_more',
    )
    new_target = (
        'checklists',
        '0025_completion_windows_in_minutes',
    )
    executor.migrate([old_target])
    old_apps = executor.loader.project_state([old_target]).apps
    OldStore = old_apps.get_model('checklists', 'Store')
    OldSchedule = old_apps.get_model(
        'checklists',
        'StoreChecklistSchedule',
    )
    store = OldStore.objects.create(
        name='Миграция минут',
        code='completion-window-minute-migration',
    )
    OldSchedule.objects.create(
        store=store,
        morning_completion_window_hours=2,
        day_completion_window_hours=1,
        evening_completion_window_hours=0,
    )

    executor = MigrationExecutor(connection)
    executor.migrate([new_target])
    new_apps = executor.loader.project_state([new_target]).apps
    NewSchedule = new_apps.get_model(
        'checklists',
        'StoreChecklistSchedule',
    )
    migrated = NewSchedule.objects.get(store_id=store.pk)
    assert migrated.morning_completion_window_minutes == 120
    assert migrated.day_completion_window_minutes == 60
    assert migrated.evening_completion_window_minutes == 0
    for old_field_name in (
        'morning_completion_window_hours',
        'day_completion_window_hours',
        'evening_completion_window_hours',
    ):
        with pytest.raises(FieldDoesNotExist):
            NewSchedule._meta.get_field(old_field_name)

    executor = MigrationExecutor(connection)
    leaf_nodes = executor.loader.graph.leaf_nodes()
    executor.migrate(leaf_nodes)
