from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import migrations, models
from django.db.models import F


WINDOW_FIELDS = (
    'morning_completion_window_minutes',
    'day_completion_window_minutes',
    'evening_completion_window_minutes',
)


def convert_hours_to_minutes(apps, schema_editor):
    Schedule = apps.get_model('checklists', 'StoreChecklistSchedule')
    database = schema_editor.connection.alias
    Schedule.objects.using(database).update(
        morning_completion_window_minutes=(
            F('morning_completion_window_minutes') * 60
        ),
        day_completion_window_minutes=(
            F('day_completion_window_minutes') * 60
        ),
        evening_completion_window_minutes=(
            F('evening_completion_window_minutes') * 60
        ),
    )


def convert_minutes_to_hours(apps, schema_editor):
    Schedule = apps.get_model('checklists', 'StoreChecklistSchedule')
    database = schema_editor.connection.alias
    schedules = list(Schedule.objects.using(database).all())
    if any(
        getattr(schedule, field_name) % 60
        for schedule in schedules
        for field_name in WINDOW_FIELDS
    ):
        raise RuntimeError(
            'Нельзя откатить минутные окна, которые не кратны 60.'
        )
    for schedule in schedules:
        for field_name in WINDOW_FIELDS:
            setattr(
                schedule,
                field_name,
                getattr(schedule, field_name) // 60,
            )
    if schedules:
        Schedule.objects.using(database).bulk_update(
            schedules,
            WINDOW_FIELDS,
        )


class Migration(migrations.Migration):

    dependencies = [
        (
            'checklists',
            '0024_dailycheckliststage_completion_available_at_and_more',
        ),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name='storechecklistschedule',
            name='schedule_morning_completion_window_0_12',
        ),
        migrations.RemoveConstraint(
            model_name='storechecklistschedule',
            name='schedule_day_completion_window_0_12',
        ),
        migrations.RemoveConstraint(
            model_name='storechecklistschedule',
            name='schedule_evening_completion_window_0_12',
        ),
        migrations.RenameField(
            model_name='storechecklistschedule',
            old_name='morning_completion_window_hours',
            new_name='morning_completion_window_minutes',
        ),
        migrations.RenameField(
            model_name='storechecklistschedule',
            old_name='day_completion_window_hours',
            new_name='day_completion_window_minutes',
        ),
        migrations.RenameField(
            model_name='storechecklistschedule',
            old_name='evening_completion_window_hours',
            new_name='evening_completion_window_minutes',
        ),
        migrations.RunPython(
            convert_hours_to_minutes,
            convert_minutes_to_hours,
        ),
        migrations.AlterField(
            model_name='storechecklistschedule',
            name='morning_completion_window_minutes',
            field=models.PositiveSmallIntegerField(
                default=120,
                help_text=(
                    'Изменение применяется только к новым ежедневным '
                    'чек-листам'
                ),
                validators=[
                    MinValueValidator(0),
                    MaxValueValidator(720),
                ],
                verbose_name='окно завершения утреннего этапа, минут',
            ),
        ),
        migrations.AlterField(
            model_name='storechecklistschedule',
            name='day_completion_window_minutes',
            field=models.PositiveSmallIntegerField(
                default=120,
                help_text=(
                    'Изменение применяется только к новым ежедневным '
                    'чек-листам'
                ),
                validators=[
                    MinValueValidator(0),
                    MaxValueValidator(720),
                ],
                verbose_name='окно завершения дневного этапа, минут',
            ),
        ),
        migrations.AlterField(
            model_name='storechecklistschedule',
            name='evening_completion_window_minutes',
            field=models.PositiveSmallIntegerField(
                default=120,
                help_text=(
                    'Изменение применяется только к новым ежедневным '
                    'чек-листам'
                ),
                validators=[
                    MinValueValidator(0),
                    MaxValueValidator(720),
                ],
                verbose_name='окно завершения вечернего этапа, минут',
            ),
        ),
        migrations.AddConstraint(
            model_name='storechecklistschedule',
            constraint=models.CheckConstraint(
                condition=models.Q(
                    morning_completion_window_minutes__in=tuple(
                        range(0, 721, 15)
                    ),
                ),
                name='schedule_morning_completion_window_valid',
            ),
        ),
        migrations.AddConstraint(
            model_name='storechecklistschedule',
            constraint=models.CheckConstraint(
                condition=models.Q(
                    day_completion_window_minutes__in=tuple(
                        range(0, 721, 15)
                    ),
                ),
                name='schedule_day_completion_window_valid',
            ),
        ),
        migrations.AddConstraint(
            model_name='storechecklistschedule',
            constraint=models.CheckConstraint(
                condition=models.Q(
                    evening_completion_window_minutes__in=tuple(
                        range(0, 721, 15)
                    ),
                ),
                name='schedule_evening_completion_window_valid',
            ),
        ),
    ]
