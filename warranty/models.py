from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone


class WarrantyTelegramSettings(models.Model):
    peer_id = models.CharField('ID Telegram-группы', max_length=64, blank=True)
    chat_id = models.CharField('ID чата для Telegram Bot API', max_length=64, blank=True)
    use_forum_topics = models.BooleanField('отдельная тема на обращение', default=True)
    is_enabled = models.BooleanField('интеграция включена', default=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'настройка Telegram гарантийного отдела'
        verbose_name_plural = 'настройки Telegram гарантийного отдела'

    @classmethod
    def get_solo(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    @staticmethod
    def bot_api_chat_id(peer_id):
        value = str(peer_id or '').strip()
        if not value:
            return ''
        if value.startswith('-100'):
            return value
        if value.lstrip('-').isdigit():
            return f'-100{value.lstrip("-")}'
        raise ValidationError({'peer_id': 'Peer ID должен быть числом.'})

    def save(self, *args, **kwargs):
        if self.pk not in (None, 1):
            raise ValidationError('Допустима только одна настройка.')
        self.pk = 1
        self.chat_id = self.bot_api_chat_id(self.peer_id)
        self.full_clean()
        return super().save(*args, **kwargs)


class WarrantyClaim(models.Model):
    class Status(models.TextChoices):
        NEW = 'new', 'Новый'
        SERVICE_DECISION = 'service_decision', 'Ожидает решение СЦ'
        IN_PROGRESS = 'in_progress', 'В работе'
        CUSTOMER_WAIT = 'customer_wait', 'Ожидаем клиента'
        DIAGNOSTICS = 'diagnostics', 'Диагностика'
        PARTS_WAIT = 'parts_wait', 'Ожидаем запчасти'
        READY = 'ready', 'Готов к выдаче'
        CLOSED = 'closed', 'Закрыт'

    class WarrantyType(models.TextChoices):
        WARRANTY = 'warranty', 'По гарантии'
        NON_WARRANTY = 'non_warranty', 'Не по гарантии'

    class Priority(models.TextChoices):
        LOW = 'low', 'Низкий'
        NORMAL = 'normal', 'Обычный'
        HIGH = 'high', 'Высокий'
        CRITICAL = 'critical', 'Критический'

    source = models.CharField('источник', max_length=32, default='bitrix')
    external_id = models.PositiveBigIntegerField('ID в источнике')
    status = models.CharField('статус', max_length=32, choices=Status.choices, default=Status.NEW)
    source_status = models.CharField('исходный статус', max_length=64, blank=True)
    warranty_type = models.CharField('тип', max_length=24, choices=WarrantyType.choices, default=WarrantyType.WARRANTY)
    priority = models.CharField('приоритет', max_length=16, choices=Priority.choices, default=Priority.NORMAL)
    customer_name = models.CharField('ФИО', max_length=255, blank=True)
    phone = models.CharField('телефон', max_length=64, blank=True)
    email = models.EmailField('email', blank=True)
    external_user_id = models.CharField('ID клиента в источнике', max_length=64, blank=True)
    external_created_by_id = models.CharField('ID автора в источнике', max_length=64, blank=True)
    created_by_name = models.CharField('создал', max_length=255, blank=True)
    product_name = models.CharField('товар', max_length=500, blank=True)
    external_product_id = models.CharField('ID товара в источнике', max_length=64, blank=True)
    serial_number = models.CharField('серийный номер', max_length=255, blank=True)
    defect = models.TextField('неисправность', blank=True)
    equipment = models.TextField('комплектация', blank=True)
    comment = models.TextField('комментарий', blank=True)
    purchase_date = models.DateField('дата покупки', null=True, blank=True)
    repair_price = models.DecimalField('стоимость ремонта', max_digits=12, decimal_places=2, null=True, blank=True)
    product_remains_with_customer = models.BooleanField('товар у клиента', default=False)
    purchased_from_us = models.BooleanField('куплено у нас', default=False)
    assigned_to = models.ForeignKey(settings.AUTH_USER_MODEL, verbose_name='ответственный', null=True, blank=True, on_delete=models.SET_NULL, related_name='assigned_warranty_claims')
    due_at = models.DateTimeField('срок решения', null=True, blank=True)
    source_created_at = models.DateTimeField('создано в источнике', null=True, blank=True)
    source_updated_at = models.DateTimeField('изменено в источнике', null=True, blank=True)
    closed_at = models.DateTimeField('закрыто', null=True, blank=True)
    raw_source_data = models.JSONField('полный снимок источника', default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ('-source_created_at', '-external_id')
        constraints = [models.UniqueConstraint(fields=('source', 'external_id'), name='unique_warranty_claim_source_id')]
        indexes = [models.Index(fields=('status', 'priority')), models.Index(fields=('phone',)), models.Index(fields=('serial_number',))]

    @property
    def is_closed(self):
        return self.status == self.Status.CLOSED

    def save(self, *args, **kwargs):
        if self.is_closed and self.closed_at is None:
            self.closed_at = timezone.now()
        elif not self.is_closed:
            self.closed_at = None
        super().save(*args, **kwargs)

    def __str__(self):
        return f'#{self.external_id} {self.product_name or self.customer_name}'


class GreenworksDrawing(models.Model):
    article = models.CharField('артикул Greenworks', max_length=120, unique=True)
    links = models.JSONField('чертежи', default=list)
    refreshed_at = models.DateTimeField('обновлено', auto_now=True)

    class Meta:
        verbose_name = 'чертёж Greenworks'
        verbose_name_plural = 'чертежи Greenworks'
        ordering = ('article',)

    def __str__(self):
        return self.article


class WarrantyTelegramStatusButton(models.Model):
    source_status = models.CharField(
        'показывать при статусе', max_length=32, choices=WarrantyClaim.Status.choices,
    )
    label = models.CharField('текст кнопки', max_length=64)
    target_status = models.CharField(
        'перевести в статус', max_length=32, choices=WarrantyClaim.Status.choices,
    )
    position = models.PositiveSmallIntegerField('порядок', default=100)
    is_enabled = models.BooleanField('показывать', default=True)

    class Meta:
        verbose_name = 'кнопка Telegram для статуса гарантии'
        verbose_name_plural = 'кнопки Telegram для статусов гарантии'
        ordering = ('source_status', 'position', 'id')
        constraints = [
            models.UniqueConstraint(
                fields=('source_status', 'label'),
                name='unique_warranty_telegram_status_button',
            ),
        ]

    def __str__(self):
        return f'{self.get_source_status_display()}: {self.label} → {self.get_target_status_display()}'


class WarrantyAttachment(models.Model):
    claim = models.ForeignKey(WarrantyClaim, on_delete=models.CASCADE, related_name='attachments')
    external_file_id = models.CharField('ID файла в источнике', max_length=64)
    file = models.FileField('файл', upload_to='warranty/attachments/%Y/%m/', blank=True)
    original_name = models.CharField('имя файла', max_length=500, blank=True)
    content_type = models.CharField(max_length=255, blank=True)
    size = models.PositiveBigIntegerField(default=0)
    checksum_sha256 = models.CharField(max_length=64, blank=True)
    source_path = models.CharField(max_length=1000, blank=True)
    imported_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=('claim', 'external_file_id'), name='unique_warranty_attachment_source_id')]


class WarrantyHistoryEvent(models.Model):
    class Kind(models.TextChoices):
        IMPORT = 'import', 'Импорт'
        CHANGE = 'change', 'Изменение'
        MESSAGE = 'message', 'Сообщение'
        TELEGRAM = 'telegram', 'Telegram'
        SYSTEM = 'system', 'Система'

    claim = models.ForeignKey(WarrantyClaim, on_delete=models.CASCADE, related_name='history')
    external_id = models.CharField(max_length=64, blank=True)
    kind = models.CharField(max_length=16, choices=Kind.choices, default=Kind.CHANGE)
    text = models.TextField()
    actor_external_id = models.CharField(max_length=64, blank=True)
    actor_name = models.CharField(max_length=255, blank=True)
    occurred_at = models.DateTimeField(default=timezone.now)
    payload = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ('occurred_at', 'id')
        constraints = [models.UniqueConstraint(fields=('claim', 'external_id'), condition=~models.Q(external_id=''), name='unique_warranty_history_external_id')]


class WarrantyWorkItem(models.Model):
    class Kind(models.TextChoices):
        DIAGNOSTIC = 'diagnostic', 'Диагностика'
        LABOR = 'labor', 'Работа'
        PART = 'part', 'Запчасть'
        DELIVERY = 'delivery', 'Доставка'

    claim = models.ForeignKey(WarrantyClaim, on_delete=models.CASCADE, related_name='work_items')
    kind = models.CharField(max_length=16, choices=Kind.choices)
    name = models.CharField(max_length=500)
    sku = models.CharField(max_length=120, blank=True)
    quantity = models.DecimalField(max_digits=10, decimal_places=2, default=1)
    unit_price = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    is_warranty_covered = models.BooleanField(default=True)
    is_completed = models.BooleanField(default=False)
    notes = models.TextField(blank=True)


class WarrantyBitrixOutbox(models.Model):
    class Status(models.TextChoices):
        PENDING = 'pending', 'Ожидает отправки'
        SENDING = 'sending', 'Отправляется'
        SENT = 'sent', 'Отправлено'
        ERROR = 'error', 'Ошибка'

    claim = models.ForeignKey(WarrantyClaim, on_delete=models.CASCADE, related_name='bitrix_outbox')
    payload = models.JSONField(default=dict)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    attempts = models.PositiveIntegerField(default=0)
    last_error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ('id',)
        indexes = [models.Index(fields=('status', 'id'))]


class WarrantyBitrixSyncState(models.Model):
    claim_cursor = models.PositiveBigIntegerField(default=0)
    history_cursor = models.PositiveBigIntegerField(default=0)
    last_success_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    @classmethod
    def get_solo(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class WarrantyTelegramThread(models.Model):
    class State(models.TextChoices):
        PLANNED = 'planned', 'Ожидает создания'
        CREATING = 'creating', 'Создаётся'
        ACTIVE = 'active', 'Активна'
        CLOSE_PENDING = 'close_pending', 'Ожидает закрытия'
        ARCHIVED = 'archived', 'Архивирована'
        RESTORE_PENDING = 'restore_pending', 'Ожидает восстановления'
        STATUS_UPDATE_PENDING = 'status_update_pending', 'Ожидает обновления статуса'
        ERROR = 'error', 'Ошибка'

    claim = models.OneToOneField(WarrantyClaim, on_delete=models.CASCADE, related_name='telegram_thread')
    chat_id = models.CharField(max_length=64, blank=True)
    topic_id = models.CharField(max_length=64, blank=True, help_text='Рекомендуется отдельная тема форума вместо отдельной группы.')
    state = models.CharField(max_length=24, choices=State.choices, default=State.PLANNED)
    title = models.CharField(max_length=255, blank=True)
    archived_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def clean(self):
        if self.state == self.State.ACTIVE and not self.chat_id:
            raise ValidationError({'chat_id': 'Для активного обсуждения нужен Telegram chat ID.'})


class WarrantyTelegramMessage(models.Model):
    thread = models.ForeignKey(WarrantyTelegramThread, on_delete=models.CASCADE, related_name='messages')
    telegram_message_id = models.CharField(max_length=64, blank=True)
    direction = models.CharField(max_length=16, choices=(('inbound', 'Входящее'), ('outbound', 'Исходящее')))
    sender_external_id = models.CharField(max_length=64, blank=True)
    sender_name = models.CharField(max_length=255, blank=True)
    text = models.TextField(blank=True)
    original_text = models.TextField('исходный текст', blank=True)
    payload = models.JSONField(default=dict, blank=True)
    sent_at = models.DateTimeField(default=timezone.now)
    edited_at = models.DateTimeField('изменено в Telegram', null=True, blank=True)

    class Meta:
        ordering = ('sent_at', 'id')
        constraints = [models.UniqueConstraint(fields=('thread', 'telegram_message_id'), condition=~models.Q(telegram_message_id=''), name='unique_warranty_telegram_message')]

    def save(self, *args, **kwargs):
        if not self.original_text:
            self.original_text = self.text
        super().save(*args, **kwargs)
