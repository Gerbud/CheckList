from django.db import migrations, models


OLD = (
    'Почти готово. Перед тем как сохранить ваши контакты, нужно согласие на обработку персональных данных.\n\n'
    'Что сохраним:\n• ФИО и телефон\n• Telegram ID\n• фото этикеток, чека и гарантийного талона\n• артикулы, серийные номера и дату покупки\n\n'
    'Зачем: зарегистрировать электронную гарантию, оформлять обращения без поездки в магазин и связываться с вами по гарантии.\n\n'
    'Оператор: {operator}, {operator_address}.\n{recognition_notice}\n'
    'Согласие действует до достижения этих целей или до отзыва. Отозвать согласие: {withdrawal_contact}.\n'
    'Политика обработки данных: {privacy_policy_url}\n\n'
    'Согласие добровольное. Нажимая «Согласен», вы подтверждаете согласие на получение, запись, хранение, уточнение, использование и удаление указанных данных с применением автоматизированных средств.'
)
NEW = OLD.replace(
    'Политика обработки данных: {privacy_policy_url}\n\n', '',
) + '\n\n{privacy_policy_url}'


def move_existing_link(apps, schema_editor):
    Settings = apps.get_model('warranty', 'WarrantyCustomerBotSettings')
    Settings.objects.filter(consent_text_template=OLD).update(consent_text_template=NEW)


class Migration(migrations.Migration):
    dependencies = [('warranty', '0026_warrantycustomerconsultationmessage')]

    operations = [
        migrations.AlterField(
            model_name='warrantycustomerbotsettings', name='consent_text_template',
            field=models.TextField(default=NEW, help_text='Можно использовать: {operator}, {operator_address}, {recognition_notice}, {withdrawal_contact}, {privacy_policy_url}.', verbose_name='текст согласия в боте'),
        ),
        migrations.RunPython(move_existing_link, migrations.RunPython.noop),
    ]
