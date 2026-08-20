from importlib import import_module
from types import SimpleNamespace
from unittest.mock import Mock


migration = import_module('warranty.migrations.0032_admin_log_utf8mb4')


def test_admin_log_is_converted_to_utf8mb4_on_mysql():
    schema_editor = SimpleNamespace(
        connection=SimpleNamespace(vendor='mysql'),
        execute=Mock(),
    )

    migration.use_utf8mb4_for_admin_log(None, schema_editor)

    schema_editor.execute.assert_called_once_with(
        'ALTER TABLE django_admin_log '
        'CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci'
    )


def test_admin_log_migration_is_noop_on_other_databases():
    schema_editor = SimpleNamespace(
        connection=SimpleNamespace(vendor='sqlite'),
        execute=Mock(),
    )

    migration.use_utf8mb4_for_admin_log(None, schema_editor)

    schema_editor.execute.assert_not_called()
