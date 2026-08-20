from django.db import migrations


def use_utf8mb4_for_admin_log(apps, schema_editor):
    if schema_editor.connection.vendor != 'mysql':
        return
    schema_editor.execute(
        'ALTER TABLE django_admin_log '
        'CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci'
    )


class Migration(migrations.Migration):
    atomic = False
    dependencies = [
        ('admin', '0003_logentry_add_action_flag_choices'),
        ('warranty', '0031_warrantytelegramsettings_closed_topic_retention_days'),
    ]
    operations = [
        migrations.RunPython(use_utf8mb4_for_admin_log, migrations.RunPython.noop),
    ]
