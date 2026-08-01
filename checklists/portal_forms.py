from urllib import parse

from django import forms
from django.contrib.auth import get_user_model
from django.db.models import Q

from checklists.models import (
    ChecklistItem,
    DailyChecklistStage,
    DailyShiftAssignment,
    EmployeeProfile,
    Store,
    StoreChecklistSchedule,
    StoreDayStatus,
    StoreEmployee,
    StoreNotificationSettings,
    StorePriceTagTemplate,
    StorePriceTagCategory,
    StoreAdHocTask,
    ShiftTemplate,
    TelegramMessageTemplate,
    TelegramStoreChat,
    TelegramSystemSettings,
    UserStoreMembership,
)
from checklists.telegram_templates import (
    validate_template_source,
)
from checklists.telegram_events import (
    TELEGRAM_EVENT_CHOICES,
    get_telegram_event,
)


class AuditClearConfirmationForm(forms.Form):
    confirmation = forms.CharField(
        label='Текст подтверждения',
        max_length=100,
        widget=forms.TextInput(attrs={'autocomplete': 'off'}),
    )

    def __init__(self, *args, expected_phrase, **kwargs):
        super().__init__(*args, **kwargs)
        self.expected_phrase = expected_phrase

    def clean_confirmation(self):
        value = self.cleaned_data['confirmation'].strip()
        if value.casefold() != self.expected_phrase.casefold():
            raise forms.ValidationError(
                f'Введите точную фразу: {self.expected_phrase}'
            )
        return value


class StoreEmployeeForm(forms.ModelForm):
    class Meta:
        model = StoreEmployee
        fields = (
            'first_name',
            'last_name',
            'display_name',
            'position',
            'department',
            'personnel_number',
            'user',
            'sort_order',
        )

    def __init__(self, *args, store, **kwargs):
        super().__init__(*args, **kwargs)
        user_ids = UserStoreMembership.objects.filter(
            store=store,
            is_active=True,
            user__is_active=True,
        ).values_list('user_id', flat=True)
        self.fields['user'].queryset = get_user_model().objects.filter(
            Q(pk__in=user_ids) | Q(pk=self.instance.user_id)
        ).distinct().order_by('username')
        self.fields['user'].required = False
        self.fields['user'].help_text = (
            'Нужно для просмотра личного графика сотрудником.'
        )
        self.fields['department'].required = False

    def clean_department(self):
        return (
            self.cleaned_data.get('department')
            or self.instance.department
            or StoreEmployee.Department.STORE
        )


class ShiftAssignmentForm(forms.ModelForm):
    class Meta:
        model = DailyShiftAssignment
        fields = (
            'employee',
            'shift_type',
            'is_responsible_for_checklist',
            'shift_start',
            'shift_end',
            'comment',
        )
        widgets = {
            'shift_start': forms.TimeInput(attrs={'type': 'time'}),
            'shift_end': forms.TimeInput(attrs={'type': 'time'}),
        }

    def __init__(self, *args, store, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance.store = store
        self.fields['employee'].queryset = StoreEmployee.objects.filter(
            store=store,
            is_active=True,
        )
        # Старые формы и внешние клиенты не передавали тип смены.
        self.fields['shift_type'].required = False

    def clean_shift_type(self):
        return (
            self.cleaned_data.get('shift_type')
            or self.instance.shift_type
            or DailyShiftAssignment.ShiftType.WORK
        )


class ShiftCopyForm(forms.Form):
    target_date = forms.DateField(
        label='Скопировать на дату',
        widget=forms.DateInput(attrs={'type': 'date'}),
    )


class ShiftTemplateForm(forms.ModelForm):
    class Meta:
        model = ShiftTemplate
        fields = (
            'name',
            'shift_type',
            'shift_start',
            'shift_end',
            'sort_order',
            'is_active',
        )
        widgets = {
            'shift_start': forms.TimeInput(attrs={'type': 'time'}),
            'shift_end': forms.TimeInput(attrs={'type': 'time'}),
        }


class StoreAdHocTaskForm(forms.Form):
    store = forms.ModelChoiceField(
        label='Магазин',
        queryset=Store.objects.none(),
        required=False,
        widget=forms.Select(attrs={'class': 'form-select'}),
    )
    date = forms.DateField(
        label='Дата',
        widget=forms.DateInput(attrs={'type': 'date', 'class': 'form-control'}),
    )
    section_code = forms.ChoiceField(
        label='Этап',
        choices=StoreAdHocTask.SectionCode.choices,
        widget=forms.Select(attrs={'class': 'form-select'}),
    )
    text = forms.CharField(
        label='Текст задачи',
        max_length=2000,
        widget=forms.Textarea(attrs={'rows': 3, 'class': 'form-control'}),
    )
    description = forms.CharField(
        label='Описание',
        required=False,
        widget=forms.Textarea(attrs={'rows': 5, 'class': 'form-control'}),
    )
    is_required = forms.BooleanField(
        label='Обязательная задача',
        required=False,
        initial=True,
        widget=forms.CheckboxInput(attrs={'class': 'form-check-input'}),
    )
    confirmation = forms.BooleanField(
        label='Подтверждаю создание или изменение задачи',
        required=True,
        widget=forms.CheckboxInput(attrs={'class': 'form-check-input'}),
    )

    def __init__(self, *args, store_queryset=None, **kwargs):
        super().__init__(*args, **kwargs)
        if store_queryset is None:
            self.fields.pop('store')
        else:
            self.fields['store'].queryset = store_queryset
            self.fields['store'].required = True

    @classmethod
    def initial_from_task(cls, task):
        return {
            'store': task.store,
            'date': task.date,
            'section_code': task.section_code,
            'text': task.text,
            'description': task.description,
            'is_required': task.is_required,
        }


class StoreAdHocTaskCopyForm(forms.Form):
    target_store = forms.ModelChoiceField(
        label='Новый магазин',
        queryset=Store.objects.none(),
        widget=forms.Select(attrs={'class': 'form-select'}),
    )
    date = forms.DateField(
        label='Дата',
        widget=forms.DateInput(attrs={'type': 'date', 'class': 'form-control'}),
    )
    confirmation = forms.BooleanField(
        label='Подтверждаю создание копии задачи',
        required=True,
        widget=forms.CheckboxInput(attrs={'class': 'form-check-input'}),
    )

    def __init__(self, *args, source_store, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['target_store'].queryset = Store.objects.filter(
            is_active=True,
        ).exclude(pk=source_store.pk).order_by('name')


class BulkShiftForm(forms.Form):
    MODE_CREATE = 'create'
    MODE_UPDATE = 'update'

    start_date = forms.DateField(
        label='Дата начала',
        widget=forms.DateInput(attrs={'type': 'date'}),
    )
    end_date = forms.DateField(
        label='Дата окончания',
        widget=forms.DateInput(attrs={'type': 'date'}),
    )
    weekdays = forms.MultipleChoiceField(
        label='Дни недели',
        choices=(
            ('0', 'Понедельник'),
            ('1', 'Вторник'),
            ('2', 'Среда'),
            ('3', 'Четверг'),
            ('4', 'Пятница'),
            ('5', 'Суббота'),
            ('6', 'Воскресенье'),
        ),
        widget=forms.CheckboxSelectMultiple,
    )
    employees = forms.ModelMultipleChoiceField(
        label='Сотрудники',
        queryset=StoreEmployee.objects.none(),
        widget=forms.CheckboxSelectMultiple,
    )
    shift_start = forms.TimeField(
        label='Начало смены',
        required=False,
        widget=forms.TimeInput(attrs={'type': 'time'}),
    )
    shift_end = forms.TimeField(
        label='Окончание смены',
        required=False,
        widget=forms.TimeInput(attrs={'type': 'time'}),
    )
    is_responsible_for_checklist = forms.BooleanField(
        label='Ответственный за чек-лист',
        required=False,
        initial=True,
    )
    shift_type = forms.ChoiceField(
        label='Тип смены',
        choices=DailyShiftAssignment.ShiftType.choices,
        initial=DailyShiftAssignment.ShiftType.WORK,
        required=False,
    )
    comment = forms.CharField(
        label='Комментарий',
        required=False,
        widget=forms.Textarea(attrs={'rows': 2}),
    )
    mode = forms.ChoiceField(
        label='Режим',
        choices=(
            (MODE_CREATE, 'Только создать отсутствующие'),
            (MODE_UPDATE, 'Создать или обновить'),
        ),
    )

    def __init__(self, *args, store, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['employees'].queryset = StoreEmployee.objects.filter(
            store=store,
            is_active=True,
        )

    def clean(self):
        data = super().clean()
        data['shift_type'] = (
            data.get('shift_type') or DailyShiftAssignment.ShiftType.WORK
        )
        if data.get('start_date') and data.get('end_date'):
            if data['start_date'] > data['end_date']:
                self.add_error('end_date', 'Конец диапазона раньше начала.')
            if (data['end_date'] - data['start_date']).days > 366:
                self.add_error('end_date', 'Диапазон не может превышать 366 дней.')
        if data.get('shift_start') and data.get('shift_end'):
            if data['shift_start'] >= data['shift_end']:
                self.add_error('shift_end', 'Ночные смены не поддерживаются.')
        return data


class ChecklistQuestionForm(forms.Form):
    text = forms.CharField(label='Текст вопроса', widget=forms.Textarea(attrs={'rows': 3}))
    description = forms.CharField(
        label='Описание или инструкция',
        required=False,
        widget=forms.Textarea(attrs={'rows': 3}),
    )
    section_code = forms.ChoiceField(
        label='Этап',
        choices=DailyChecklistStage.SectionCode.choices,
    )
    is_required = forms.BooleanField(label='Обязателен', required=False, initial=True)
    answer_type = forms.ChoiceField(
        label='Тип ответа',
        choices=ChecklistItem.AnswerType.choices,
        initial=ChecklistItem.AnswerType.STATUS,
        required=False,
    )
    allow_not_applicable = forms.BooleanField(
        label='Разрешить «Не применимо»',
        required=False,
    )
    comment_required_on_failure = forms.BooleanField(
        label='Требовать комментарий при «Не выполнено»',
        required=False,
        initial=True,
    )
    sort_order = forms.IntegerField(label='Исходный порядок', min_value=0, initial=0)
    is_active = forms.BooleanField(label='Активен', required=False, initial=True)
    effective_from = forms.DateField(
        label='Действует с',
        required=False,
        widget=forms.DateInput(attrs={'type': 'date'}),
    )
    effective_until = forms.DateField(
        label='Действует до',
        required=False,
        widget=forms.DateInput(attrs={'type': 'date'}),
    )

    def clean(self):
        data = super().clean()
        data['answer_type'] = (
            data.get('answer_type') or ChecklistItem.AnswerType.STATUS
        )
        if data.get('answer_type') == ChecklistItem.AnswerType.INTEGER:
            data['allow_not_applicable'] = False
            data['comment_required_on_failure'] = False
        if data.get('effective_from') and data.get('effective_until'):
            if data['effective_from'] > data['effective_until']:
                self.add_error('effective_until', 'Окончание раньше начала.')
        return data

    @classmethod
    def initial_from_item(cls, item):
        return {
            'text': item.text,
            'description': item.description,
            'section_code': item.section.code,
            'is_required': item.is_required,
            'answer_type': item.answer_type,
            'allow_not_applicable': item.allow_not_applicable,
            'comment_required_on_failure': item.comment_required_on_failure,
            'sort_order': item.sort_order,
            'is_active': item.is_active,
            'effective_from': item.effective_from,
            'effective_until': item.effective_until,
        }


def format_completion_window(minutes):
    if minutes == 0:
        return 'Сразу после открытия'
    hours, remaining_minutes = divmod(minutes, 60)
    parts = []
    if hours:
        if hours % 10 == 1 and hours % 100 != 11:
            unit = 'час'
        elif hours % 10 in {2, 3, 4} and hours % 100 not in {12, 13, 14}:
            unit = 'часа'
        else:
            unit = 'часов'
        parts.append(f'{hours} {unit}')
    if remaining_minutes:
        parts.append(f'{remaining_minutes} минут')
    return ' '.join(parts)


class StoreScheduleForm(forms.ModelForm):

    COMPLETION_WINDOW_CHOICES = tuple(
        (minutes, format_completion_window(minutes))
        for minutes in range(0, 721, 15)
    )

    WEEKDAY_CHOICES = (
        ('0', 'Понедельник'),
        ('1', 'Вторник'),
        ('2', 'Среда'),
        ('3', 'Четверг'),
        ('4', 'Пятница'),
        ('5', 'Суббота'),
        ('6', 'Воскресенье'),
    )
    working_weekdays = forms.MultipleChoiceField(
        label='Рабочие дни недели',
        choices=WEEKDAY_CHOICES,
        widget=forms.CheckboxSelectMultiple,
        required=False,
        help_text='В остальные дни чек-лист не требуется.',
    )

    class Meta:
        model = StoreChecklistSchedule
        fields = (
            'opening_time',
            'morning_deadline',
            'daytime_deadline',
            'closing_deadline',
            'morning_completion_window_minutes',
            'day_completion_window_minutes',
            'evening_completion_window_minutes',
            'warning_minutes_before',
            'notifications_enabled',
            'working_weekdays',
            'is_active',
        )
        labels = {
            'opening_time': 'Начало рабочего дня',
            'morning_deadline': 'Утренние задачи выполнить до',
            'daytime_deadline': 'Дневные задачи выполнить до',
            'closing_deadline': 'Вечерние задачи выполнить до',
            'morning_completion_window_minutes': (
                'Утро'
            ),
            'day_completion_window_minutes': (
                'День'
            ),
            'evening_completion_window_minutes': (
                'Вечер'
            ),
            'warning_minutes_before': 'Предупредить за N минут',
            'notifications_enabled': 'Отправлять уведомления',
            'is_active': 'Расписание активно',
        }
        widgets = {
            'opening_time': forms.TimeInput(attrs={'type': 'time'}),
            'morning_deadline': forms.TimeInput(attrs={'type': 'time'}),
            'daytime_deadline': forms.TimeInput(attrs={'type': 'time'}),
            'closing_deadline': forms.TimeInput(attrs={'type': 'time'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field_name in (
            'morning_completion_window_minutes',
            'day_completion_window_minutes',
            'evening_completion_window_minutes',
        ):
            self.fields[field_name].required = False
            self.fields[field_name].widget = forms.Select(
                choices=self.COMPLETION_WINDOW_CHOICES,
            )
        if self.instance and self.instance.pk:
            self.initial['working_weekdays'] = [
                str(value) for value in self.instance.working_weekdays
            ]

    def clean(self):
        cleaned_data = super().clean()
        for field_name in (
            'morning_completion_window_minutes',
            'day_completion_window_minutes',
            'evening_completion_window_minutes',
        ):
            if field_name not in self.data:
                cleaned_data[field_name] = getattr(
                    self.instance,
                    field_name,
                    120,
                )
            elif cleaned_data.get(field_name) is None:
                self.add_error(
                    field_name,
                    'Выберите интервал от 0 до 720 минут.',
                )
        return cleaned_data

    def clean_working_weekdays(self):
        values = self.cleaned_data['working_weekdays']
        if not values and 'working_weekdays' not in self.data:
            return list(self.instance.working_weekdays)
        if not values:
            raise forms.ValidationError(
                'Выберите хотя бы один рабочий день.'
            )
        return sorted(int(value) for value in values)


class StoreDayStatusForm(forms.ModelForm):
    class Meta:
        model = StoreDayStatus
        fields = ('date', 'status', 'comment')
        widgets = {
            'date': forms.DateInput(attrs={'type': 'date'}),
            'comment': forms.Textarea(attrs={'rows': 2}),
        }


class StoreLogoForm(forms.ModelForm):
    class Meta:
        model = Store
        fields = ('logo',)


class StoreNotificationForm(forms.ModelForm):
    class Meta:
        model = StoreNotificationSettings
        fields = (
            'telegram_chat_id',
            'warning_enabled',
            'overdue_enabled',
            'completed_late_enabled',
            'is_active',
        )


class TelegramTestForm(forms.Form):
    confirm = forms.BooleanField(
        label='Подтверждаю отправку тестового сообщения',
    )


class TelegramSystemSettingsForm(forms.ModelForm):
    new_token = forms.CharField(
        label='Новый токен',
        required=False,
        widget=forms.PasswordInput(render_value=False),
        help_text='Оставьте пустым, чтобы сохранить текущий токен.',
    )
    clear_token = forms.BooleanField(
        label='Удалить сохранённый токен',
        required=False,
    )
    new_webhook_secret = forms.CharField(
        label='Новый secret token webhook',
        required=False,
        widget=forms.PasswordInput(render_value=False),
        help_text='Оставьте пустым, чтобы сохранить текущий secret.',
    )
    clear_webhook_secret = forms.BooleanField(
        label='Удалить secret token webhook',
        required=False,
    )

    class Meta:
        model = TelegramSystemSettings
        fields = (
            'alternative_api_base_url',
            'use_alternative_gateway',
            'fallback_to_official_api',
            'alternative_attempts',
            'official_attempts',
            'request_timeout_seconds',
            'retry_delay_seconds',
            'is_enabled',
            'incoming_mode',
            'webhook_max_connections',
            'webhook_allowed_updates',
            'immediate_ack_enabled',
            'immediate_ack_text',
        )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in (
            'incoming_mode',
            'webhook_max_connections',
            'webhook_allowed_updates',
            'immediate_ack_enabled',
            'immediate_ack_text',
        ):
            self.fields[name].required = False

    def clean(self):
        data = super().clean()
        for name in (
            'incoming_mode',
            'webhook_max_connections',
            'webhook_allowed_updates',
            'immediate_ack_text',
        ):
            if data.get(name) in (None, ''):
                data[name] = getattr(self.instance, name)
        if 'immediate_ack_enabled' not in self.data:
            data['immediate_ack_enabled'] = self.instance.immediate_ack_enabled
        if data.get('new_token') and data.get('clear_token'):
            raise forms.ValidationError(
                'Нельзя одновременно заменить и удалить токен.'
            )
        if data.get('new_webhook_secret') and data.get('clear_webhook_secret'):
            raise forms.ValidationError(
                'Нельзя одновременно заменить и удалить webhook secret.'
            )
        return data


class TelegramStoreChatForm(forms.ModelForm):
    class Meta:
        model = TelegramStoreChat
        fields = (
            'title',
            'chat_id',
            'chat_type',
            'message_thread_id',
            'purpose',
            'is_active',
        )

    def __init__(self, *args, store, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance.store = store
        self.fields['chat_id'].help_text = (
            'Добавьте бота в группу и узнайте chat_id через getUpdates. '
            'Для Telegram Topic укажите message_thread_id.'
        )


class TelegramMessageTemplateForm(forms.ModelForm):
    class Meta:
        model = TelegramMessageTemplate
        fields = (
            'name',
            'title',
            'body',
            'parse_mode',
            'is_enabled',
            'send_to_private',
            'send_to_group',
        )
        widgets = {
            'title': forms.Textarea(attrs={'rows': 2}),
            'body': forms.Textarea(attrs={'rows': 10}),
        }

    def __init__(self, *args, event_code=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.event_code = event_code or self.instance.event_code
        for field in self.fields.values():
            if isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs['class'] = 'form-check-input'
            else:
                field.widget.attrs['class'] = 'form-control'

    def clean_title(self):
        return validate_template_source(
            self.cleaned_data['title'],
            self.event_code,
        )

    def clean_body(self):
        return validate_template_source(
            self.cleaned_data['body'],
            self.event_code,
        )

    @property
    def available_variables(self):
        return get_telegram_event(self.event_code).variables


class TelegramMessageTemplateCreateForm(TelegramMessageTemplateForm):
    event_code = forms.ChoiceField(
        label='Событие',
        choices=TELEGRAM_EVENT_CHOICES,
    )

    class Meta(TelegramMessageTemplateForm.Meta):
        fields = (
            'event_code',
            *TelegramMessageTemplateForm.Meta.fields,
        )

    def __init__(self, *args, store, event_code=None, **kwargs):
        super().__init__(*args, event_code=event_code, **kwargs)
        self.store = store
        self.instance.store = store
        used = set(
            TelegramMessageTemplate.objects.filter(store=store).values_list(
                'event_code',
                flat=True,
            )
        )
        selected = (
            self.data.get('event_code')
            if self.is_bound
            else event_code or self.initial.get('event_code')
        )
        self.fields['event_code'].choices = tuple(
            choice
            for choice in TELEGRAM_EVENT_CHOICES
            if choice[0] not in used or choice[0] == selected
        )
        self.fields['event_code'].widget.attrs['class'] = 'form-select'

    def clean_event_code(self):
        event_code = self.cleaned_data['event_code']
        self.event_code = event_code
        if TelegramMessageTemplate.objects.filter(
            store=self.store,
            event_code=event_code,
        ).exists():
            raise forms.ValidationError(
                'Для этого события уже создан шаблон магазина.'
            )
        return event_code


class TelegramBindingApprovalForm(forms.Form):
    store = forms.ModelChoiceField(
        label='Магазин',
        queryset=Store.objects.filter(is_active=True).order_by('name'),
    )
    user = forms.ModelChoiceField(
        label='Пользователь сайта',
        queryset=get_user_model().objects.filter(is_active=True).order_by(
            'username'
        ),
        required=False,
        help_text='Нужен для создания задач от имени пользователя.',
    )


class TelegramProfileUserForm(forms.Form):
    user = forms.ModelChoiceField(
        label='Пользователь сайта',
        queryset=get_user_model().objects.filter(is_active=True).order_by(
            'username'
        ),
    )


class UserStoreMembershipForm(forms.ModelForm):
    class Meta:
        model = UserStoreMembership
        fields = ('store', 'role_in_store', 'is_active')

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        self.fields['store'].queryset = Store.objects.filter(
            is_active=True
        ).order_by('name')

    def clean_store(self):
        store = self.cleaned_data['store']
        if self.user and UserStoreMembership.objects.filter(
            user=self.user,
            store=store,
        ).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError(
                'Пользователь уже связан с этим магазином.'
            )
        return store


class TelegramQueueRetryForm(forms.Form):
    confirm = forms.BooleanField(label='Повторить отправку')


class ReopenStageForm(forms.Form):
    reason = forms.CharField(
        label='Причина повторного открытия',
        min_length=5,
        widget=forms.Textarea(attrs={'rows': 3}),
    )


class PriceTagLinksForm(forms.Form):
    urls = forms.CharField(
        label='Ссылки на товары',
        help_text='Каждая ссылка с новой строки. Максимум 20 товаров.',
        widget=forms.Textarea(attrs={
            'class': 'form-control',
            'rows': 7,
            'placeholder': 'https://example.ru/product/123/\nhttps://example.ru/product/456/',
        }),
    )

    def clean_urls(self):
        urls = [line.strip() for line in self.cleaned_data['urls'].splitlines()]
        urls = list(dict.fromkeys(line for line in urls if line))
        if not urls:
            raise forms.ValidationError('Добавьте хотя бы одну ссылку.')
        if len(urls) > 20:
            raise forms.ValidationError('За один раз можно создать не более 20 ценников.')
        return urls


class StorePriceTagTemplateForm(forms.ModelForm):
    def clean_qr_utm_parameters(self):
        value = self.cleaned_data['qr_utm_parameters'].strip().lstrip('?')
        if not value:
            return ''
        pairs = parse.parse_qsl(value, keep_blank_values=True)
        if not pairs or any(not key.startswith('utm_') for key, _ in pairs):
            raise forms.ValidationError(
                'Укажите UTM-параметры в формате '
                'utm_source=price_tag&utm_medium=offline.'
            )
        return parse.urlencode(pairs)

    class Meta:
        model = StorePriceTagTemplate
        fields = (
            'logo', 'heading', 'primary_color', 'accent_color', 'show_image',
            'show_sku', 'show_properties', 'max_properties', 'footer',
            'qr_utm_parameters', 'print_mode',
        )
        widgets = {
            'logo': forms.FileInput(attrs={'class': 'form-control'}),
            'heading': forms.TextInput(attrs={'class': 'form-control'}),
            'primary_color': forms.TextInput(attrs={
                'type': 'color', 'class': 'form-control form-control-color',
            }),
            'accent_color': forms.TextInput(attrs={
                'type': 'color', 'class': 'form-control form-control-color',
            }),
            'max_properties': forms.NumberInput(attrs={'class': 'form-control'}),
            'footer': forms.TextInput(attrs={'class': 'form-control'}),
            'qr_utm_parameters': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'utm_source=price_tag&utm_medium=offline',
            }),
            'print_mode': forms.Select(attrs={'class': 'form-select'}),
        }


class StorePriceTagCategoryForm(forms.ModelForm):
    property_names = forms.MultipleChoiceField(
        label='Свойства на ценнике',
        required=False,
        widget=forms.CheckboxSelectMultiple,
        help_text='Отметьте свойства, которые нужно печатать.',
    )

    def __init__(self, *args, available_names=(), **kwargs):
        super().__init__(*args, **kwargs)
        selected = self.instance.property_name_list if self.instance.pk else []
        names = list(dict.fromkeys([*available_names, *selected]))
        self.fields['property_names'].choices = [(name, name) for name in names]
        self.initial['property_names'] = selected

    def clean_property_names(self):
        return '\n'.join(self.cleaned_data['property_names'])

    class Meta:
        model = StorePriceTagCategory
        fields = ('name', 'keywords', 'property_names', 'sort_order', 'is_active')
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control'}),
            'keywords': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'газонокосилка, lawn mower',
            }),
            'sort_order': forms.NumberInput(attrs={'class': 'form-control'}),
        }


class StoreForm(forms.ModelForm):
    class Meta:
        model = Store
        fields = ('name', 'code', 'timezone', 'logo', 'is_active')


class StoreCreateForm(StoreForm):
    terminal_username = forms.CharField(
        label='Логин аккаунта магазина',
        required=False,
    )
    terminal_password = forms.CharField(
        label='Пароль',
        required=False,
        widget=forms.PasswordInput,
    )
    terminal_password_confirmation = forms.CharField(
        label='Подтверждение пароля',
        required=False,
        widget=forms.PasswordInput,
    )

    def clean(self):
        data = super().clean()
        values = (
            data.get('terminal_username'),
            data.get('terminal_password'),
            data.get('terminal_password_confirmation'),
        )
        if any(values) and not all(values):
            raise forms.ValidationError(
                'Для аккаунта магазина заполните логин и оба поля пароля.'
            )
        if values[1] and values[1] != values[2]:
            self.add_error('terminal_password_confirmation', 'Пароли не совпадают.')
        return data


class ManagedUserForm(forms.Form):
    username = forms.CharField(label='Логин', max_length=150)
    first_name = forms.CharField(label='Имя', max_length=150, required=False)
    last_name = forms.CharField(label='Фамилия', max_length=150, required=False)
    email = forms.EmailField(label='Email', required=False)
    role = forms.ChoiceField(label='Роль', choices=EmployeeProfile.Role.choices)
    store = forms.ModelChoiceField(
        label='Магазин',
        queryset=Store.objects.filter(is_active=True),
        required=False,
    )
    is_active = forms.BooleanField(label='Активен', required=False, initial=True)

    def clean(self):
        data = super().clean()
        role = data.get('role')
        store = data.get('store')
        if role in {
            EmployeeProfile.Role.STORE_ACCOUNT,
            EmployeeProfile.Role.STORE_DIRECTOR,
        } and store is None:
            self.add_error('store', 'Для этой роли магазин обязателен.')
        if role == EmployeeProfile.Role.SYSTEM_ADMIN and store is not None:
            self.add_error('store', 'Для администратора системы магазин должен быть пустым.')
        return data


class ManagedUserCreateForm(ManagedUserForm):
    password = forms.CharField(label='Пароль', widget=forms.PasswordInput)
    password_confirmation = forms.CharField(
        label='Подтверждение пароля',
        widget=forms.PasswordInput,
    )

    def clean(self):
        data = super().clean()
        if data.get('password') != data.get('password_confirmation'):
            self.add_error('password_confirmation', 'Пароли не совпадают.')
        return data


class ManagedUserUpdateForm(ManagedUserForm):
    pass


class PasswordResetForm(forms.Form):
    password = forms.CharField(label='Новый временный пароль', widget=forms.PasswordInput)
    password_confirmation = forms.CharField(
        label='Подтверждение пароля',
        widget=forms.PasswordInput,
    )

    def clean(self):
        data = super().clean()
        if data.get('password') != data.get('password_confirmation'):
            self.add_error('password_confirmation', 'Пароли не совпадают.')
        return data


def managed_user_initial(user):
    profile = user.employee_profile
    return {
        'username': user.username,
        'first_name': user.first_name,
        'last_name': user.last_name,
        'email': user.email,
        'role': profile.role,
        'store': profile.store,
        'is_active': user.is_active and profile.is_active,
    }
