import unicodedata

from django import forms
from django.contrib import admin, messages
from django.http import HttpResponseRedirect
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html, format_html_join

from warranty.models import WarrantyActivity, WarrantyAttachment, WarrantyBitrixOutbox, WarrantyBitrixSyncState, WarrantyClaim, WarrantyCustomerBotSettings, WarrantyCustomerConsultationMessage, WarrantyCustomerDocument, WarrantyCustomerProfile, WarrantyCustomerSession, WarrantyCustomerSupportMessage, WarrantyCustomerSupportThread, WarrantyCustomerUpdate, WarrantyHistoryEvent, WarrantyProductRegistration, WarrantyTelegramMessage, WarrantyTelegramSettings, WarrantyTelegramStatusButton, WarrantyTelegramStatusIcon, WarrantyTelegramThread, WarrantyWorkItem


class HiddenTechnicalAdmin(admin.ModelAdmin):
    """Keep support URLs available while removing technical tables from the menu."""

    def get_model_perms(self, request):
        return {}


class SingletonSettingsAdmin(admin.ModelAdmin):
    """Open the only settings object instead of showing a one-row list."""

    def changelist_view(self, request, extra_context=None):
        obj = self.model._default_manager.order_by('pk').first()
        route = 'change' if obj else 'add'
        args = (obj.pk,) if obj else ()
        url = reverse(
            f'admin:{self.model._meta.app_label}_{self.model._meta.model_name}_{route}',
            args=args,
        )
        return HttpResponseRedirect(url)


for model in (
    WarrantyAttachment, WarrantyHistoryEvent, WarrantyWorkItem,
    WarrantyTelegramThread, WarrantyBitrixOutbox, WarrantyBitrixSyncState,
    WarrantyCustomerSession, WarrantyCustomerDocument, WarrantyCustomerUpdate,
    WarrantyCustomerSupportThread, WarrantyCustomerSupportMessage,
    WarrantyCustomerConsultationMessage,
):
    admin.site.register(model, HiddenTechnicalAdmin)


def activity_rows(claim):
    rows = [
        (event.occurred_at, event.get_kind_display(), event.actor_name, event.text)
        for event in claim.history.all()
    ]
    thread = getattr(claim, 'telegram_thread', None)
    if thread:
        rows.extend(
            (
                message.sent_at,
                'Telegram · ' + message.get_direction_display(),
                message.sender_name,
                message.text or '[сообщение без текста]',
            )
            for message in thread.messages.all()
        )
    rows.extend(
        (item.created_at, 'Синхронизация', '', item.last_error or item.get_status_display())
        for item in claim.bitrix_outbox.all()
        if item.status == WarrantyBitrixOutbox.Status.ERROR
    )
    return sorted(rows, key=lambda row: (row[0], row[1]), reverse=True)


def render_activity(claim):
    rows = activity_rows(claim)
    if not rows:
        return 'История пока пуста.'
    return format_html_join(
        '',
        '<section style="margin:0 0 10px;padding:12px 14px;border-left:4px solid #417690;'
        'border-radius:4px;background:var(--darkened-bg,#f8f8f8)">'
        '<div style="display:flex;gap:10px;justify-content:space-between">'
        '<strong>{}</strong><time style="white-space:nowrap;color:var(--body-quiet-color,#666)">{}</time>'
        '</div><div style="margin-top:5px;white-space:pre-wrap">{}</div>{}</section>',
        ((
            label,
            timezone.localtime(occurred_at).strftime('%d.%m.%Y %H:%M'),
            text,
            format_html('<small style="color:var(--body-quiet-color,#666)">{}</small>', actor)
            if actor else '',
        ) for occurred_at, label, actor, text in rows),
    )


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
class WarrantyCustomerBotSettingsAdmin(SingletonSettingsAdmin):
    change_form_template = 'admin/warranty/warrantycustomerbotsettings/change_form.html'
    fields = (
        'bot_token', 'is_enabled', 'support_group_id',
        'ocr_api_key', 'ocr_model', 'product_consultation_enabled', 'yandex_review_url',
        'ocr_space_api_key', 'tesseract_command', 'welcome_text',
        'personal_data_operator', 'personal_data_operator_address', 'privacy_policy_url',
        'consent_withdrawal_contact', 'consent_version', 'consent_text_template',
        'webhook_secret_status', 'webhook_status', 'webhook_url', 'webhook_registered_at',
    )
    readonly_fields = ('webhook_secret_status', 'webhook_status', 'webhook_url', 'webhook_registered_at')

    @admin.display(description='секрет webhook')
    def webhook_secret_status(self, obj):
        return 'создан автоматически' if obj and obj.webhook_secret_token else 'будет создан кнопкой автоматически'

    @admin.display(description='статус webhook')
    def webhook_status(self, obj):
        if not obj or not obj.bot_token:
            color, label, details = '#ba2121', 'Не настроен', 'Не указан токен бота.'
        elif not obj.is_enabled:
            color, label, details = '#777', 'Выключен', 'Клиентский бот выключен.'
        elif obj.webhook_last_error:
            color, label, details = '#ba2121', 'Есть ошибка', obj.webhook_last_error
        elif not obj.webhook_url:
            color, label, details = '#ba7d00', 'Не зарегистрирован', 'Создайте webhook.'
        else:
            color, label = '#2b8a3e', 'Работает'
            details = f'В очереди: {obj.webhook_pending_updates}.'
        checked = timezone.localtime(obj.webhook_checked_at).strftime('%d.%m.%Y %H:%M') if obj and obj.webhook_checked_at else 'ещё не проверялся'
        return format_html(
            '<span style="display:inline-block;margin-right:12px;padding:5px 10px;border-radius:12px;'
            'background:{};color:white;font-weight:600">{}</span>'
            '<button type="submit" name="_check_webhook" class="button">Проверить</button>'
            '<div style="margin-top:7px">{} <small style="color:var(--body-quiet-color,#666)">'
            'Проверен: {}</small></div>',
            color, label, details, checked,
        )

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
                obj.webhook_checked_at = timezone.now()
                obj.webhook_pending_updates = 0
                obj.webhook_last_error = ''
                self.message_user(request, f'Webhook создан: {webhook_url}', messages.SUCCESS)
            elif action == '_check_webhook':
                info = _telegram(obj, 'getWebhookInfo', {})
                actual_url = str(info.get('url') or '')
                pending = int(info.get('pending_update_count') or 0)
                error = str(info.get('last_error_message') or '')
                obj.webhook_url = actual_url
                obj.webhook_checked_at = timezone.now()
                obj.webhook_pending_updates = pending
                obj.webhook_last_error = error if pending else ''
                level = messages.SUCCESS if actual_url == webhook_url and not obj.webhook_last_error else messages.WARNING
                self.message_user(request, f'Webhook: {actual_url or "не зарегистрирован"}. В очереди: {pending}.', level)
            else:
                _telegram(obj, 'deleteWebhook', {'drop_pending_updates': False})
                obj.webhook_url = ''
                obj.webhook_registered_at = None
                obj.webhook_checked_at = timezone.now()
                obj.webhook_pending_updates = 0
                obj.webhook_last_error = ''
                self.message_user(request, 'Webhook удалён.', messages.SUCCESS)
            obj.save()
        except (RuntimeError, ValueError) as exc:
            obj.webhook_checked_at = timezone.now()
            obj.webhook_last_error = str(exc)
            obj.save(update_fields=('webhook_checked_at', 'webhook_last_error', 'updated_at'))
            self.message_user(request, f'Не удалось выполнить операцию: {exc}', messages.ERROR)
        return super().response_change(request, obj)


@admin.register(WarrantyClaim)
class WarrantyClaimAdmin(admin.ModelAdmin):
    list_display = ('external_id', 'customer_name', 'product_name', 'status', 'updated_at')
    search_fields = ('=external_id', 'customer_name', 'phone', 'product_name', 'serial_number')
    list_filter = ('status', 'priority', 'warranty_type')
    readonly_fields = ('work_history',)

    @admin.display(description='Единая история работы')
    def work_history(self, obj):
        return render_activity(obj) if obj else 'История появится после сохранения.'

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


@admin.register(WarrantyActivity)
class WarrantyActivityAdmin(admin.ModelAdmin):
    list_display = ('claim_number', 'customer_name', 'product_name', 'status', 'updated_at')
    list_filter = ('status',)
    search_fields = ('=external_id', 'customer_name', 'phone', 'product_name', 'serial_number')
    ordering = ('-updated_at',)
    fields = ('claim_link', 'customer_name', 'product_name', 'status', 'work_history')
    readonly_fields = fields

    def get_queryset(self, request):
        return super().get_queryset(request).prefetch_related(
            'history', 'bitrix_outbox', 'telegram_thread__messages',
        )

    @admin.display(description='Обращение', ordering='external_id')
    def claim_number(self, obj):
        url = reverse('admin:warranty_warrantyclaim_change', args=(obj.pk,))
        return format_html('<a href="{}">#{}</a>', url, obj.external_id)

    @admin.display(description='Карточка обращения')
    def claim_link(self, obj):
        return self.claim_number(obj)

    @admin.display(description='Хронология')
    def work_history(self, obj):
        return render_activity(obj)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_view_permission(self, request, obj=None):
        return request.user.has_perm('warranty.view_warrantyclaim')

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(WarrantyTelegramMessage)
class WarrantyTelegramMessageAdmin(admin.ModelAdmin):
    list_display = ('claim_number', 'sent_at', 'direction', 'sender_name', 'telegram_message_id', 'edited_at')
    list_filter = ('direction',)
    search_fields = ('thread__claim__external_id', 'sender_name', 'telegram_message_id', 'text', 'original_text')
    readonly_fields = ('thread', 'telegram_message_id', 'direction', 'sender_external_id', 'sender_name', 'text', 'original_text', 'payload', 'sent_at', 'edited_at')
    ordering = ('-sent_at', '-id')

    def get_model_perms(self, request):
        return {}

    @admin.display(description='Обращение', ordering='thread__claim__external_id')
    def claim_number(self, obj):
        return obj.thread.claim.external_id

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(WarrantyTelegramSettings)
class WarrantyTelegramSettingsAdmin(SingletonSettingsAdmin):
    fields = (
        'peer_id', 'use_forum_topics', 'closed_topic_retention_days',
        'is_enabled',
    )


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

    EMOJI_SEARCH_ALIASES = {
        '🤖': 'робот бот robot bot',
        '🛠': 'инструмент ремонт молоток ключ tools repair hammer wrench',
        '🔧': 'инструмент ремонт ключ wrench tool repair',
        '🔨': 'инструмент ремонт молоток hammer tool repair',
        '🔍': 'поиск диагностика лупа search diagnostics magnifier',
        '🔎': 'поиск диагностика лупа search diagnostics magnifier',
        '🛍': 'покупки магазин сумка пакет shopping store bag',
        '👜': 'сумка покупки bag shopping',
        '💼': 'портфель работа кейс briefcase work case',
        '🛒': 'тележка покупки магазин cart shopping store',
        '🚂': 'поезд транспорт train transport',
        '🚗': 'машина автомобиль транспорт car auto transport',
        '✈': 'самолет транспорт путешествие plane travel transport',
        '🚢': 'корабль транспорт ship transport',
        '📦': 'коробка посылка доставка запчасти box parcel delivery parts',
        '👤': 'клиент человек пользователь person customer user',
        '👥': 'люди клиенты группа people customers group',
        '✅': 'готово да галочка успех ready yes check success',
        '❓': 'вопрос решение помощь question decision help',
        '❗': 'важно внимание ошибка important warning error',
        '🆕': 'новый new',
        '🔒': 'закрыто замок closed lock',
        '⭐': 'звезда избранное star favorite',
        '❤️': 'сердце любовь heart love',
        '🎓': 'учеба образование выпускник study education graduate',
        '🎤': 'микрофон музыка голос microphone music voice',
        '🎵': 'музыка нота music note',
        '🧪': 'тест лаборатория диагностика test lab diagnostics',
        '🏕': 'палатка туризм отдых tent camping travel',
        '🦄': 'единорог unicorn',
    }

    class SearchableEmojiSelect(forms.Select):
        def __init__(self, *args, search_aliases=None, **kwargs):
            self.search_aliases = search_aliases or {}
            super().__init__(*args, **kwargs)

        def create_option(
            self, name, value, label, selected, index, subindex=None, attrs=None,
        ):
            option = super().create_option(
                name, value, label, selected, index, subindex=subindex, attrs=attrs,
            )
            emoji = str(label) if value else ''
            unicode_names = ' '.join(
                unicodedata.name(character, '') for character in emoji
            ).lower()
            option['attrs']['data-search'] = ' '.join(filter(None, (
                emoji,
                unicode_names,
                self.search_aliases.get(emoji, ''),
            )))
            return option

    class Media:
        css = {'all': ('warranty/admin_status_icon.css',)}
        js = ('warranty/admin_status_icon.js',)

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
                widget=self.SearchableEmojiSelect(
                    search_aliases=self.EMOJI_SEARCH_ALIASES,
                    attrs={
                        'class': 'telegram-emoji-select',
                        'title': 'Выберите иконку Telegram',
                    },
                ),
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
