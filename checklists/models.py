from datetime import time

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone

from checklists.telegram_events import TELEGRAM_EVENT_CHOICES


def default_working_weekdays():
    # Сохраняем прежнее поведение существующих магазинов: до явной настройки
    # чек-лист доступен во все дни недели.
    return [0, 1, 2, 3, 4, 5, 6]


class ChecklistDayStatus(models.TextChoices):
    NORMAL = 'normal', 'Обычный день'
    TESTING = 'testing', 'Тестирование'
    DAY_OFF = 'day_off', 'Выходной'
    EMERGENCY = 'emergency', 'Чрезвычайная ситуация'


class Store(models.Model):
    name = models.CharField('название', max_length=255)
    code = models.SlugField('код', max_length=32, unique=True)
    logo = models.ImageField(
        'логотип',
        upload_to='stores/logo/',
        null=True,
        blank=True,
    )
    users = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        through='UserStoreMembership',
        related_name='stores',
        verbose_name='пользователи',
    )
    timezone = models.CharField(
        'часовой пояс',
        max_length=64,
        default='Europe/Moscow',
    )
    is_active = models.BooleanField('активен', default=True)
    created_at = models.DateTimeField('создан', auto_now_add=True)
    updated_at = models.DateTimeField('изменён', auto_now=True)

    class Meta:
        verbose_name = 'магазин'
        verbose_name_plural = 'магазины'
        ordering = ('name',)

    def __str__(self):
        return f'{self.name} ({self.code})'


class StorePriceTagTemplate(models.Model):
    class LayoutTemplate(models.TextChoices):
        ES_AUTO = 'es_auto', 'ES-AUTO — текущий шаблон'
        PINEL = 'pinel', 'PINEL — отдельный шаблон'

    class PrintMode(models.TextChoices):
        COLOR = 'color', 'Цветной принтер'
        MONOCHROME = 'monochrome', 'Чёрно-белый принтер'

    class CategoryDetectionMode(models.TextChoices):
        URL = 'url', 'По адресу раздела'
        PROPERTY = 'property', 'По свойству товара'

    color_validator = RegexValidator(
        r'^#[0-9A-Fa-f]{6}$',
        'Укажите цвет в формате #112233.',
    )
    store = models.ForeignKey(
        Store,
        verbose_name='магазин',
        on_delete=models.CASCADE,
        related_name='price_tag_templates',
    )
    name = models.CharField(
        'название интернет-магазина',
        max_length=120,
        default='ES-AUTO',
    )
    site_domain = models.CharField(
        'домен сайта',
        max_length=255,
        default='es-auto.ru',
        help_text='Например: es-auto.ru или pinel.ru.',
    )
    category_detection_mode = models.CharField(
        'определение категории',
        max_length=20,
        choices=CategoryDetectionMode.choices,
        default=CategoryDetectionMode.URL,
    )
    layout_template = models.CharField(
        'шаблон оформления ценника',
        max_length=20,
        choices=LayoutTemplate.choices,
        default=LayoutTemplate.ES_AUTO,
        help_text=(
            'Выберите оформление, которое используется для товаров этого сайта.'
        ),
    )
    logo = models.ImageField(
        'логотип для ценников',
        upload_to='stores/price_tag_logo/',
        null=True,
        blank=True,
    )
    available_property_names = models.JSONField(
        'найденные свойства',
        default=list,
        blank=True,
    )
    primary_color = models.CharField(
        'основной цвет',
        max_length=7,
        default='#172554',
        validators=[color_validator],
    )
    accent_color = models.CharField(
        'акцентный цвет',
        max_length=7,
        default='#f97316',
        validators=[color_validator],
    )
    promotion_background_color = models.CharField(
        'цвет фона акции',
        max_length=7,
        default='#fff7ed',
        validators=[color_validator],
        help_text='Используется для блока акции в шаблоне ES-AUTO.',
    )
    show_image = models.BooleanField('показывать фото', default=True)
    show_sku = models.BooleanField('показывать артикул', default=True)
    show_properties = models.BooleanField(
        'показывать характеристики',
        default=True,
    )
    max_properties = models.PositiveSmallIntegerField(
        'максимум характеристик',
        default=5,
        validators=[MinValueValidator(1), MaxValueValidator(5)],
    )
    footer = models.CharField(
        'текст внизу',
        max_length=120,
        blank=True,
        default='',
    )
    qr_utm_parameters = models.CharField(
        'UTM-параметры для QR-кода',
        max_length=500,
        blank=True,
        default='',
        help_text=(
            'Например: utm_source=price_tag&utm_medium=offline. '
            'Параметры добавятся к ссылке товара в QR-коде.'
        ),
    )
    print_mode = models.CharField(
        'режим печати',
        max_length=20,
        choices=PrintMode.choices,
        default=PrintMode.COLOR,
    )
    is_active = models.BooleanField('активен', default=True)
    updated_at = models.DateTimeField('изменён', auto_now=True)

    class Meta:
        verbose_name = 'шаблон ценника'
        verbose_name_plural = 'шаблоны ценников'
        ordering = ('name', 'id')
        constraints = [
            models.UniqueConstraint(
                fields=('store', 'site_domain'),
                name='unique_price_tag_profile_store_domain',
            ),
        ]

    def __str__(self):
        return f'{self.store.name}: {self.name}'


class StorePriceTagCategory(models.Model):
    profile = models.ForeignKey(
        StorePriceTagTemplate,
        verbose_name='профиль интернет-магазина',
        on_delete=models.CASCADE,
        related_name='categories',
    )
    name = models.CharField('категория', max_length=120)
    source_url = models.URLField(
        'ссылка на раздел сайта',
        max_length=500,
        blank=True,
        default='',
        help_text='Например: https://es-auto.ru/car-box/.',
    )
    match_property_name = models.CharField(
        'свойство для определения',
        max_length=160,
        blank=True,
        default='',
    )
    match_property_value = models.CharField(
        'значение свойства',
        max_length=255,
        blank=True,
        default='',
    )
    property_names = models.TextField(
        'свойства на ценнике',
        blank=True,
        default='',
        help_text='Каждое свойство с новой строки, в нужном порядке.',
    )
    available_property_names = models.JSONField(
        'найденные свойства категории',
        default=list,
        blank=True,
    )
    promotion_title = models.CharField(
        'заголовок акции',
        max_length=100,
        blank=True,
        default='',
        help_text='Например: БЕСПЛАТНАЯ УСТАНОВКА.',
    )
    promotion_details = models.CharField(
        'условия акции',
        max_length=200,
        blank=True,
        default='',
        help_text='Например: условия акции уточняйте у менеджера.',
    )
    sort_order = models.PositiveIntegerField('порядок', default=0)
    is_active = models.BooleanField('активна', default=True)
    updated_at = models.DateTimeField('изменена', auto_now=True)

    class Meta:
        verbose_name = 'категория ценников'
        verbose_name_plural = 'категории ценников'
        ordering = ('sort_order', 'name', 'id')
        constraints = [
            models.UniqueConstraint(
                fields=('profile', 'name'),
                name='unique_price_tag_category_profile',
            ),
        ]

    @property
    def property_name_list(self):
        return [item.strip() for item in self.property_names.splitlines() if item.strip()]

    def __str__(self):
        return f'{self.profile.name}: {self.name}'


class PriceTagGeneration(models.Model):
    store = models.ForeignKey(
        Store,
        verbose_name='магазин',
        on_delete=models.CASCADE,
        related_name='price_tag_generations',
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name='создал',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='price_tag_generations',
    )
    item_count = models.PositiveSmallIntegerField('ценников', default=0)
    seller_praise = models.CharField(
        'похвала продавцу',
        max_length=160,
        blank=True,
        default='',
    )
    sales_tip = models.CharField(
        'совет продавцу',
        max_length=400,
        blank=True,
        default='',
    )
    created_at = models.DateTimeField('создано', auto_now_add=True)

    class Meta:
        verbose_name = 'генерация ценников'
        verbose_name_plural = 'генерации ценников'
        ordering = ('-created_at', '-id')


class PriceTagGenerationItem(models.Model):
    generation = models.ForeignKey(
        PriceTagGeneration,
        verbose_name='генерация',
        on_delete=models.CASCADE,
        related_name='items',
    )
    source_url = models.URLField('ссылка товара', max_length=2000)
    product_name = models.CharField('название из h1', max_length=500)
    profile_name = models.CharField('интернет-магазин', max_length=120)
    category_name = models.CharField('категория', max_length=120, blank=True)
    sort_order = models.PositiveSmallIntegerField('порядок', default=0)

    class Meta:
        verbose_name = 'товар в генерации ценников'
        verbose_name_plural = 'товары в генерации ценников'
        ordering = ('sort_order', 'id')


class PriceTagNameCorrection(models.Model):
    profile = models.ForeignKey(
        StorePriceTagTemplate,
        verbose_name='профиль сайта',
        on_delete=models.CASCADE,
        related_name='name_corrections',
    )
    category = models.ForeignKey(
        StorePriceTagCategory,
        verbose_name='категория',
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name='name_corrections',
    )
    source_url = models.URLField('ссылка товара', max_length=500)
    original_name = models.CharField('исходное название', max_length=500)
    corrected_name = models.CharField('исправленное название', max_length=500)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name='исправил',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='price_tag_name_corrections',
    )
    use_count = models.PositiveIntegerField('использований', default=0)
    created_at = models.DateTimeField('создано', auto_now_add=True)
    updated_at = models.DateTimeField('изменено', auto_now=True)

    class Meta:
        verbose_name = 'правка названия ценника'
        verbose_name_plural = 'правки названий ценников'
        ordering = ('-updated_at', '-id')
        constraints = [
            models.UniqueConstraint(
                fields=('profile', 'source_url'),
                name='unique_price_tag_name_correction_url',
            ),
        ]


SCHEDULE_CHANGE_HELP = (
    'Изменение применяется только к новым ежедневным чек-листам'
)


class StoreChecklistSchedule(models.Model):
    store = models.OneToOneField(
        Store,
        verbose_name='магазин',
        on_delete=models.CASCADE,
        related_name='checklist_schedule',
        help_text=SCHEDULE_CHANGE_HELP,
    )
    opening_time = models.TimeField(
        'начало работы',
        default=time(9),
        help_text=SCHEDULE_CHANGE_HELP,
    )
    morning_deadline = models.TimeField(
        'дедлайн утреннего этапа',
        default=time(11),
        help_text=SCHEDULE_CHANGE_HELP,
    )
    daytime_deadline = models.TimeField(
        'дедлайн дневного этапа',
        default=time(20),
        help_text=SCHEDULE_CHANGE_HELP,
    )
    closing_deadline = models.TimeField(
        'дедлайн вечернего этапа',
        default=time(22),
        help_text=SCHEDULE_CHANGE_HELP,
    )
    morning_completion_window_minutes = models.PositiveSmallIntegerField(
        'окно завершения утреннего этапа, минут',
        default=120,
        validators=(MinValueValidator(0), MaxValueValidator(720)),
        help_text=SCHEDULE_CHANGE_HELP,
    )
    day_completion_window_minutes = models.PositiveSmallIntegerField(
        'окно завершения дневного этапа, минут',
        default=120,
        validators=(MinValueValidator(0), MaxValueValidator(720)),
        help_text=SCHEDULE_CHANGE_HELP,
    )
    evening_completion_window_minutes = models.PositiveSmallIntegerField(
        'окно завершения вечернего этапа, минут',
        default=120,
        validators=(MinValueValidator(0), MaxValueValidator(720)),
        help_text=SCHEDULE_CHANGE_HELP,
    )
    warning_minutes_before = models.PositiveIntegerField(
        'предупреждать за, минут',
        default=30,
        help_text=SCHEDULE_CHANGE_HELP,
    )
    notifications_enabled = models.BooleanField(
        'уведомления включены',
        default=True,
        help_text=SCHEDULE_CHANGE_HELP,
    )
    working_weekdays = models.JSONField(
        'рабочие дни недели',
        default=default_working_weekdays,
        help_text='Номера дней: понедельник — 0, воскресенье — 6.',
    )
    is_active = models.BooleanField(
        'активно',
        default=True,
        help_text=SCHEDULE_CHANGE_HELP,
    )
    created_at = models.DateTimeField('создано', auto_now_add=True)
    updated_at = models.DateTimeField('изменено', auto_now=True)

    class Meta:
        verbose_name = 'расписание чек-листа магазина'
        verbose_name_plural = 'расписания чек-листов магазинов'
        constraints = [
            models.CheckConstraint(
                condition=Q(warning_minutes_before__gt=0),
                name='schedule_warning_minutes_positive',
            ),
            models.CheckConstraint(
                condition=Q(
                    morning_completion_window_minutes__in=tuple(
                        range(0, 721, 15)
                    ),
                ),
                name='schedule_morning_completion_window_valid',
            ),
            models.CheckConstraint(
                condition=Q(
                    day_completion_window_minutes__in=tuple(
                        range(0, 721, 15)
                    ),
                ),
                name='schedule_day_completion_window_valid',
            ),
            models.CheckConstraint(
                condition=Q(
                    evening_completion_window_minutes__in=tuple(
                        range(0, 721, 15)
                    ),
                ),
                name='schedule_evening_completion_window_valid',
            ),
        ]

    def __str__(self):
        return f'Расписание: {self.store}'

    @staticmethod
    def _minutes(value):
        return value.hour * 60 + value.minute + value.second / 60

    def clean(self):
        errors = {}
        weekdays = self.working_weekdays
        if not isinstance(weekdays, list):
            errors['working_weekdays'] = 'Рабочие дни должны быть списком.'
        else:
            normalized_weekdays = []
            for weekday in weekdays:
                if isinstance(weekday, bool):
                    errors['working_weekdays'] = 'Некорректный день недели.'
                    break
                try:
                    normalized_weekdays.append(int(weekday))
                except (TypeError, ValueError):
                    errors['working_weekdays'] = 'Некорректный день недели.'
                    break
            if (
                'working_weekdays' not in errors
                and (
                    any(value < 0 or value > 6 for value in normalized_weekdays)
                    or len(normalized_weekdays) != len(set(normalized_weekdays))
                )
            ):
                errors['working_weekdays'] = (
                    'Дни недели должны быть уникальными числами от 0 до 6.'
                )
            elif 'working_weekdays' not in errors:
                self.working_weekdays = sorted(normalized_weekdays)
        daytime_times = (
            self.opening_time,
            self.morning_deadline,
            self.daytime_deadline,
        )
        if all(daytime_times) and self.closing_deadline:
            values = [self._minutes(value) for value in daytime_times]
            if values != sorted(values) or len(set(values)) != len(values):
                errors['opening_time'] = (
                    'Время должно идти строго по порядку: начало работы, '
                    'утренний и дневной дедлайны.'
                )
            else:
                closing_value = self._minutes(self.closing_deadline)
                if closing_value <= values[-1]:
                    closing_value += 24 * 60
                if closing_value > values[0] + 24 * 60:
                    errors['closing_deadline'] = (
                        'Вечерний дедлайн должен наступить не позднее чем '
                        'через 24 часа после начала работы.'
                    )
                timeline = (*values, closing_value)
                shortest_stage = min(
                    later - earlier
                    for earlier, later in zip(timeline, timeline[1:])
                )
                if (
                    self.warning_minutes_before is not None
                    and self.warning_minutes_before > shortest_stage
                ):
                    errors['warning_minutes_before'] = (
                        'Предупреждение не может быть длиннее самого '
                        'короткого этапа.'
                    )
        if (
            self.warning_minutes_before is not None
            and self.warning_minutes_before <= 0
        ):
            errors['warning_minutes_before'] = (
                'Время предупреждения должно быть больше нуля.'
            )
        for field_name in (
            'morning_completion_window_minutes',
            'day_completion_window_minutes',
            'evening_completion_window_minutes',
        ):
            value = getattr(self, field_name)
            if (
                value is None
                or not 0 <= value <= 720
                or value % 15 != 0
            ):
                errors[field_name] = (
                    'Выберите значение от 0 до 720 минут с шагом 15 минут.'
                )
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class StoreDayStatus(models.Model):
    store = models.ForeignKey(
        Store,
        verbose_name='магазин',
        on_delete=models.CASCADE,
        related_name='day_statuses',
    )
    date = models.DateField('дата')
    status = models.CharField(
        'статус дня',
        max_length=16,
        choices=ChecklistDayStatus.choices,
        default=ChecklistDayStatus.NORMAL,
    )
    comment = models.TextField('комментарий', blank=True)
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name='изменил',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='changed_store_day_statuses',
    )
    created_at = models.DateTimeField('создан', auto_now_add=True)
    updated_at = models.DateTimeField('изменён', auto_now=True)

    class Meta:
        verbose_name = 'статус дня магазина'
        verbose_name_plural = 'статусы дней магазинов'
        ordering = ('-date', 'store__name')
        constraints = [
            models.UniqueConstraint(
                fields=('store', 'date'),
                name='unique_store_day_status',
            ),
        ]
        indexes = [
            models.Index(
                fields=('store', 'date', 'status'),
                name='store_day_status_idx',
            ),
        ]

    def __str__(self):
        return (
            f'{self.store.name}, {self.date:%d.%m.%Y}: '
            f'{self.get_status_display()}'
        )


class StoreNotificationSettings(models.Model):
    store = models.OneToOneField(
        Store,
        verbose_name='магазин',
        on_delete=models.CASCADE,
        related_name='notification_settings',
    )
    telegram_chat_id = models.CharField(
        'Telegram chat ID',
        max_length=255,
        blank=True,
    )
    warning_enabled = models.BooleanField(
        'предупреждения включены',
        default=True,
    )
    overdue_enabled = models.BooleanField(
        'уведомления о просрочке включены',
        default=True,
    )
    completed_late_enabled = models.BooleanField(
        'уведомления о позднем завершении включены',
        default=True,
    )
    is_active = models.BooleanField('активно', default=True)
    created_at = models.DateTimeField('создано', auto_now_add=True)
    updated_at = models.DateTimeField('изменено', auto_now=True)

    class Meta:
        verbose_name = 'настройка уведомлений магазина'
        verbose_name_plural = 'настройки уведомлений магазинов'

    def __str__(self):
        return f'Уведомления: {self.store}'

    def clean(self):
        if self.is_active and not (self.telegram_chat_id or '').strip():
            raise ValidationError(
                {'telegram_chat_id': 'Для активной настройки нужен chat ID.'}
            )

    def save(self, *args, **kwargs):
        self.telegram_chat_id = (self.telegram_chat_id or '').strip()
        self.full_clean()
        return super().save(*args, **kwargs)


class StoreEmployee(models.Model):
    class Department(models.TextChoices):
        SERVICE = 'service', 'Сервис'
        CALL_CENTER = 'call_center', 'КЦ'
        STORE = 'store', 'Магазин'

    store = models.ForeignKey(
        Store,
        verbose_name='магазин',
        on_delete=models.PROTECT,
        related_name='store_employees',
    )
    first_name = models.CharField('имя', max_length=150)
    last_name = models.CharField('фамилия', max_length=150, blank=True)
    display_name = models.CharField('отображаемое имя', max_length=255)
    position = models.CharField('должность', max_length=150, blank=True)
    department = models.CharField(
        'подразделение',
        max_length=20,
        choices=Department.choices,
        default=Department.STORE,
    )
    personnel_number = models.CharField(
        'табельный номер',
        max_length=64,
        null=True,
        blank=True,
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name='учётная запись',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='store_employee_records',
    )
    is_active = models.BooleanField('активен', default=True)
    sort_order = models.PositiveIntegerField('порядок сортировки', default=0)
    created_at = models.DateTimeField('создан', auto_now_add=True)
    updated_at = models.DateTimeField('изменён', auto_now=True)

    class Meta:
        verbose_name = 'сотрудник магазина'
        verbose_name_plural = 'сотрудники магазина'
        ordering = ('sort_order', 'display_name', 'id')
        constraints = [
            models.UniqueConstraint(
                fields=('store', 'user'),
                name='unique_store_employee_user',
            ),
            models.UniqueConstraint(
                fields=('store', 'personnel_number'),
                name='unique_store_personnel_number',
            ),
        ]

    def __str__(self):
        return f'{self.display_name} — {self.store.name}'

    def clean(self):
        if not self.display_name.strip():
            raise ValidationError(
                {'display_name': 'Укажите отображаемое имя сотрудника.'}
            )
        if self.user_id:
            if not self.user.is_active:
                raise ValidationError(
                    {'user': 'Нельзя связать неактивную учётную запись.'}
                )
            if not UserStoreMembership.objects.filter(
                user_id=self.user_id,
                store_id=self.store_id,
                is_active=True,
            ).exists():
                raise ValidationError(
                    {
                        'user': (
                            'Учётная запись должна иметь активную связь '
                            'с этим магазином.'
                        )
                    }
                )

    def save(self, *args, **kwargs):
        self.first_name = self.first_name.strip()
        self.last_name = self.last_name.strip()
        self.display_name = self.display_name.strip()
        self.position = self.position.strip()
        self.personnel_number = (
            self.personnel_number.strip() if self.personnel_number else None
        )
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        protected_relations = (
            self.shift_assignments.exists()
            or self.answers_given.exists()
            or self.answers_last_edited.exists()
            or self.completed_stages.exists()
            or self.answer_revisions.exists()
        )
        if protected_relations:
            raise ValidationError(
                'Сотрудника с историей действий нужно деактивировать.'
            )
        return super().delete(*args, **kwargs)


class StoreTerminalAccount(models.Model):
    store = models.OneToOneField(
        Store,
        verbose_name='магазин',
        on_delete=models.CASCADE,
        related_name='terminal_account',
    )
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        verbose_name='пользователь терминала',
        on_delete=models.PROTECT,
        related_name='store_terminal_account',
    )
    is_active = models.BooleanField('активен', default=True)
    created_at = models.DateTimeField('создан', auto_now_add=True)
    updated_at = models.DateTimeField('изменён', auto_now=True)

    class Meta:
        verbose_name = 'терминальный аккаунт магазина'
        verbose_name_plural = 'терминальные аккаунты магазинов'

    def __str__(self):
        return f'Терминал: {self.store}'

    def clean(self):
        if self.user_id and (self.user.is_staff or self.user.is_superuser):
            raise ValidationError(
                {'user': 'Терминальный аккаунт не должен иметь staff/admin права.'}
            )
        if self.user_id and self.is_active:
            profile = EmployeeProfile.objects.filter(user_id=self.user_id).first()
            if (
                profile is None
                or not profile.is_active
                or profile.role != EmployeeProfile.Role.STORE_ACCOUNT
                or profile.store_id != self.store_id
            ):
                raise ValidationError(
                    {'user': 'Активный терминал должен иметь активный '
                    'профиль «Аккаунт магазина» того же магазина.'}
                )

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class EmployeeProfile(models.Model):
    class Role(models.TextChoices):
        STORE_ACCOUNT = 'store_account', 'Аккаунт магазина'
        STORE_DIRECTOR = 'store_director', 'Директор магазина'
        SYSTEM_ADMIN = 'system_admin', 'Администратор системы'

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        verbose_name='пользователь',
        on_delete=models.CASCADE,
        related_name='employee_profile',
    )
    store = models.ForeignKey(
        Store,
        verbose_name='магазин',
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name='employees',
    )
    role = models.CharField(
        'роль',
        max_length=20,
        choices=Role.choices,
        default=Role.STORE_ACCOUNT,
        null=True,
        blank=True,
    )
    is_active = models.BooleanField('активен', default=True)
    created_at = models.DateTimeField('создан', auto_now_add=True)
    updated_at = models.DateTimeField('изменён', auto_now=True)

    class Meta:
        verbose_name = 'профиль сотрудника'
        verbose_name_plural = 'профили сотрудников'
        ordering = ('store', 'user__username')
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(
                        role__in=('store_account', 'store_director'),
                        store__isnull=False,
                    )
                    | Q(role='system_admin', store__isnull=True)
                    | Q(role__isnull=True, is_active=False)
                ),
                name='access_profile_role_store_consistent',
            ),
        ]

    def __str__(self):
        store_name = self.store.name if self.store_id else 'все магазины'
        return f'{self.user.get_username()} — {store_name}'

    def clean(self):
        errors = {}
        if self.role in {self.Role.STORE_ACCOUNT, self.Role.STORE_DIRECTOR}:
            if self.store_id is None:
                errors['store'] = 'Для этой роли магазин обязателен.'
            elif not self.store.is_active and self.is_active:
                errors['store'] = 'Нельзя активировать профиль неактивного магазина.'
        elif self.role == self.Role.SYSTEM_ADMIN:
            if self.store_id is not None:
                errors['store'] = 'Администратор системы не привязывается к магазину.'
        elif self.is_active:
            errors['role'] = 'Активному профилю нужна одна из трёх ролей.'
        active_terminal = StoreTerminalAccount.objects.filter(
            user_id=self.user_id,
            is_active=True,
        ).first() if self.user_id else None
        if active_terminal and (
            self.role != self.Role.STORE_ACCOUNT
            or active_terminal.store_id != self.store_id
            or not self.is_active
        ):
            errors['role'] = 'Активный StoreTerminalAccount не согласован с ролью.'
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


# Временные API-алиасы для сервисов этапов 2–3.8. Они не входят в choices
# и не создают дополнительных бизнес-ролей.
EmployeeProfile.Role.EMPLOYEE = EmployeeProfile.Role.STORE_ACCOUNT
EmployeeProfile.Role.MANAGER = EmployeeProfile.Role.STORE_DIRECTOR
EmployeeProfile.Role.ADMINISTRATOR = EmployeeProfile.Role.SYSTEM_ADMIN


class UserStoreMembership(models.Model):
    class Role(models.TextChoices):
        DIRECTOR = 'director', 'Директор'
        EMPLOYEE = 'employee', 'Сотрудник'
        ADMINISTRATOR = 'administrator', 'Администратор'

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name='пользователь',
        on_delete=models.CASCADE,
        related_name='store_memberships',
    )
    store = models.ForeignKey(
        Store,
        verbose_name='магазин',
        on_delete=models.CASCADE,
        related_name='user_memberships',
    )
    role_in_store = models.CharField(
        'роль в магазине',
        max_length=20,
        choices=Role.choices,
    )
    is_active = models.BooleanField('активна', default=True)
    created_at = models.DateTimeField('создана', auto_now_add=True)

    class Meta:
        verbose_name = 'связь пользователя с магазином'
        verbose_name_plural = 'связи пользователей с магазинами'
        ordering = ('store__name', 'user__username')
        constraints = [
            models.UniqueConstraint(
                fields=('user', 'store'),
                name='unique_user_store_membership',
            ),
        ]
        indexes = [
            models.Index(
                fields=('user', 'is_active'),
                name='membership_user_active_idx',
            ),
            models.Index(
                fields=('store', 'role_in_store', 'is_active'),
                name='membership_store_role_idx',
            ),
        ]

    def __str__(self):
        return (
            f'{self.user.get_username()} → {self.store.name} '
            f'({self.get_role_in_store_display()})'
        )

    def clean(self):
        if self.is_active and not self.store.is_active:
            raise ValidationError(
                {'store': 'Нельзя активировать связь с неактивным магазином.'}
            )


class ChecklistTemplate(models.Model):
    store = models.ForeignKey(
        Store,
        verbose_name='магазин',
        on_delete=models.PROTECT,
        related_name='checklist_templates',
    )
    name = models.CharField('название', max_length=255)
    is_active = models.BooleanField('активен', default=True)
    created_at = models.DateTimeField('создан', auto_now_add=True)
    updated_at = models.DateTimeField('изменён', auto_now=True)

    class Meta:
        verbose_name = 'шаблон чек-листа'
        verbose_name_plural = 'шаблоны чек-листов'
        ordering = ('store', 'name')

    def __str__(self):
        return f'{self.store.name}: {self.name}'


class ChecklistTemplateVersion(models.Model):
    class Status(models.TextChoices):
        DRAFT = 'draft', 'Черновик'
        PUBLISHED = 'published', 'Опубликована'
        ARCHIVED = 'archived', 'В архиве'

    template = models.ForeignKey(
        ChecklistTemplate,
        verbose_name='шаблон',
        on_delete=models.CASCADE,
        related_name='versions',
    )
    version_number = models.PositiveIntegerField('номер версии')
    status = models.CharField(
        'статус',
        max_length=16,
        choices=Status.choices,
        default=Status.DRAFT,
    )
    published_at = models.DateTimeField(
        'опубликована',
        null=True,
        blank=True,
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name='создал',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='created_checklist_versions',
    )
    created_at = models.DateTimeField('создана', auto_now_add=True)

    class Meta:
        verbose_name = 'версия шаблона'
        verbose_name_plural = 'версии шаблонов'
        ordering = ('template', '-version_number')
        constraints = [
            models.UniqueConstraint(
                fields=('template', 'version_number'),
                name='unique_template_version_number',
            ),
            models.CheckConstraint(
                condition=Q(status='draft') | Q(published_at__isnull=False),
                name='published_or_archived_version_has_timestamp',
            ),
        ]

    def __str__(self):
        return f'{self.template} — версия {self.version_number}'

    def save(self, *args, **kwargs):
        if self.pk:
            original = type(self).objects.get(pk=self.pk)
            if original.status == self.Status.PUBLISHED:
                changed = any(
                    getattr(original, field.attname) != getattr(self, field.attname)
                    for field in self._meta.concrete_fields
                    if field.name not in {'id'}
                )
                if changed:
                    raise ValidationError(
                        'Опубликованную версию нельзя изменять; создайте новую.'
                    )
            if (
                original.status != self.Status.PUBLISHED
                and self.status == self.Status.PUBLISHED
                and not getattr(self, '_publishing_via_service', False)
            ):
                raise ValidationError(
                    'Публикация версии разрешена только через сервисный слой.'
                )
        elif (
            self.status == self.Status.PUBLISHED
            and not getattr(self, '_publishing_via_service', False)
        ):
            raise ValidationError(
                'Публикация версии разрешена только через сервисный слой.'
            )
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.status == self.Status.PUBLISHED:
            raise ValidationError('Опубликованную версию нельзя удалить.')
        return super().delete(*args, **kwargs)


class ChecklistSection(models.Model):
    version = models.ForeignKey(
        ChecklistTemplateVersion,
        verbose_name='версия шаблона',
        on_delete=models.CASCADE,
        related_name='sections',
    )
    name = models.CharField('название', max_length=255)
    code = models.SlugField('код', max_length=50)
    sort_order = models.PositiveIntegerField('порядок сортировки', default=0)
    created_at = models.DateTimeField('создан', auto_now_add=True)

    class Meta:
        verbose_name = 'раздел чек-листа'
        verbose_name_plural = 'разделы чек-листа'
        ordering = ('sort_order', 'id')
        constraints = [
            models.UniqueConstraint(
                fields=('version', 'code'),
                name='unique_section_code_per_version',
            ),
        ]

    def __str__(self):
        return f'{self.version}: {self.name}'

    def _ensure_version_is_editable(self):
        if self.version_id:
            status = ChecklistTemplateVersion.objects.values_list(
                'status', flat=True
            ).get(pk=self.version_id)
            if status == ChecklistTemplateVersion.Status.PUBLISHED:
                raise ValidationError(
                    'Разделы опубликованной версии нельзя изменять.'
                )

    def save(self, *args, **kwargs):
        self._ensure_version_is_editable()
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        self._ensure_version_is_editable()
        return super().delete(*args, **kwargs)


class ChecklistItem(models.Model):
    class AnswerType(models.TextChoices):
        STATUS = 'status', 'Статус выполнения'
        INTEGER = 'integer', 'Целое число'

    section = models.ForeignKey(
        ChecklistSection,
        verbose_name='раздел',
        on_delete=models.CASCADE,
        related_name='items',
    )
    text = models.TextField('текст пункта')
    description = models.TextField('описание или инструкция', blank=True)
    sort_order = models.PositiveIntegerField('порядок сортировки', default=0)
    is_active = models.BooleanField('активен', default=True)
    is_required = models.BooleanField('обязателен', default=True)
    answer_type = models.CharField(
        'тип ответа',
        max_length=16,
        choices=AnswerType.choices,
        default=AnswerType.STATUS,
    )
    comment_required_on_failure = models.BooleanField(
        'требовать комментарий при невыполнении',
        default=True,
    )
    allow_not_applicable = models.BooleanField(
        'разрешить «не применимо»',
        default=False,
    )
    effective_from = models.DateField(
        'действует с',
        null=True,
        blank=True,
    )
    effective_until = models.DateField(
        'действует до',
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField('создан', auto_now_add=True)
    updated_at = models.DateTimeField('изменён', auto_now=True)

    class Meta:
        verbose_name = 'пункт чек-листа'
        verbose_name_plural = 'пункты чек-листа'
        ordering = ('sort_order', 'id')

    def __str__(self):
        return self.text

    def _ensure_version_is_editable(self):
        if self.section_id:
            status = ChecklistSection.objects.values_list(
                'version__status', flat=True
            ).get(pk=self.section_id)
            if status == ChecklistTemplateVersion.Status.PUBLISHED:
                raise ValidationError(
                    'Пункты опубликованной версии нельзя изменять.'
                )

    def save(self, *args, **kwargs):
        self._ensure_version_is_editable()
        self.full_clean()
        return super().save(*args, **kwargs)

    def clean(self):
        if self.answer_type == self.AnswerType.INTEGER:
            self.allow_not_applicable = False
            self.comment_required_on_failure = False
        if (
            self.effective_from
            and self.effective_until
            and self.effective_from > self.effective_until
        ):
            raise ValidationError(
                {'effective_until': 'Дата окончания не может быть раньше начала.'}
            )

    def delete(self, *args, **kwargs):
        self._ensure_version_is_editable()
        return super().delete(*args, **kwargs)


class DailyChecklist(models.Model):
    class Status(models.TextChoices):
        DRAFT = 'draft', 'Черновик'
        COMPLETED = 'completed', 'Завершён'
        REOPENED = 'reopened', 'Открыт повторно'

    store = models.ForeignKey(
        Store,
        verbose_name='магазин',
        on_delete=models.PROTECT,
        related_name='daily_checklists',
    )
    employee = models.ForeignKey(
        EmployeeProfile,
        verbose_name='сотрудник',
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name='daily_checklists',
    )
    terminal_account = models.ForeignKey(
        StoreTerminalAccount,
        verbose_name='терминальный аккаунт',
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name='daily_checklists',
    )
    checklist_date = models.DateField('дата чек-листа')
    template_version = models.ForeignKey(
        ChecklistTemplateVersion,
        verbose_name='версия шаблона',
        on_delete=models.PROTECT,
        related_name='daily_checklists',
    )
    status = models.CharField(
        'статус',
        max_length=16,
        choices=Status.choices,
        default=Status.DRAFT,
    )
    day_status = models.CharField(
        'статус дня',
        max_length=16,
        choices=ChecklistDayStatus.choices,
        default=ChecklistDayStatus.NORMAL,
    )
    started_at = models.DateTimeField('начат', null=True, blank=True)
    completed_at = models.DateTimeField('завершён', null=True, blank=True)
    reopened_at = models.DateTimeField('открыт повторно', null=True, blank=True)
    reopened_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name='повторно открыл',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='reopened_daily_checklists',
    )
    created_at = models.DateTimeField('создан', auto_now_add=True)
    updated_at = models.DateTimeField('изменён', auto_now=True)

    class Meta:
        verbose_name = 'ежедневный чек-лист'
        verbose_name_plural = 'ежедневные чек-листы'
        ordering = ('-checklist_date', 'store', 'employee')
        constraints = [
            models.UniqueConstraint(
                fields=('store', 'employee', 'checklist_date'),
                name='unique_employee_daily_checklist',
            ),
            models.UniqueConstraint(
                fields=('store', 'terminal_account', 'checklist_date'),
                name='unique_terminal_daily_checklist',
            ),
            models.CheckConstraint(
                condition=(
                    Q(employee__isnull=False, terminal_account__isnull=True)
                    | Q(employee__isnull=True, terminal_account__isnull=False)
                ),
                name='daily_checklist_one_account_type',
            ),
        ]

    def __str__(self):
        account = self.employee or self.terminal_account
        return f'{account} — {self.checklist_date:%d.%m.%Y}'

    def clean(self):
        errors = {}
        if self.employee_id and self.store_id:
            employee_store_id = EmployeeProfile.objects.values_list(
                'store_id', flat=True
            ).get(pk=self.employee_id)
            if employee_store_id != self.store_id:
                errors['store'] = 'Магазин не совпадает с магазином сотрудника.'
        if self.terminal_account_id and self.store_id:
            terminal_store_id = StoreTerminalAccount.objects.values_list(
                'store_id', flat=True
            ).get(pk=self.terminal_account_id)
            if terminal_store_id != self.store_id:
                errors['store'] = 'Терминал относится к другому магазину.'
        if bool(self.employee_id) == bool(self.terminal_account_id):
            errors['employee'] = (
                'Укажите ровно один тип аккаунта: индивидуальный или терминальный.'
            )
        if self.template_version_id and self.store_id:
            template_store_id = ChecklistTemplateVersion.objects.values_list(
                'template__store_id', flat=True
            ).get(pk=self.template_version_id)
            if template_store_id != self.store_id:
                errors['template_version'] = (
                    'Версия шаблона относится к другому магазину.'
                )
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class DailyChecklistStage(models.Model):
    class SectionCode(models.TextChoices):
        OPENING = 'opening', 'Утренние задачи'
        DURING_DAY = 'during_day', 'Дневные задачи'
        CLOSING = 'closing', 'Вечерние задачи'

    class Status(models.TextChoices):
        LOCKED = 'locked', 'Недоступен'
        AVAILABLE = 'available', 'Доступен'
        COMPLETED = 'completed', 'Завершён вовремя'
        OVERDUE = 'overdue', 'Просрочен'
        COMPLETED_LATE = 'completed_late', 'Завершён с опозданием'

    daily_checklist = models.ForeignKey(
        DailyChecklist,
        verbose_name='ежедневный чек-лист',
        on_delete=models.CASCADE,
        related_name='stages',
    )
    section_code = models.CharField(
        'этап',
        max_length=20,
        choices=SectionCode.choices,
    )
    status = models.CharField(
        'статус',
        max_length=20,
        choices=Status.choices,
        default=Status.LOCKED,
    )
    opens_at = models.DateTimeField('открывается')
    completion_available_at = models.DateTimeField(
        'завершение доступно с',
    )
    deadline_at = models.DateTimeField('срок завершения')
    completed_at = models.DateTimeField('завершён', null=True, blank=True)
    completed_by_employee = models.ForeignKey(
        StoreEmployee,
        verbose_name='завершил сотрудник',
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name='completed_stages',
    )
    last_edited_by_employee = models.ForeignKey(
        StoreEmployee,
        verbose_name='последним изменил сотрудник',
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name='last_edited_stages',
    )
    first_completed_at = models.DateTimeField(
        'впервые завершён',
        null=True,
        blank=True,
    )
    first_completed_by_employee = models.ForeignKey(
        StoreEmployee,
        verbose_name='впервые завершил сотрудник',
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name='first_completed_stages',
    )
    reopened_count = models.PositiveIntegerField(
        'количество повторных открытий',
        default=0,
    )
    created_at = models.DateTimeField('создан', auto_now_add=True)
    updated_at = models.DateTimeField('изменён', auto_now=True)

    class Meta:
        verbose_name = 'временной этап чек-листа'
        verbose_name_plural = 'временные этапы чек-листов'
        ordering = ('opens_at', 'id')
        constraints = [
            models.UniqueConstraint(
                fields=('daily_checklist', 'section_code'),
                name='unique_daily_checklist_stage',
            ),
        ]

    def __str__(self):
        return f'{self.daily_checklist}: {self.get_section_code_display()}'

    def clean(self):
        errors = {}
        if self.opens_at and timezone.is_naive(self.opens_at):
            errors['opens_at'] = 'Время открытия должно содержать часовой пояс.'
        if self.deadline_at and timezone.is_naive(self.deadline_at):
            errors['deadline_at'] = 'Дедлайн должен содержать часовой пояс.'
        if (
            self.completion_available_at
            and timezone.is_naive(self.completion_available_at)
        ):
            errors['completion_available_at'] = (
                'Начало окна завершения должно содержать часовой пояс.'
            )
        if (
            self.opens_at
            and self.deadline_at
            and not errors
            and self.opens_at >= self.deadline_at
        ):
            errors['deadline_at'] = 'Дедлайн должен быть позже открытия.'
        if (
            self.opens_at
            and self.completion_available_at
            and self.deadline_at
            and not errors
            and not (
                self.opens_at
                <= self.completion_available_at
                <= self.deadline_at
            )
        ):
            errors['completion_available_at'] = (
                'Окно завершения должно начинаться между открытием '
                'и дедлайном этапа.'
            )
        if self.completed_at and timezone.is_naive(self.completed_at):
            errors['completed_at'] = 'Время завершения должно содержать часовой пояс.'
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class ChecklistNotification(models.Model):
    class NotificationType(models.TextChoices):
        DEADLINE_WARNING = 'deadline_warning', 'Скоро дедлайн'
        OVERDUE = 'overdue', 'Просрочка'
        COMPLETED_LATE = 'completed_late', 'Завершено с опозданием'
        COMPLETED_WITH_ISSUES = (
            'completed_with_issues',
            'Завершено с невыполненными пунктами',
        )

    class Status(models.TextChoices):
        PENDING = 'pending', 'Ожидает отправки'
        SENDING = 'sending', 'Отправляется'
        SENT = 'sent', 'Отправлено'
        FAILED = 'failed', 'Ошибка отправки'

    stage = models.ForeignKey(
        DailyChecklistStage,
        verbose_name='этап',
        on_delete=models.CASCADE,
        related_name='notifications',
    )
    notification_type = models.CharField(
        'тип уведомления',
        max_length=24,
        choices=NotificationType.choices,
    )
    scheduled_for = models.DateTimeField('запланировано на')
    status = models.CharField(
        'статус',
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
    )
    attempts = models.PositiveIntegerField('попыток', default=0)
    sending_started_at = models.DateTimeField(
        'отправка начата',
        null=True,
        blank=True,
    )
    sent_at = models.DateTimeField('отправлено', null=True, blank=True)
    telegram_message_id = models.BigIntegerField(
        'Telegram message ID',
        null=True,
        blank=True,
    )
    last_error = models.TextField(
        'последняя ошибка',
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField('создано', auto_now_add=True)
    updated_at = models.DateTimeField('изменено', auto_now=True)

    class Meta:
        verbose_name = 'уведомление чек-листа'
        verbose_name_plural = 'уведомления чек-листов'
        ordering = ('scheduled_for', 'id')
        constraints = [
            models.UniqueConstraint(
                fields=('stage', 'notification_type'),
                name='unique_notification_type_stage',
            ),
        ]
        indexes = [
            models.Index(fields=('status',), name='notification_status_idx'),
            models.Index(
                fields=('scheduled_for',),
                name='notification_schedule_idx',
            ),
            models.Index(
                fields=('stage', 'notification_type'),
                name='notification_stage_type_idx',
            ),
        ]

    def __str__(self):
        return f'{self.stage}: {self.get_notification_type_display()}'


class DailyChecklistItem(models.Model):
    daily_checklist = models.ForeignKey(
        DailyChecklist,
        verbose_name='ежедневный чек-лист',
        on_delete=models.CASCADE,
        related_name='items',
    )
    source_item = models.ForeignKey(
        ChecklistItem,
        verbose_name='исходный пункт',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='daily_snapshots',
    )
    section_code = models.CharField('код раздела', max_length=50)
    section_name = models.CharField('название раздела', max_length=255)
    section_sort_order = models.PositiveIntegerField('порядок раздела')
    item_text = models.TextField('текст пункта')
    item_description = models.TextField(
        'описание или инструкция',
        blank=True,
    )
    item_sort_order = models.PositiveIntegerField('порядок пункта')
    is_required = models.BooleanField('обязателен', default=True)
    answer_type_snapshot = models.CharField(
        'тип ответа',
        max_length=16,
        choices=ChecklistItem.AnswerType.choices,
        default=ChecklistItem.AnswerType.STATUS,
    )
    display_order = models.PositiveBigIntegerField(
        'порядок показа',
        default=0,
    )
    comment_required_on_failure = models.BooleanField(
        'требовать комментарий при невыполнении',
        default=True,
    )
    allow_not_applicable = models.BooleanField(
        'разрешить «не применимо»',
        default=False,
    )
    created_at = models.DateTimeField('создан', auto_now_add=True)

    class Meta:
        verbose_name = 'снимок пункта чек-листа'
        verbose_name_plural = 'снимки пунктов чек-листов'
        ordering = ('section_sort_order', 'display_order', 'id')
        indexes = [
            models.Index(
                fields=('daily_checklist', 'section_code', 'display_order'),
                name='daily_stage_display_idx',
            ),
        ]

    def __str__(self):
        return self.item_text

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError('Исторический снимок пункта нельзя изменять.')
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('Исторический снимок пункта нельзя удалить.')


class ChecklistAnswer(models.Model):
    class Status(models.TextChoices):
        PENDING = 'pending', 'Ожидает ответа'
        COMPLETED = 'completed', 'Выполнено'
        FAILED = 'failed', 'Не выполнено'
        NOT_APPLICABLE = 'not_applicable', 'Не применимо'

    daily_item = models.OneToOneField(
        DailyChecklistItem,
        verbose_name='пункт ежедневного чек-листа',
        on_delete=models.CASCADE,
        related_name='answer',
    )
    status = models.CharField(
        'статус',
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
        null=True,
        blank=True,
    )
    integer_value = models.PositiveIntegerField(
        'целочисленный ответ',
        null=True,
        blank=True,
    )
    comment = models.TextField('комментарий', blank=True)
    answered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name='ответил',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='checklist_answers',
    )
    answered_by_employee = models.ForeignKey(
        StoreEmployee,
        verbose_name='первым ответил сотрудник',
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name='answers_given',
    )
    last_edited_by_employee = models.ForeignKey(
        StoreEmployee,
        verbose_name='последним изменил сотрудник',
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name='answers_last_edited',
    )
    answered_at = models.DateTimeField('время ответа', null=True, blank=True)
    updated_at = models.DateTimeField('изменён', auto_now=True)

    class Meta:
        verbose_name = 'ответ на пункт чек-листа'
        verbose_name_plural = 'ответы на пункты чек-листа'
        constraints = [
            models.CheckConstraint(
                condition=Q(status__isnull=True) | Q(integer_value__isnull=True),
                name='answer_status_integer_exclusive',
            ),
        ]

    def __str__(self):
        value = (
            self.integer_value
            if self.daily_item.answer_type_snapshot
            == ChecklistItem.AnswerType.INTEGER
            else self.get_status_display()
        )
        return f'{self.daily_item}: {value}'

    def clean(self):
        errors = {}
        if self.daily_item_id:
            item = self.daily_item
            if item.answer_type_snapshot == ChecklistItem.AnswerType.INTEGER:
                if self.status is not None:
                    errors['status'] = (
                        'Для числового вопроса нельзя сохранять статус.'
                    )
                if self.integer_value is None:
                    errors['integer_value'] = 'Укажите целое число.'
                elif (
                    isinstance(self.integer_value, bool)
                    or not isinstance(self.integer_value, int)
                    or self.integer_value < 0
                ):
                    errors['integer_value'] = (
                        'Укажите целое неотрицательное число.'
                    )
                if self.comment:
                    errors['comment'] = (
                        'Комментарий не используется для числового вопроса.'
                    )
            else:
                if self.status is None:
                    errors['status'] = 'Для статусного вопроса нужен статус.'
                if self.integer_value is not None:
                    errors['integer_value'] = (
                        'Числовое значение нельзя сохранить для статусного вопроса.'
                    )
                if (
                    self.status == self.Status.FAILED
                    and item.comment_required_on_failure
                    and not self.comment.strip()
                ):
                    errors['comment'] = (
                        'Для невыполненного пункта нужен комментарий.'
                    )
                if (
                    self.status == self.Status.NOT_APPLICABLE
                    and not item.allow_not_applicable
                ):
                    errors['status'] = (
                        'Для этого пункта нельзя выбрать «не применимо».'
                    )
            daily_status = DailyChecklistItem.objects.values_list(
                'daily_checklist__status', flat=True
            ).get(pk=self.daily_item_id)
            if self.status == self.Status.PENDING and daily_status == DailyChecklist.Status.COMPLETED:
                errors['status'] = 'Завершённый чек-лист не может иметь ожидающие ответы.'
            if self.pk and daily_status == DailyChecklist.Status.COMPLETED:
                original = type(self).objects.get(pk=self.pk)
                changed = any(
                    getattr(original, field) != getattr(self, field)
                    for field in (
                        'status',
                        'integer_value',
                        'comment',
                        'answered_by_id',
                        'answered_at',
                    )
                )
                if changed:
                    errors['daily_item'] = (
                        'Ответ завершённого чек-листа нельзя изменять.'
                    )
        if errors:
            raise ValidationError(errors)

    def full_clean(self, *args, **kwargs):
        if self.integer_value is not None and (
            isinstance(self.integer_value, bool)
            or not isinstance(self.integer_value, int)
        ):
            raise ValidationError(
                {'integer_value': 'Укажите целое неотрицательное число.'}
            )
        return super().full_clean(*args, **kwargs)

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class AnswerRevision(models.Model):
    answer = models.ForeignKey(
        ChecklistAnswer,
        verbose_name='ответ',
        on_delete=models.PROTECT,
        related_name='revisions',
    )
    daily_item = models.ForeignKey(
        DailyChecklistItem,
        verbose_name='пункт ежедневного чек-листа',
        on_delete=models.PROTECT,
        related_name='answer_revisions',
    )
    changed_by_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name='технический пользователь',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='answer_revisions',
    )
    changed_by_employee = models.ForeignKey(
        StoreEmployee,
        verbose_name='сотрудник',
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name='answer_revisions',
    )
    previous_status = models.CharField(
        'предыдущий статус',
        max_length=20,
        choices=ChecklistAnswer.Status.choices,
        null=True,
        blank=True,
    )
    new_status = models.CharField(
        'новый статус',
        max_length=20,
        choices=ChecklistAnswer.Status.choices,
        null=True,
        blank=True,
    )
    previous_integer_value = models.PositiveIntegerField(
        'предыдущее числовое значение',
        null=True,
        blank=True,
    )
    new_integer_value = models.PositiveIntegerField(
        'новое числовое значение',
        null=True,
        blank=True,
    )
    previous_comment = models.TextField('предыдущий комментарий', blank=True)
    new_comment = models.TextField('новый комментарий', blank=True)
    change_reason = models.TextField('причина изменения')
    changed_at = models.DateTimeField('изменено', auto_now_add=True)
    ip_address = models.GenericIPAddressField('IP-адрес', null=True, blank=True)
    user_agent = models.TextField('User-Agent', null=True, blank=True)

    class Meta:
        verbose_name = 'ревизия ответа'
        verbose_name_plural = 'ревизии ответов'
        ordering = ('-changed_at', '-id')

    def __str__(self):
        return f'Изменение ответа #{self.answer_id}'

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError('Ревизию ответа нельзя изменять.')
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('Ревизию ответа нельзя удалить.')


class DailyShiftAssignment(models.Model):
    class ShiftType(models.TextChoices):
        WORK = 'work', 'Работа'
        NIGHT = 'night', 'Ночная смена'
        DAY_OFF = 'day_off', 'Выходной'
        VACATION = 'vacation', 'Отпуск'
        SICK_LEAVE = 'sick_leave', 'Больничный'
        SERVICE = 'service', 'Смена сервис'
        PERSONAL = 'personal', 'Личное отсутствие'

    store = models.ForeignKey(
        Store,
        verbose_name='магазин',
        on_delete=models.PROTECT,
        related_name='shift_assignments',
    )
    employee = models.ForeignKey(
        StoreEmployee,
        verbose_name='сотрудник',
        on_delete=models.PROTECT,
        related_name='shift_assignments',
    )
    work_date = models.DateField('дата работы')
    shift_type = models.CharField(
        'тип смены',
        max_length=20,
        choices=ShiftType.choices,
        default=ShiftType.WORK,
    )
    is_responsible_for_checklist = models.BooleanField(
        'ответственный за чек-лист',
        default=True,
    )
    shift_start = models.TimeField('начало смены', null=True, blank=True)
    shift_end = models.TimeField('окончание смены', null=True, blank=True)
    comment = models.TextField('комментарий', null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name='создал',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='created_shift_assignments',
    )
    created_at = models.DateTimeField('создано', auto_now_add=True)
    updated_at = models.DateTimeField('изменено', auto_now=True)

    class Meta:
        verbose_name = 'назначение на смену'
        verbose_name_plural = 'назначения на смену'
        ordering = ('-work_date', 'employee__sort_order', 'employee__display_name')
        constraints = [
            models.UniqueConstraint(
                fields=('store', 'employee', 'work_date'),
                name='unique_store_employee_work_date',
            ),
        ]

    def __str__(self):
        return f'{self.work_date:%d.%m.%Y}: {self.employee.display_name}'

    def clean(self):
        errors = {}
        if self.shift_type in {
            self.ShiftType.DAY_OFF,
            self.ShiftType.VACATION,
            self.ShiftType.SICK_LEAVE,
            self.ShiftType.PERSONAL,
        }:
            self.shift_start = None
            self.shift_end = None
            self.is_responsible_for_checklist = False
        if self.employee_id:
            if self.employee.store_id != self.store_id:
                errors['employee'] = 'Сотрудник относится к другому магазину.'
            if not self.employee.is_active:
                errors['employee'] = 'Нельзя назначить неактивного сотрудника.'
        if self.shift_start and self.shift_end and self.shift_start >= self.shift_end:
            errors['shift_end'] = 'Окончание смены должно быть позже начала.'
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    @property
    def short_code(self):
        return {
            self.ShiftType.WORK: 'Д',
            self.ShiftType.NIGHT: 'Н',
            self.ShiftType.DAY_OFF: 'В',
            self.ShiftType.VACATION: 'О',
            self.ShiftType.SICK_LEAVE: 'Б',
            self.ShiftType.SERVICE: 'С',
            self.ShiftType.PERSONAL: 'Л',
        }[self.shift_type]


class ShiftTemplate(models.Model):
    store = models.ForeignKey(
        Store,
        verbose_name='магазин',
        on_delete=models.CASCADE,
        related_name='shift_templates',
    )
    name = models.CharField('название', max_length=100)
    shift_type = models.CharField(
        'тип смены',
        max_length=20,
        choices=DailyShiftAssignment.ShiftType.choices,
        default=DailyShiftAssignment.ShiftType.WORK,
    )
    shift_start = models.TimeField('начало смены', null=True, blank=True)
    shift_end = models.TimeField('окончание смены', null=True, blank=True)
    is_active = models.BooleanField('активен', default=True)
    sort_order = models.PositiveIntegerField('порядок', default=0)
    created_at = models.DateTimeField('создан', auto_now_add=True)
    updated_at = models.DateTimeField('изменён', auto_now=True)

    class Meta:
        verbose_name = 'шаблон смены'
        verbose_name_plural = 'шаблоны смен'
        ordering = ('sort_order', 'name', 'id')
        constraints = [
            models.UniqueConstraint(
                fields=('store', 'name'),
                name='unique_store_shift_template_name',
            ),
        ]

    def __str__(self):
        return f'{self.name} — {self.store.name}'

    def clean(self):
        if (
            self.shift_start
            and self.shift_end
            and self.shift_start >= self.shift_end
        ):
            raise ValidationError(
                {'shift_end': 'Окончание смены должно быть позже начала.'}
            )
        if self.shift_type in {
            DailyShiftAssignment.ShiftType.DAY_OFF,
            DailyShiftAssignment.ShiftType.VACATION,
            DailyShiftAssignment.ShiftType.SICK_LEAVE,
            DailyShiftAssignment.ShiftType.PERSONAL,
        }:
            self.shift_start = None
            self.shift_end = None

    def save(self, *args, **kwargs):
        self.name = self.name.strip()
        self.full_clean()
        return super().save(*args, **kwargs)


class AuditLog(models.Model):
    class Action(models.TextChoices):
        DAILY_CHECKLIST_CREATED = (
            'daily_checklist_created',
            'Создан ежедневный чек-лист',
        )
        ANSWER_STATUS_CHANGED = 'answer_status_changed', 'Изменён статус ответа'
        ANSWER_COMMENT_CHANGED = (
            'answer_comment_changed',
            'Изменён комментарий ответа',
        )
        DAILY_CHECKLIST_COMPLETED = (
            'daily_checklist_completed',
            'Чек-лист завершён',
        )
        DAILY_CHECKLIST_REOPENED = (
            'daily_checklist_reopened',
            'Чек-лист открыт повторно',
        )
        CHECKLIST_STAGE_COMPLETED = (
            'checklist_stage_completed',
            'Этап чек-листа завершён',
        )
        TELEGRAM_NOTIFICATION_SENT = (
            'telegram_notification_sent',
            'Telegram-уведомление отправлено',
        )
        TELEGRAM_NOTIFICATION_FAILED = (
            'telegram_notification_failed',
            'Ошибка Telegram-уведомления',
        )
        ANSWER_REVISED = 'answer_revised', 'Ответ изменён с указанием причины'
        TEMPLATE_VERSION_PUBLISHED = (
            'template_version_published',
            'Версия шаблона опубликована',
        )
        STORE_CREATED = 'store_created', 'Магазин создан'
        STORE_UPDATED = 'store_updated', 'Магазин изменён'
        STORE_ACTIVATED = 'store_activated', 'Магазин активирован'
        STORE_DEACTIVATED = 'store_deactivated', 'Магазин деактивирован'
        STORE_DELETED = 'store_deleted', 'Магазин удалён'
        STORE_DEACTIVATED_WITH_HISTORY = (
            'store_deactivated_with_history',
            'Магазин с историей деактивирован',
        )
        AUDIT_LOG_CLEARED = 'audit_log_cleared', 'Журнал действий очищен'
        USER_CREATED = 'user_created', 'Пользователь создан'
        USER_UPDATED = 'user_updated', 'Пользователь изменён'
        USER_ACTIVATED = 'user_activated', 'Пользователь активирован'
        USER_DEACTIVATED = 'user_deactivated', 'Пользователь деактивирован'
        USER_ROLE_CHANGED = 'user_role_changed', 'Роль пользователя изменена'
        USER_STORE_CHANGED = 'user_store_changed', 'Магазин пользователя изменён'
        USER_PASSWORD_RESET = 'user_password_reset', 'Пароль пользователя сброшен'
        USER_DELETED = 'user_deleted', 'Пользователь удалён'
        USER_STORE_MEMBERSHIP_CREATED = (
            'user_store_membership_created',
            'Пользователь добавлен в магазин',
        )
        USER_STORE_MEMBERSHIP_UPDATED = (
            'user_store_membership_updated',
            'Роль пользователя в магазине изменена',
        )
        USER_STORE_MEMBERSHIP_DELETED = (
            'user_store_membership_deleted',
            'Пользователь удалён из магазина',
        )
        STORE_EMPLOYEE_CREATED = 'store_employee_created', 'Сотрудник создан'
        STORE_EMPLOYEE_UPDATED = 'store_employee_updated', 'Сотрудник изменён'
        STORE_EMPLOYEE_ACTIVATED = 'store_employee_activated', 'Сотрудник активирован'
        STORE_EMPLOYEE_DEACTIVATED = 'store_employee_deactivated', 'Сотрудник деактивирован'
        CHECKLIST_QUESTION_CREATED = 'checklist_question_created', 'Вопрос создан'
        CHECKLIST_QUESTION_UPDATED = 'checklist_question_updated', 'Вопрос изменён'
        CHECKLIST_QUESTION_ACTIVATED = 'checklist_question_activated', 'Вопрос активирован'
        CHECKLIST_QUESTION_DEACTIVATED = 'checklist_question_deactivated', 'Вопрос деактивирован'
        CHECKLIST_QUESTION_DELETED = 'checklist_question_deleted', 'Вопрос удалён'
        CHECKLIST_QUESTION_REMOVED_FROM_TEMPLATE = (
            'checklist_question_removed_from_template',
            'Вопрос исключён из новых чек-листов',
        )
        CHECKLIST_QUESTIONS_REORDERED = 'checklist_questions_reordered', 'Вопросы переупорядочены'
        STORE_SCHEDULE_UPDATED = 'store_schedule_updated', 'Расписание магазина изменено'
        STORE_DAY_STATUS_UPDATED = (
            'store_day_status_updated',
            'Статус дня магазина изменён',
        )
        STORE_NOTIFICATION_SETTINGS_UPDATED = (
            'store_notification_settings_updated',
            'Настройки уведомлений изменены',
        )
        TELEGRAM_TEST_MESSAGE_SENT = 'telegram_test_message_sent', 'Тест Telegram отправлен'
        STORE_TASK_DELETED = 'store_task_deleted', 'Задача магазина удалена'
        CHECKLIST_STAGE_REOPENED = 'checklist_stage_reopened', 'Этап повторно открыт'
        SHIFT_ASSIGNMENT_CREATED = 'shift_assignment_created', 'Назначение на смену создано'
        SHIFT_ASSIGNMENT_UPDATED = 'shift_assignment_updated', 'Назначение на смену изменено'
        SHIFT_ASSIGNMENT_DELETED = 'shift_assignment_deleted', 'Назначение на смену удалено'
        SHIFT_ASSIGNMENTS_BULK_CREATED = (
            'shift_assignments_bulk_created',
            'Назначения на смены созданы массово',
        )
        SHIFT_TEMPLATE_CREATED = 'shift_template_created', 'Шаблон смены создан'
        SHIFT_TEMPLATE_UPDATED = 'shift_template_updated', 'Шаблон смены изменён'
        SHIFT_TEMPLATE_DELETED = 'shift_template_deleted', 'Шаблон смены удалён'
        EMPLOYEE_SCHEDULE_REMINDER_QUEUED = (
            'employee_schedule_reminder_queued',
            'Напоминание о графике поставлено в очередь',
        )
        TELEGRAM_SYSTEM_SETTINGS_UPDATED = (
            'telegram_system_settings_updated',
            'Системные настройки Telegram изменены',
        )
        TELEGRAM_STORE_CHAT_CREATED = (
            'telegram_store_chat_created',
            'Telegram-чат создан',
        )
        TELEGRAM_STORE_CHAT_UPDATED = (
            'telegram_store_chat_updated',
            'Telegram-чат изменён',
        )
        TELEGRAM_STORE_CHAT_DELETED = (
            'telegram_store_chat_deleted',
            'Telegram-чат удалён',
        )
        TELEGRAM_TEMPLATE_UPDATED = (
            'telegram_template_updated',
            'Шаблон Telegram изменён',
        )
        TELEGRAM_TEMPLATE_CREATED = (
            'telegram_template_created',
            'Шаблон Telegram создан',
        )
        TELEGRAM_TEMPLATE_DELETED = (
            'telegram_template_deleted',
            'Шаблон Telegram удалён',
        )
        TELEGRAM_TEMPLATE_ENABLED = (
            'telegram_template_enabled',
            'Шаблон Telegram включён',
        )
        TELEGRAM_TEMPLATE_DISABLED = (
            'telegram_template_disabled',
            'Шаблон Telegram выключен',
        )
        TELEGRAM_TEMPLATE_RESET = (
            'telegram_template_reset',
            'Шаблон Telegram восстановлен',
        )
        TELEGRAM_TEMPLATE_TEST_SENT = (
            'telegram_template_test_sent',
            'Тест шаблона Telegram поставлен в очередь',
        )
        TELEGRAM_BINDING_APPROVED = (
            'telegram_binding_approved',
            'Привязка Telegram подтверждена',
        )
        TELEGRAM_BINDING_DISABLED = (
            'telegram_binding_disabled',
            'Привязка Telegram отключена',
        )
        TELEGRAM_PROFILE_REASSIGNED = (
            'telegram_profile_reassigned',
            'Пользователь Telegram-привязки изменён',
        )
        TELEGRAM_PROFILE_DISCONNECTED = (
            'telegram_profile_disconnected',
            'Telegram отвязан от пользователя',
        )
        TELEGRAM_TASK_CREATED = (
            'telegram_task_created',
            'Разовая задача создана',
        )
        TELEGRAM_TASK_COMPLETED = (
            'telegram_task_completed',
            'Разовая задача выполнена',
        )
        TELEGRAM_TASK_FAILED = (
            'telegram_task_failed',
            'Разовая задача не выполнена',
        )
        TELEGRAM_TEST_SENT = 'telegram_test_sent', 'Тест Telegram поставлен в очередь'
        TELEGRAM_DELIVERY_FAILED = (
            'telegram_delivery_failed',
            'Доставка Telegram завершилась ошибкой',
        )
        TELEGRAM_WEBHOOK_REGISTERED = (
            'telegram_webhook_registered',
            'Webhook Telegram зарегистрирован',
        )
        TELEGRAM_WEBHOOK_DELETED = (
            'telegram_webhook_deleted',
            'Webhook Telegram удалён',
        )
        TELEGRAM_WEBHOOK_CHECKED = (
            'telegram_webhook_checked',
            'Webhook Telegram проверен',
        )
        TELEGRAM_INCOMING_MODE_CHANGED = (
            'telegram_incoming_mode_changed',
            'Режим входящих Telegram изменён',
        )
        TELEGRAM_INBOUND_JOB_RETRIED = (
            'telegram_inbound_job_retried',
            'Входящая Telegram-команда повторена',
        )
        STORE_TASK_CREATED_BY_ADMIN = (
            'store_task_created_by_admin',
            'Разовая задача создана через кабинет',
        )
        STORE_TASK_UPDATED = (
            'store_task_updated',
            'Разовая задача изменена',
        )
        STORE_TASK_CANCELLED = (
            'store_task_cancelled',
            'Разовая задача отменена',
        )

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name='инициатор',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='checklist_audit_logs',
    )
    employee = models.ForeignKey(
        StoreEmployee,
        verbose_name='фактический сотрудник',
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name='audit_logs',
    )
    store = models.ForeignKey(
        Store,
        verbose_name='магазин',
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name='audit_logs',
    )
    object_type = models.CharField('тип объекта', max_length=100)
    object_id = models.CharField('ID объекта', max_length=255)
    action = models.CharField('действие', max_length=50, choices=Action.choices)
    field_name = models.CharField('поле', max_length=100, blank=True)
    old_value = models.JSONField('старое значение', null=True, blank=True)
    new_value = models.JSONField('новое значение', null=True, blank=True)
    ip_address = models.GenericIPAddressField('IP-адрес', null=True, blank=True)
    user_agent = models.TextField('User-Agent', null=True, blank=True)
    created_at = models.DateTimeField('создан', auto_now_add=True)

    class Meta:
        verbose_name = 'запись журнала изменений'
        verbose_name_plural = 'журнал изменений'
        ordering = ('-created_at', '-id')

    def __str__(self):
        return f'{self.created_at:%d.%m.%Y %H:%M} — {self.get_action_display()}'


class TelegramSystemSettings(models.Model):
    class IncomingMode(models.TextChoices):
        WEBHOOK = 'webhook', 'Webhook'
        POLLING = 'polling', 'Polling'

    bot_token = models.CharField('токен бота', max_length=255, blank=True)
    alternative_api_base_url = models.URLField(
        'URL альтернативного шлюза',
        default='https://tauto.gerbud.ru',
    )
    use_alternative_gateway = models.BooleanField(
        'использовать альтернативный шлюз',
        default=True,
    )
    fallback_to_official_api = models.BooleanField(
        'использовать официальный API при ошибке',
        default=True,
    )
    alternative_attempts = models.PositiveSmallIntegerField(
        'попыток через альтернативный шлюз',
        default=5,
    )
    official_attempts = models.PositiveSmallIntegerField(
        'попыток через официальный API',
        default=5,
    )
    request_timeout_seconds = models.PositiveSmallIntegerField(
        'таймаут запроса, секунд',
        default=10,
    )
    retry_delay_seconds = models.PositiveSmallIntegerField(
        'задержка между попытками, секунд',
        default=1,
    )
    is_enabled = models.BooleanField('интеграция включена', default=False)
    incoming_mode = models.CharField(
        'режим входящих сообщений',
        max_length=16,
        choices=IncomingMode.choices,
        default=IncomingMode.WEBHOOK,
    )
    webhook_url = models.URLField('URL webhook', blank=True)
    webhook_secret_token = models.CharField(
        'секрет webhook',
        max_length=255,
        blank=True,
    )
    webhook_is_enabled = models.BooleanField(
        'webhook зарегистрирован',
        default=False,
    )
    webhook_registered_at = models.DateTimeField(
        'webhook зарегистрирован',
        null=True,
        blank=True,
    )
    webhook_last_checked_at = models.DateTimeField(
        'webhook проверен',
        null=True,
        blank=True,
    )
    webhook_last_error = models.TextField('последняя ошибка webhook', blank=True)
    webhook_max_connections = models.PositiveSmallIntegerField(
        'максимум соединений webhook',
        default=20,
    )
    webhook_allowed_updates = models.JSONField(
        'разрешённые типы updates',
        null=True,
        blank=True,
        default=list,
    )
    immediate_ack_enabled = models.BooleanField(
        'немедленное подтверждение',
        default=True,
    )
    immediate_ack_text = models.CharField(
        'текст подтверждения',
        max_length=255,
        default='Принято',
    )
    bot_commands_registered_at = models.DateTimeField(
        'команды бота зарегистрированы',
        null=True,
        blank=True,
    )
    bot_commands_last_checked_at = models.DateTimeField(
        'команды бота проверены',
        null=True,
        blank=True,
    )
    bot_commands_last_error = models.TextField(
        'последняя ошибка команд бота',
        blank=True,
    )
    updated_at = models.DateTimeField('изменено', auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name='изменил',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='telegram_system_settings_updates',
    )

    class Meta:
        verbose_name = 'системная настройка Telegram'
        verbose_name_plural = 'системные настройки Telegram'

    def __str__(self):
        return 'Системные настройки Telegram'

    @classmethod
    def get_solo(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    @property
    def masked_token(self):
        if not self.bot_token:
            return 'не задан'
        if len(self.bot_token) <= 8:
            return '••••••••'
        return f'{self.bot_token[:4]}…{self.bot_token[-4:]}'

    @property
    def masked_webhook_secret(self):
        if not self.webhook_secret_token:
            return 'не задан'
        return '••••••••'

    def clean(self):
        errors = {}
        for field_name in (
            'alternative_attempts',
            'official_attempts',
            'request_timeout_seconds',
        ):
            value = getattr(self, field_name)
            if value is None or value < 1:
                errors[field_name] = 'Значение должно быть не меньше 1.'
        if self.alternative_attempts and self.alternative_attempts > 20:
            errors['alternative_attempts'] = 'Допустимо не более 20 попыток.'
        if self.official_attempts and self.official_attempts > 20:
            errors['official_attempts'] = 'Допустимо не более 20 попыток.'
        if not 1 <= self.webhook_max_connections <= 100:
            errors['webhook_max_connections'] = 'Допустимо от 1 до 100.'
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        if self.pk not in (None, 1):
            raise ValidationError('Допустима только одна системная настройка.')
        self.pk = 1
        self.alternative_api_base_url = self.alternative_api_base_url.rstrip('/')
        self.full_clean()
        return super().save(*args, **kwargs)


class TelegramOutboundMessage(models.Model):
    class Status(models.TextChoices):
        PENDING = 'pending', 'Ожидает'
        PROCESSING = 'processing', 'Обрабатывается'
        SENT = 'sent', 'Отправлено'
        FAILED = 'failed', 'Ошибка'
        DELETED = 'deleted', 'Удалено в Telegram'

    store = models.ForeignKey(
        Store,
        verbose_name='магазин',
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name='telegram_outbound_messages',
    )
    chat_id = models.CharField('Telegram chat ID', max_length=255)
    message_thread_id = models.BigIntegerField(
        'Telegram Topic ID',
        null=True,
        blank=True,
    )
    method = models.CharField('метод Telegram API', max_length=64, default='sendMessage')
    payload = models.JSONField('безопасная нагрузка', default=dict)
    message_type = models.CharField('тип сообщения', max_length=64)
    idempotency_key = models.CharField(
        'ключ идемпотентности',
        max_length=255,
        unique=True,
    )
    status = models.CharField(
        'статус',
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
    )
    alternative_attempts_count = models.PositiveSmallIntegerField(
        'попыток через альтернативный шлюз',
        default=0,
    )
    official_attempts_count = models.PositiveSmallIntegerField(
        'попыток через официальный API',
        default=0,
    )
    last_error = models.TextField('последняя ошибка', blank=True)
    telegram_message_id = models.BigIntegerField(
        'Telegram message ID',
        null=True,
        blank=True,
    )
    scheduled_at = models.DateTimeField('запланировано', default=timezone.now)
    sent_at = models.DateTimeField('отправлено', null=True, blank=True)
    deleted_at = models.DateTimeField('удалено в Telegram', null=True, blank=True)
    deleted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name='удалил',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='deleted_telegram_messages',
    )
    created_at = models.DateTimeField('создано', auto_now_add=True)
    updated_at = models.DateTimeField('изменено', auto_now=True)

    class Meta:
        verbose_name = 'исходящее сообщение Telegram'
        verbose_name_plural = 'исходящие сообщения Telegram'
        ordering = ('scheduled_at', 'id')
        indexes = [
            models.Index(
                fields=('status', 'scheduled_at'),
                name='tg_out_status_sched_idx',
            ),
            models.Index(
                fields=('store', 'status'),
                name='tg_out_store_status_idx',
            ),
        ]

    def __str__(self):
        return f'{self.message_type}: {self.get_status_display()}'

    def clean(self):
        if not isinstance(self.payload, dict):
            raise ValidationError({'payload': 'Payload должен быть JSON-объектом.'})
        forbidden = {'bot_token', 'token', 'csrfmiddlewaretoken', 'sessionid'}
        if forbidden.intersection(key.lower() for key in self.payload):
            raise ValidationError({'payload': 'Payload содержит запрещённые секреты.'})

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class TelegramStoreChat(models.Model):
    class ChatType(models.TextChoices):
        PRIVATE = 'private', 'Личный чат'
        GROUP = 'group', 'Группа'
        SUPERGROUP = 'supergroup', 'Супергруппа'
        CHANNEL = 'channel', 'Канал'

    class Purpose(models.TextChoices):
        NOTIFICATIONS = 'notifications', 'Уведомления'
        TASKS = 'tasks', 'Задачи'
        FAILURES = 'failures', 'Невыполненные задачи'
        ALL = 'all', 'Все сообщения'

    store = models.ForeignKey(
        Store,
        verbose_name='магазин',
        on_delete=models.CASCADE,
        related_name='telegram_chats',
    )
    title = models.CharField('название', max_length=255)
    chat_id = models.CharField('Telegram chat ID', max_length=255)
    chat_type = models.CharField(
        'тип чата',
        max_length=16,
        choices=ChatType.choices,
    )
    message_thread_id = models.BigIntegerField(
        'Telegram Topic ID',
        null=True,
        blank=True,
    )
    purpose = models.CharField(
        'назначение',
        max_length=20,
        choices=Purpose.choices,
        default=Purpose.ALL,
    )
    is_active = models.BooleanField('активен', default=True)
    created_at = models.DateTimeField('создан', auto_now_add=True)
    updated_at = models.DateTimeField('изменён', auto_now=True)

    class Meta:
        verbose_name = 'Telegram-чат магазина'
        verbose_name_plural = 'Telegram-чаты магазинов'
        ordering = ('store', 'title', 'id')
        constraints = [
            models.UniqueConstraint(
                fields=('store', 'chat_id', 'message_thread_id', 'purpose'),
                name='unique_store_telegram_destination',
            ),
        ]

    def __str__(self):
        topic = f' / topic {self.message_thread_id}' if self.message_thread_id else ''
        return f'{self.store}: {self.title}{topic}'


class TelegramPendingBinding(models.Model):
    class Status(models.TextChoices):
        PENDING = 'pending', 'Ожидает'
        APPROVED = 'approved', 'Подтверждена'
        REJECTED = 'rejected', 'Отклонена'
        EXPIRED = 'expired', 'Истекла'

    telegram_user_id = models.BigIntegerField('Telegram user ID')
    telegram_chat_id = models.BigIntegerField('Telegram chat ID')
    username = models.CharField('username', max_length=64, blank=True)
    first_name = models.CharField('имя', max_length=128, blank=True)
    last_name = models.CharField('фамилия', max_length=128, blank=True)
    one_time_code = models.CharField('одноразовый код', max_length=12, unique=True)
    status = models.CharField(
        'статус',
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
    )
    created_at = models.DateTimeField('создана', auto_now_add=True)
    expires_at = models.DateTimeField('истекает')
    update_id = models.BigIntegerField('Telegram update ID', unique=True)

    class Meta:
        verbose_name = 'ожидающая привязка Telegram'
        verbose_name_plural = 'ожидающие привязки Telegram'
        ordering = ('-created_at',)

    def __str__(self):
        return f'{self.telegram_user_id}: {self.get_status_display()}'


class TelegramStoreBinding(models.Model):
    store = models.ForeignKey(
        Store,
        verbose_name='магазин',
        on_delete=models.PROTECT,
        related_name='telegram_bindings',
    )
    telegram_user_id = models.BigIntegerField('Telegram user ID', unique=True)
    telegram_chat_id = models.BigIntegerField('Telegram chat ID')
    username = models.CharField('username', max_length=64, blank=True)
    first_name = models.CharField('имя', max_length=128, blank=True)
    last_name = models.CharField('фамилия', max_length=128, blank=True)
    is_active = models.BooleanField('активна', default=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name='подтвердил',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='approved_telegram_bindings',
    )
    approved_at = models.DateTimeField('подтверждена', default=timezone.now)
    created_at = models.DateTimeField('создана', auto_now_add=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name='пользователь сайта',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='telegram_store_bindings',
    )

    class Meta:
        verbose_name = 'привязка Telegram к магазину'
        verbose_name_plural = 'привязки Telegram к магазинам'
        ordering = ('store', '-is_active', 'telegram_user_id')

    def __str__(self):
        return f'{self.telegram_user_id} → {self.store}'

    def clean(self):
        if self.is_active and not self.store.is_active:
            raise ValidationError({'store': 'Нельзя активировать привязку магазина.'})


class TelegramUserProfile(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        verbose_name='пользователь',
        on_delete=models.CASCADE,
        related_name='telegram_profile',
    )
    telegram_user_id = models.BigIntegerField('Telegram user ID', unique=True)
    telegram_chat_id = models.BigIntegerField('Telegram chat ID')
    telegram_username = models.CharField('username', max_length=64, blank=True)
    first_name = models.CharField('имя', max_length=128, blank=True)
    last_name = models.CharField('фамилия', max_length=128, blank=True)
    is_verified = models.BooleanField('подтверждён', default=False)
    created_at = models.DateTimeField('создан', auto_now_add=True)
    updated_at = models.DateTimeField('изменён', auto_now=True)

    class Meta:
        verbose_name = 'Telegram-профиль пользователя'
        verbose_name_plural = 'Telegram-профили пользователей'
        ordering = ('user__username',)

    def __str__(self):
        return f'{self.user.get_username()} · {self.telegram_user_id}'


class TelegramUpdateLog(models.Model):
    class ResponseStatus(models.TextChoices):
        SENT = 'sent', 'Отправлен'
        QUEUED = 'queued', 'Поставлен в очередь'
        FAILED = 'failed', 'Ошибка'
        BACKGROUND = 'background', 'Фоновая обработка'
        IGNORED = 'ignored', 'Игнорирован'

    update_id = models.BigIntegerField('Telegram update ID', unique=True)
    telegram_user_id = models.BigIntegerField(
        'Telegram user ID',
        null=True,
        blank=True,
    )
    telegram_chat_id = models.BigIntegerField(
        'Telegram chat ID',
        null=True,
        blank=True,
    )
    update_type = models.CharField('тип update', max_length=32)
    command = models.CharField('команда', max_length=64, blank=True)
    payload = models.JSONField('безопасные данные', default=dict)
    processed = models.BooleanField('обработан', default=False)
    processing_error = models.TextField('ошибка обработки', blank=True)
    response_status = models.CharField(
        'статус ответа',
        max_length=16,
        choices=ResponseStatus.choices,
        blank=True,
    )
    response_error = models.TextField('ошибка ответа', blank=True)
    created_at = models.DateTimeField('создан', auto_now_add=True)
    processed_at = models.DateTimeField('обработан в', null=True, blank=True)
    responded_at = models.DateTimeField('ответ отправлен в', null=True, blank=True)

    class Meta:
        verbose_name = 'входящий Telegram update'
        verbose_name_plural = 'входящие Telegram updates'
        ordering = ('-update_id',)

    @property
    def sender_display(self):
        message = self.payload.get('message') or {}
        callback = self.payload.get('callback_query') or {}
        source = callback.get('from') or message.get('from') or {}
        username = str(source.get('username', '')).strip()
        if username:
            return f'@{username}'
        full_name = ' '.join(
            filter(
                None,
                (
                    str(source.get('first_name', '')).strip(),
                    str(source.get('last_name', '')).strip(),
                ),
            )
        )
        return full_name or str(self.telegram_user_id or 'не определён')


class TelegramInboundJob(models.Model):
    class Status(models.TextChoices):
        PENDING = 'pending', 'Ожидает'
        PROCESSING = 'processing', 'Обрабатывается'
        COMPLETED = 'completed', 'Завершено'
        FAILED = 'failed', 'Ошибка'

    update_id = models.BigIntegerField('Telegram update ID', unique=True)
    update_log = models.OneToOneField(
        TelegramUpdateLog,
        verbose_name='журнал update',
        on_delete=models.PROTECT,
        related_name='inbound_job',
    )
    store = models.ForeignKey(
        Store,
        verbose_name='магазин',
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name='telegram_inbound_jobs',
    )
    telegram_user_id = models.BigIntegerField(
        'Telegram user ID',
        null=True,
        blank=True,
    )
    telegram_chat_id = models.BigIntegerField(
        'Telegram chat ID',
        null=True,
        blank=True,
    )
    update_type = models.CharField('тип update', max_length=32)
    command = models.CharField('команда', max_length=64, blank=True)
    status = models.CharField(
        'статус',
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
    )
    attempts_count = models.PositiveSmallIntegerField('попыток', default=0)
    available_at = models.DateTimeField('доступно с', default=timezone.now)
    locked_at = models.DateTimeField('захвачено', null=True, blank=True)
    completed_at = models.DateTimeField('завершено', null=True, blank=True)
    last_error = models.TextField('последняя ошибка', blank=True)
    created_at = models.DateTimeField('создано', auto_now_add=True)
    updated_at = models.DateTimeField('изменено', auto_now=True)

    class Meta:
        verbose_name = 'входящая Telegram-команда'
        verbose_name_plural = 'входящие Telegram-команды'
        ordering = ('available_at', 'id')
        indexes = [
            models.Index(
                fields=('status', 'available_at'),
                name='tg_inbound_status_idx',
            ),
            models.Index(
                fields=('store', 'status'),
                name='tg_inbound_store_idx',
            ),
        ]


class TelegramConversationState(models.Model):
    telegram_binding = models.OneToOneField(
        TelegramStoreBinding,
        verbose_name='привязка Telegram',
        on_delete=models.CASCADE,
        related_name='conversation_state',
    )
    state = models.CharField('состояние', max_length=64)
    data = models.JSONField('данные диалога', default=dict)
    expires_at = models.DateTimeField('истекает')
    updated_at = models.DateTimeField('изменено', auto_now=True)

    class Meta:
        verbose_name = 'состояние Telegram-диалога'
        verbose_name_plural = 'состояния Telegram-диалогов'


class StoreAdHocTask(models.Model):
    class SectionCode(models.TextChoices):
        MORNING = 'morning', 'Утро'
        DAY = 'day', 'День'
        EVENING = 'evening', 'Вечер'

    class Status(models.TextChoices):
        PLANNED = 'planned', 'Запланирована'
        ACTIVE = 'active', 'Активна'
        COMPLETED = 'completed', 'Выполнена'
        FAILED = 'failed', 'Не выполнена'
        CANCELLED = 'cancelled', 'Отменена'

    class Source(models.TextChoices):
        WEB = 'web', 'Сайт'
        TELEGRAM = 'telegram', 'Telegram'

    store = models.ForeignKey(
        Store,
        verbose_name='магазин',
        on_delete=models.PROTECT,
        related_name='ad_hoc_tasks',
    )
    date = models.DateField('дата')
    section_code = models.CharField(
        'этап',
        max_length=16,
        choices=SectionCode.choices,
    )
    text = models.TextField('текст задачи')
    description = models.TextField('описание', blank=True)
    is_required = models.BooleanField('обязательная', default=True)
    status = models.CharField(
        'статус',
        max_length=16,
        choices=Status.choices,
        default=Status.PLANNED,
    )
    source = models.CharField(
        'источник',
        max_length=16,
        choices=Source.choices,
        default=Source.WEB,
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name='создал пользователь',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='created_ad_hoc_tasks',
    )
    created_by_telegram_binding = models.ForeignKey(
        TelegramStoreBinding,
        verbose_name='создал через Telegram',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='created_ad_hoc_tasks',
    )
    completed_by_employee = models.ForeignKey(
        StoreEmployee,
        verbose_name='завершил сотрудник',
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name='completed_ad_hoc_tasks',
    )
    completion_comment = models.TextField('комментарий выполнения', blank=True)
    daily_checklist = models.ForeignKey(
        DailyChecklist,
        verbose_name='ежедневный чек-лист',
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name='ad_hoc_tasks',
    )
    daily_stage = models.ForeignKey(
        DailyChecklistStage,
        verbose_name='этап чек-листа',
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name='ad_hoc_tasks',
    )
    daily_item = models.OneToOneField(
        DailyChecklistItem,
        verbose_name='снимок задачи',
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name='ad_hoc_task',
    )
    created_at = models.DateTimeField('создана', auto_now_add=True)
    updated_at = models.DateTimeField('изменена', auto_now=True)
    completed_at = models.DateTimeField('завершена', null=True, blank=True)

    class Meta:
        verbose_name = 'разовая задача магазина'
        verbose_name_plural = 'разовые задачи магазинов'
        ordering = ('date', 'section_code', 'created_at', 'id')
        indexes = [
            models.Index(
                fields=('store', 'date', 'section_code', 'status'),
                name='ad_hoc_store_stage_idx',
            ),
        ]

    def __str__(self):
        return f'{self.store}: {self.text}'

    def clean(self):
        errors = {}
        if self.created_by_telegram_binding_id:
            if self.created_by_telegram_binding.store_id != self.store_id:
                errors['created_by_telegram_binding'] = (
                    'Привязка относится к другому магазину.'
                )
        if self.completed_by_employee_id:
            if self.completed_by_employee.store_id != self.store_id:
                errors['completed_by_employee'] = (
                    'Сотрудник относится к другому магазину.'
                )
        if errors:
            raise ValidationError(errors)


class TelegramMessageTemplate(models.Model):
    class ParseMode(models.TextChoices):
        HTML = 'HTML', 'HTML'
        MARKDOWN_V2 = 'MarkdownV2', 'MarkdownV2'
        PLAIN = 'plain', 'Без разметки'

    store = models.ForeignKey(
        Store,
        verbose_name='магазин',
        on_delete=models.CASCADE,
        related_name='telegram_message_templates',
    )
    event_code = models.CharField(
        'событие',
        max_length=40,
        choices=TELEGRAM_EVENT_CHOICES,
    )
    name = models.CharField('название', max_length=255)
    title = models.CharField('заголовок', max_length=255)
    body = models.TextField('текст')
    parse_mode = models.CharField(
        'разметка',
        max_length=16,
        choices=ParseMode.choices,
        default=ParseMode.HTML,
    )
    is_enabled = models.BooleanField('включён', default=True)
    send_to_private = models.BooleanField('отправлять лично', default=False)
    send_to_group = models.BooleanField('отправлять в группы', default=True)
    created_at = models.DateTimeField('создан', auto_now_add=True)
    updated_at = models.DateTimeField('изменён', auto_now=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name='создал',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='created_telegram_templates',
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name='изменил',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='updated_telegram_templates',
    )

    class Meta:
        verbose_name = 'шаблон сообщения Telegram'
        verbose_name_plural = 'шаблоны сообщений Telegram'
        ordering = ('store', 'event_code')
        constraints = [
            models.UniqueConstraint(
                fields=('store', 'event_code'),
                name='unique_store_telegram_template',
            ),
        ]

    def __str__(self):
        return f'{self.store}: {self.name}'
