from django import forms
from django.contrib import admin, messages
from django.utils.html import format_html, format_html_join

from warranty.models import WarrantyAttachment, WarrantyBitrixOutbox, WarrantyBitrixSyncState, WarrantyClaim, WarrantyHistoryEvent, WarrantyTelegramMessage, WarrantyTelegramSettings, WarrantyTelegramStatusButton, WarrantyTelegramStatusIcon, WarrantyTelegramThread, WarrantyWorkItem

for model in (WarrantyAttachment, WarrantyHistoryEvent, WarrantyWorkItem, WarrantyTelegramThread, WarrantyBitrixOutbox, WarrantyBitrixSyncState):
    admin.site.register(model)


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
    list_display = ('status', 'emoji', 'updated_at')
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

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)

        from warranty.telegram import refresh_claim_topic_icons_for_statuses

        results = refresh_claim_topic_icons_for_statuses([obj.status])
        self.message_user(
            request,
            'Иконки Telegram обновлены: {updated}; пропущено: {skipped}; '
            'rate limit: {rate_limited}; ошибок: {failed}.'.format(**results),
            level=messages.WARNING if results['failed'] or results['rate_limited'] else messages.SUCCESS,
        )
