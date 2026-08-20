import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('warranty', '0030_warrantytelegramthread_deleted_topics')]

    operations = [
        migrations.AddField(
            model_name='warrantytelegramsettings',
            name='closed_topic_retention_days',
            field=models.PositiveSmallIntegerField(
                default=10,
                help_text='Срок считается с момента перехода обращения в статус «Закрыт».',
                validators=[
                    django.core.validators.MinValueValidator(1),
                    django.core.validators.MaxValueValidator(3650),
                ],
                verbose_name='удалять закрытые темы через, дней',
            ),
        ),
    ]
