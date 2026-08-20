from django.db import migrations


def use_utf8mb4_for_message_text(apps, schema_editor):
    if schema_editor.connection.vendor != 'mysql':
        return
    schema_editor.execute(
        'ALTER TABLE warranty_warrantytelegrammessage '
        'MODIFY text LONGTEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL'
    )


class Migration(migrations.Migration):
    dependencies = [('warranty', '0009_alter_warrantytelegramsettings_chat_id_and_more')]

    operations = [
        migrations.RunPython(use_utf8mb4_for_message_text, migrations.RunPython.noop),
    ]
