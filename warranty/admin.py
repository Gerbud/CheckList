from django import forms
from django.contrib import admin, messages
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html, format_html_join

from warranty.models import WarrantyAttachment, WarrantyBitrixOutbox, WarrantyBitrixSyncState, WarrantyClaim, WarrantyCustomerBotSettings, WarrantyCustomerConsultationMessage, WarrantyCustomerDocument, WarrantyCustomerProfile, WarrantyCustomerSession, WarrantyCustomerSupportMessage, WarrantyCustomerSupportThread, WarrantyCustomerUpdate, WarrantyHistoryEvent, WarrantyProductRegistration, WarrantyTelegramMessage, WarrantyTelegramSettings, WarrantyTelegramStatusButton, WarrantyTelegramStatusIcon, WarrantyTelegramThread, WarrantyWorkItem

for model in (WarrantyAttachment, WarrantyHistoryEvent, WarrantyWorkItem, WarrantyTelegramThread, WarrantyBitrixOutbox, WarrantyBitrixSyncState):
    admin.site.register(model)

admin.site.register(WarrantyCustomerSession)
admin.site.register(WarrantyCustomerDocument)
admin.site.register(WarrantyCustomerUpdate)
admin.site.register(WarrantyCustomerSupportThread)
admin.site.register(WarrantyCustomerSupportMessage)
admin.site.register(WarrantyCustomerConsultationMessage)


@admin.register(WarrantyCustomerProfile)
class WarrantyCustomerProfileAdmin(admin.ModelAdmin):
    list_display = ('full_name', 'phone', 'telegram_user_id', 'consent_version', 'consent_accepted_at', 'consent_revoked_at')
    search_fields = ('full_name', 'phone', 'telegram_user_id', 'username')
    readonly_fields = ('telegram_user_id', 'consent_version', 'consent_text', 'consent_message_id', 'consent_accepted_at', 'created_at', 'updated_at')

    def has_add_permission(self, request):
        return False


@admin.register(WarrantyProductRegistration)
class WarrantyProductRegistrationAdmin(admin.ModelAdmin):
    list_display = ('article', 'serial_number', 'profile', 'purchase_date', 'activated_at')
    search_fields = ('article', 'serial_number', 'profile__full_name', 'profile__phone')
    readonly_fields = ('profile', 'article', 'serial_number', 'purchase_date', 'label_document', 'receipt_document', 'raw_ocr_data', 'activated_at')

    def has_add_permission(self, request):
        return False


@admin.register(WarrantyCustomerBotSettings)
class WarrantyCustomerBotSettingsAdmin(admin.ModelAdmin):
    change_form_template = 'admin/warranty/warrantycustomerbotsettings/change_form.html'
    fields = (
        'bot_token', 'is_enabled', 'support_group_id',
        'ocr_api_key', 'ocr_model', 'product_consultation_enabled', 'yandex_review_url',
        'ocr_space_api_key', 'tesseract_command', 'welcome_text',
        'personal_data_operator', 'personal_data_operator_address', 'privacy_policy_url',
        'consent_withdrawal_contact', 'consent_version', 'consent_text_template',
        'webhook_secret_status', 'webhook_url', 'webhook_registered_at', 'webhook_last_error',
    )
    readonly_fields = ('webhook_secret_status', 'webhook_url', 'webhook_registered_at', 'webhook_last_error')

    @admin.display(description='секрет webhook')
    def webhook_secret_status(self, obj):
        return 'создан автоматически' if obj and obj.webhook_secret_token else 'будет создан кнопкой автоматически'

    def has_add_permission(self, request):
        return not WarrantyCustomerBotSettings.objects.exists()

    def response_change(self, request, obj):
        action = next((name for name in ('_register_webhook', '_check_webhook', '_delete_webhook') if name in request.POST), '')
        if not action:
            return super().response_change(request, obj)
        if not obj.bot_token:
            self.message_user(request, 'Сначала укажите токен клиентского бота.', messages.ERROR)
            return super().response_change(request, obj)

        from warranty.customer_bot import _customer_bot_commands, _telegram

        webhook_url = request.build_absolute_uri(reverse('warranty:customer_bot_webhook')).replace('http://', 'https://', 1)
        try:
            if action == '_register_webhook':
                obj.webhook_secret_token = __import__('secrets').token_urlsafe(32)
                _telegram(obj, 'setWebhook', {
                    'url': webhook_url,
                    'secret_token': obj.webhook_secret_token,
                    'allowed_updates': ['message', 'callback_query'],
                    'drop_pending_updates': False,
                })
                _telegram(obj, 'setMyCommands', {'commands': _customer_bot_commands()})
                obj.webhook_url = webhook_url
                obj.webhook_registered_at = timezone.now()
                obj.webhook_last_error = ''
                self.message_user(request, f'Webhook создан: {webhook_url}', messages.SUCCESS)
            elif action == '_check_webhook':
                info = _telegram(obj, 'getWebhookInfo', {})
                actual_url = str(info.get('url') or '')
                pending = int(info.get('pending_update_count') or 0)
                error = str(info.get('last_error_message') or '')
                obj.webhook_url = actual_url
                obj.webhook_last_error = error
                level = messages.SUCCESS if actual_url == webhook_url and not error else messages.WARNING
                self.message_user(request, f'Webhook: {actual_url or "не зарегистрирован"}. В очереди: {pending}.', level)
            else:
                _telegram(obj, 'deleteWebhook', {'drop_pending_updates': False})
                obj.webhook_url = ''
                obj.webhook_registered_at = None
                obj.webhook_last_error = ''
                self.message_user(request, 'Webhook удалён.', messages.SUCCESS)
            obj.save()
        except (RuntimeError, ValueError) as exc:
            obj.webhook_last_error = str(exc)
            obj.save(update_fields=('webhook_last_error', 'updated_at'))
            self.message_user(request, f'Не удалось выполнить операцию: {exc}', messages.ERROR)
        return super().response_change(request, obj)


@admin.register(WarrantyClaim)
class WarrantyClaimAdmin(admin.ModelAdmin):
    list_display = ('external_id', 'customer_name', 'product_name', 'status', 'updated_at')
    search_fields = ('=external_id', 'customer_name', 'phone', 'product_name', 'serial_number')
    list_filter = ('status', 'priority', 'warranty_type')
    readonly_fields = ('telegram_correspondence',)

    @admin.display(description='Переписка в Telegram')
    def telegram_correspondence(self, obj):
        if not obj or not hasattr(obj, 'telegram_thread'):
            return 'Сообщений нет.'
        messages = obj.telegram_thread.messages.order_by('sent_at', 'id')
        if not messages.exists():
            return 'Сообщений нет.'
        return format_html_join(
            '',
            '<div style="padding:8px 0;border-bottom:1px solid #ddd">'
            '<strong>{} · {}</strong> <small>{}</small><br>{}{}</div>',
            ((
                message.get_direction_display(),
                message.sender_name or 'Telegram',
                message.sent_at.strftime('%d.%m.%Y %H:%M:%S'),
                message.text or '[сообщение без текста]',
                format_html('<br><small>Исходный текст: {}</small>', message.original_text)
                if message.edited_at and message.original_text != message.text else '',
            ) for message in messages),
        )


@admin.register(WarrantyTelegramMessage)
class WarrantyTelegramMessageAdmin(admin.ModelAdmin):
    list_display = ('claim_number', 'sent_at', 'direction', 'sender_name', 'telegram_message_id', 'edited_at')
    list_filter = ('direction',)
    search_fields = ('thread__claim__external_id', 'sender_name', 'telegram_message_id', 'text', 'original_text')
    readonly_fields = ('thread', 'telegram_message_id', 'direction', 'sender_external_id', 'sender_name', 'text', 'original_text', 'payload', 'sent_at', 'edited_at')
    ordering = ('-sent_at', '-id')

    @admin.display(description='Обращение', ordering='thread__claim__external_id')
    def claim_number(self, obj):
        return obj.thread.claim.external_id

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(WarrantyTelegramSettings)
class WarrantyTelegramSettingsAdmin(admin.ModelAdmin):
    fields = ('peer_id', 'use_forum_topics', 'is_enabled')


@admin.register(WarrantyTelegramStatusButton)
class WarrantyTelegramStatusButtonAdmin(admin.ModelAdmin):
    list_display = ('label', 'source_status', 'target_status', 'position', 'is_enabled')
    list_editable = ('position', 'is_enabled')
    list_filter = ('source_status', 'target_status', 'is_enabled')
    search_fields = ('label',)
    ordering = ('source_status', 'position', 'id')

    def save_model(self, request, obj, form, change):
        old_source_status = None
        if change:
            old_source_status = type(obj).objects.filter(pk=obj.pk).values_list(
                'source_status', flat=True,
            ).first()
        super().save_model(request, obj, form, change)

        from warranty.telegram import refresh_claim_buttons_for_statuses

        results = refresh_claim_buttons_for_statuses(
            status for status in (old_source_status, obj.source_status) if status
        )
        self.message_user(
            request,
            'Кнопки Telegram обновлены: {updated}; пропущено: {skipped}; '
            'rate limit: {rate_limited}; ошибок: {failed}.'.format(**results),
            level=messages.WARNING if results['failed'] or results['rate_limited'] else messages.SUCCESS,
        )


@admin.register(WarrantyTelegramStatusIcon)
class WarrantyTelegramStatusIconAdmin(admin.ModelAdmin):
    list_display = ('status', 'emoji', 'custom_emoji_id', 'updated_at')
    list_editable = ('custom_emoji_id',)
    ordering = ('status',)
    readonly_fields = ('status',)

    class StatusIconForm(forms.ModelForm):
        telegram_icon = forms.ChoiceField(
            label='Иконка темы',
            widget=forms.RadioSelect(attrs={'class': 'telegram-emoji-picker'}),
            help_text='Показаны только иконки, которые Telegram разрешает для тем.',
        )

        class Meta:
            model = WarrantyTelegramStatusIcon
            fields = ('telegram_icon',)

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            from checklists.telegram_client import TelegramAPIError
            from warranty.telegram import _forum_topic_icons

            current_id = self.instance.custom_emoji_id if self.instance.pk else ''
            current_emoji = self.instance.emoji if self.instance.pk else ''
            try:
                icons = _forum_topic_icons()
            except TelegramAPIError:
                icons = ((current_emoji, current_id),) if current_id else ()
                self.fields['telegram_icon'].help_text = (
                    'Не удалось получить список иконок из Telegram. '
                    'Попробуйте открыть страницу позже.'
                )
            self.icon_by_id = {icon_id: emoji for emoji, icon_id in icons}
            self.fields['telegram_icon'].choices = [
                (icon_id, emoji) for emoji, icon_id in icons
            ]
            if current_id in self.icon_by_id:
                self.initial['telegram_icon'] = current_id
            elif current_emoji:
                matching_id = next((
                    icon_id for icon_id, emoji in self.icon_by_id.items()
                    if emoji == current_emoji
                ), '')
                if matching_id:
                    self.initial['telegram_icon'] = matching_id

        def save(self, commit=True):
            obj = super().save(commit=False)
            obj.custom_emoji_id = self.cleaned_data['telegram_icon']
            obj.emoji = self.icon_by_id[obj.custom_emoji_id]
            if commit:
                obj.save()
            return obj

    form = StatusIconForm

    class Media:
        css = {'all': ('warranty/admin_status_icon.css',)}

    @staticmethod
    def telegram_icons():
        from checklists.telegram_client import TelegramAPIError
        from warranty.telegram import _forum_topic_icons

        try:
            return _forum_topic_icons()
        except TelegramAPIError:
            return ()

    def formfield_for_dbfield(self, db_field, request, **kwargs):
        if db_field.name == 'custom_emoji_id':
            choices = [('', '—')] + [
                (icon_id, emoji) for emoji, icon_id in self.telegram_icons()
            ]
            return forms.ChoiceField(
                label=db_field.verbose_name,
                required=not db_field.blank,
                choices=choices,
                widget=forms.Select(attrs={
                    'class': 'telegram-emoji-select',
                    'title': 'Выберите иконку Telegram',
                }),
            )
        return super().formfield_for_dbfield(db_field, request, **kwargs)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        if obj.custom_emoji_id:
            emoji_by_id = {
                icon_id: emoji for emoji, icon_id in self.telegram_icons()
            }
            if obj.custom_emoji_id in emoji_by_id:
                obj.emoji = emoji_by_id[obj.custom_emoji_id]
        super().save_model(request, obj, form, change)

        from warranty.telegram import refresh_claim_topic_icons_for_statuses

        results = refresh_claim_topic_icons_for_statuses([obj.status])
        self.message_user(
            request,
            'Иконки Telegram обновлены: {updated}; пропущено: {skipped}; '
            'rate limit: {rate_limited}; ошибок: {failed}.'.format(**results),
            level=messages.WARNING if results['failed'] or results['rate_limited'] else messages.SUCCESS,
        )
