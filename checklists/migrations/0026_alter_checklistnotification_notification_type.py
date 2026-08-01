from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('checklists', '0025_completion_windows_in_minutes'),
    ]

    operations = [
        migrations.AlterField(
            model_name='checklistnotification',
            name='notification_type',
            field=models.CharField(
                choices=[
                    ('deadline_warning', 'Скоро дедлайн'),
                    ('overdue', 'Просрочка'),
                    ('completed_late', 'Завершено с опозданием'),
                    (
                        'completed_with_issues',
                        'Завершено с невыполненными пунктами',
                    ),
                ],
                max_length=24,
                verbose_name='тип уведомления',
            ),
        ),
    ]
