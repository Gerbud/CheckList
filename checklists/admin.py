from datetime import timedelta

from django.contrib import admin

from checklists.models import (
    AnswerRevision,
    AuditLog,
    ChecklistAnswer,
    ChecklistItem,
    ChecklistNotification,
    ChecklistSection,
    ChecklistTemplate,
    ChecklistTemplateVersion,
    DailyChecklist,
    DailyChecklistItem,
    DailyChecklistStage,
    DailyShiftAssignment,
    EmployeeProfile,
    Store,
    ShiftTemplate,
    StoreChecklistSchedule,
    StoreDayStatus,
    StoreEmployee,
    StoreNotificationSettings,
    StoreTerminalAccount,
    StoreAdHocTask,
    TelegramConversationState,
    TelegramMessageTemplate,
    TelegramInboundJob,
    TelegramOutboundMessage,
    TelegramPendingBinding,
    TelegramStoreBinding,
    TelegramStoreChat,
    TelegramSystemSettings,
    TelegramUpdateLog,
    TelegramUserProfile,
    UserStoreMembership,
)


def editable_field_names(model):
    return tuple(
        field.name
        for field in model._meta.fields
        if field.name != 'id'
    )


class SuperuserMutationAdminMixin:
    """Обычный staff может просматривать, но не обходить сервисный слой."""

    def has_add_permission(self, request):
        return request.user.is_superuser and super().has_add_permission(request)

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser and super().has_change_permission(
            request,
            obj,
        )

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser and super().has_delete_permission(
            request,
            obj,
        )


class ChecklistSectionInline(admin.TabularInline):
    model = ChecklistSection
    extra = 0
    fields = ('name', 'code', 'sort_order', 'created_at')
    readonly_fields = ('created_at',)
    ordering = ('sort_order',)

    def get_readonly_fields(self, request, obj=None):
        if obj and obj.status != ChecklistTemplateVersion.Status.DRAFT:
            return self.fields
        return self.readonly_fields

    def has_add_permission(self, request, obj=None):
        if obj and obj.status != ChecklistTemplateVersion.Status.DRAFT:
            return False
        return super().has_add_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        if obj and obj.status != ChecklistTemplateVersion.Status.DRAFT:
            return False
        return super().has_delete_permission(request, obj)


class ChecklistItemInline(admin.TabularInline):
    model = ChecklistItem
    extra = 0
    fields = (
        'text',
        'description',
        'sort_order',
        'is_active',
        'is_required',
        'comment_required_on_failure',
        'allow_not_applicable',
        'effective_from',
        'effective_until',
        'created_at',
        'updated_at',
    )
    readonly_fields = ('created_at', 'updated_at')
    ordering = ('sort_order',)

    def _is_locked(self, obj):
        return (
            obj
            and obj.version.status != ChecklistTemplateVersion.Status.DRAFT
        )

    def get_readonly_fields(self, request, obj=None):
        if self._is_locked(obj):
            return self.fields
        return self.readonly_fields

    def has_add_permission(self, request, obj=None):
        if self._is_locked(obj):
            return False
        return super().has_add_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        if self._is_locked(obj):
            return False
        return super().has_delete_permission(request, obj)


@admin.register(Store)
class StoreAdmin(SuperuserMutationAdminMixin, admin.ModelAdmin):
    list_display = ('name', 'code', 'timezone', 'is_active', 'updated_at')
    list_filter = ('is_active', 'timezone')
    search_fields = ('name', 'code')
    readonly_fields = ('created_at', 'updated_at')


@admin.register(StoreChecklistSchedule)
class StoreChecklistScheduleAdmin(SuperuserMutationAdminMixin, admin.ModelAdmin):
    list_display = (
        'store',
        'opening_time',
        'morning_deadline',
        'daytime_deadline',
        'closing_deadline',
        'warning_minutes_before',
        'notifications_enabled',
        'is_active',
    )
    list_filter = ('notifications_enabled', 'is_active')
    search_fields = ('store__name', 'store__code')
    autocomplete_fields = ('store',)
    readonly_fields = ('created_at', 'updated_at')


@admin.register(StoreDayStatus)
class StoreDayStatusAdmin(SuperuserMutationAdminMixin, admin.ModelAdmin):
    list_display = ('store', 'date', 'status', 'changed_by', 'updated_at')
    list_filter = ('status', 'store')
    search_fields = ('store__name', 'store__code', 'comment')
    autocomplete_fields = ('store', 'changed_by')
    readonly_fields = ('created_at', 'updated_at')


@admin.register(StoreNotificationSettings)
class StoreNotificationSettingsAdmin(SuperuserMutationAdminMixin, admin.ModelAdmin):
    list_display = (
        'store',
        'telegram_chat_id',
        'warning_enabled',
        'overdue_enabled',
        'completed_late_enabled',
        'is_active',
    )
    list_filter = (
        'warning_enabled',
        'overdue_enabled',
        'completed_late_enabled',
        'is_active',
    )
    search_fields = ('store__name', 'store__code', 'telegram_chat_id')
    autocomplete_fields = ('store',)
    readonly_fields = ('created_at', 'updated_at')


@admin.register(StoreTerminalAccount)
class StoreTerminalAccountAdmin(SuperuserMutationAdminMixin, admin.ModelAdmin):
    list_display = ('store', 'user', 'is_active', 'updated_at')
    list_filter = ('is_active', 'store')
    search_fields = ('store__name', 'store__code', 'user__username')
    autocomplete_fields = ('store', 'user')
    readonly_fields = ('created_at', 'updated_at')


@admin.register(StoreEmployee)
class StoreEmployeeAdmin(SuperuserMutationAdminMixin, admin.ModelAdmin):
    list_display = (
        'display_name',
        'store',
        'position',
        'department',
        'user',
        'personnel_number',
        'is_active',
        'sort_order',
    )
    list_filter = ('store', 'department', 'is_active')
    search_fields = (
        'first_name',
        'last_name',
        'display_name',
        'position',
        'personnel_number',
        'user__username',
    )
    autocomplete_fields = ('store', 'user')
    readonly_fields = ('created_at', 'updated_at')
    actions = ('deactivate_employees',)

    @admin.action(description='Деактивировать выбранных сотрудников')
    def deactivate_employees(self, request, queryset):
        updated = queryset.update(is_active=False)
        self.message_user(request, f'Деактивировано: {updated}.')


@admin.register(ShiftTemplate)
class ShiftTemplateAdmin(SuperuserMutationAdminMixin, admin.ModelAdmin):
    list_display = (
        'name',
        'store',
        'shift_type',
        'shift_start',
        'shift_end',
        'is_active',
    )
    list_filter = ('store', 'shift_type', 'is_active')
    search_fields = ('name', 'store__name', 'store__code')
    autocomplete_fields = ('store',)
    readonly_fields = ('created_at', 'updated_at')

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(DailyShiftAssignment)
class DailyShiftAssignmentAdmin(SuperuserMutationAdminMixin, admin.ModelAdmin):
    list_display = (
        'work_date',
        'store',
        'employee',
        'is_responsible_for_checklist',
        'shift_start',
        'shift_end',
    )
    list_filter = (
        'store',
        'work_date',
        'employee',
        'is_responsible_for_checklist',
    )
    search_fields = ('employee__display_name', 'employee__personnel_number')
    autocomplete_fields = ('store', 'employee', 'created_by')
    readonly_fields = ('created_at', 'updated_at')
    actions = ('copy_to_next_day',)

    @admin.action(description='Копировать назначения на следующий день')
    def copy_to_next_day(self, request, queryset):
        created = 0
        for assignment in queryset.select_related('employee'):
            _, was_created = DailyShiftAssignment.objects.get_or_create(
                store=assignment.store,
                employee=assignment.employee,
                work_date=assignment.work_date + timedelta(days=1),
                defaults={
                    'is_responsible_for_checklist': (
                        assignment.is_responsible_for_checklist
                    ),
                    'shift_start': assignment.shift_start,
                    'shift_end': assignment.shift_end,
                    'comment': assignment.comment,
                    'created_by': request.user,
                },
            )
            created += int(was_created)
        self.message_user(request, f'Создано назначений: {created}.')


@admin.register(EmployeeProfile)
class EmployeeProfileAdmin(SuperuserMutationAdminMixin, admin.ModelAdmin):
    list_display = ('user', 'store', 'role', 'is_active', 'updated_at')
    list_filter = ('store', 'role', 'is_active')
    search_fields = ('user__username', 'user__first_name', 'user__last_name')
    autocomplete_fields = ('user', 'store')
    readonly_fields = ('created_at', 'updated_at')


@admin.register(ChecklistTemplate)
class ChecklistTemplateAdmin(SuperuserMutationAdminMixin, admin.ModelAdmin):
    list_display = ('name', 'store', 'is_active', 'updated_at')
    list_filter = ('store', 'is_active')
    search_fields = ('name', 'store__name', 'store__code')
    autocomplete_fields = ('store',)
    readonly_fields = ('created_at', 'updated_at')


@admin.register(ChecklistTemplateVersion)
class ChecklistTemplateVersionAdmin(SuperuserMutationAdminMixin, admin.ModelAdmin):
    list_display = (
        'template',
        'version_number',
        'status',
        'published_at',
        'created_by',
    )
    list_filter = ('status', 'template__store', 'template')
    search_fields = ('template__name', 'template__store__name')
    autocomplete_fields = ('template', 'created_by')
    readonly_fields = ('status', 'published_at', 'created_at')
    inlines = (ChecklistSectionInline,)

    def get_readonly_fields(self, request, obj=None):
        if obj and obj.status != ChecklistTemplateVersion.Status.DRAFT:
            return editable_field_names(self.model)
        return self.readonly_fields

    def has_delete_permission(self, request, obj=None):
        if obj and obj.status != ChecklistTemplateVersion.Status.DRAFT:
            return False
        return super().has_delete_permission(request, obj)


@admin.register(ChecklistSection)
class ChecklistSectionAdmin(SuperuserMutationAdminMixin, admin.ModelAdmin):
    list_display = ('name', 'code', 'version', 'sort_order')
    list_filter = ('version__template__store', 'version__status')
    search_fields = ('name', 'code', 'version__template__name')
    autocomplete_fields = ('version',)
    readonly_fields = ('created_at',)
    inlines = (ChecklistItemInline,)

    def get_readonly_fields(self, request, obj=None):
        if obj and obj.version.status != ChecklistTemplateVersion.Status.DRAFT:
            return editable_field_names(self.model)
        return self.readonly_fields

    def has_delete_permission(self, request, obj=None):
        if obj and obj.version.status != ChecklistTemplateVersion.Status.DRAFT:
            return False
        return super().has_delete_permission(request, obj)


@admin.register(ChecklistItem)
class ChecklistItemAdmin(SuperuserMutationAdminMixin, admin.ModelAdmin):
    list_display = (
        'short_text',
        'section',
        'sort_order',
        'is_active',
        'is_required',
        'allow_not_applicable',
    )
    list_filter = (
        'section__version__template__store',
        'section__version__status',
        'is_active',
        'is_required',
        'allow_not_applicable',
    )
    search_fields = ('text', 'section__name')
    autocomplete_fields = ('section',)
    readonly_fields = ('created_at', 'updated_at')

    @admin.display(description='Текст пункта')
    def short_text(self, obj):
        return obj.text[:100]

    def get_readonly_fields(self, request, obj=None):
        if (
            obj
            and obj.section.version.status
            != ChecklistTemplateVersion.Status.DRAFT
        ):
            return editable_field_names(self.model)
        return self.readonly_fields

    def has_delete_permission(self, request, obj=None):
        if (
            obj
            and obj.section.version.status
            != ChecklistTemplateVersion.Status.DRAFT
        ):
            return False
        return super().has_delete_permission(request, obj)


@admin.register(DailyChecklist)
class DailyChecklistAdmin(admin.ModelAdmin):
    list_display = (
        'checklist_date',
        'store',
        'employee',
        'status',
        'started_at',
        'completed_at',
    )
    list_filter = ('store', 'checklist_date', 'status', 'employee')
    search_fields = (
        'employee__user__username',
        'employee__user__first_name',
        'employee__user__last_name',
    )
    autocomplete_fields = (
        'store',
        'employee',
        'terminal_account',
        'template_version',
        'reopened_by',
    )
    readonly_fields = (
        'status',
        'started_at',
        'completed_at',
        'reopened_at',
        'reopened_by',
        'created_at',
        'updated_at',
    )

    def get_readonly_fields(self, request, obj=None):
        if obj and obj.status == DailyChecklist.Status.COMPLETED:
            return editable_field_names(self.model)
        return self.readonly_fields

    def has_delete_permission(self, request, obj=None):
        if obj and obj.status == DailyChecklist.Status.COMPLETED:
            return False
        return super().has_delete_permission(request, obj)


class ReadOnlyAdminMixin:
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def has_view_permission(self, request, obj=None):
        return request.user.has_perm(
            f'{self.opts.app_label}.view_{self.opts.model_name}'
        ) or request.user.has_perm(
            f'{self.opts.app_label}.change_{self.opts.model_name}'
        )


@admin.register(AnswerRevision)
class AnswerRevisionAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = (
        'changed_at',
        'store_name',
        'work_date',
        'changed_by_employee',
        'previous_status',
        'new_status',
    )
    list_filter = (
        'answer__daily_item__daily_checklist__store',
        'answer__daily_item__daily_checklist__checklist_date',
        'changed_by_employee',
    )
    search_fields = (
        'change_reason',
        'changed_by_employee__display_name',
        'answer__daily_item__item_text',
    )
    readonly_fields = editable_field_names(AnswerRevision)

    @admin.display(description='Магазин')
    def store_name(self, obj):
        return obj.answer.daily_item.daily_checklist.store

    @admin.display(description='Дата')
    def work_date(self, obj):
        return obj.answer.daily_item.daily_checklist.checklist_date


@admin.register(DailyChecklistItem)
class DailyChecklistItemAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = (
        'short_text',
        'daily_checklist',
        'section_name',
        'display_order',
        'item_sort_order',
    )
    list_filter = (
        'daily_checklist__store',
        'daily_checklist__checklist_date',
        'daily_checklist__status',
    )
    search_fields = ('item_text', 'section_name', 'daily_checklist__employee__user__username')
    readonly_fields = editable_field_names(DailyChecklistItem)

    @admin.display(description='Текст пункта')
    def short_text(self, obj):
        return obj.item_text[:100]


@admin.register(DailyChecklistStage)
class DailyChecklistStageAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = (
        'daily_checklist',
        'section_code',
        'status',
        'opens_at',
        'deadline_at',
        'completed_at',
    )
    list_filter = (
        'section_code',
        'status',
        'daily_checklist__store',
        'daily_checklist__checklist_date',
    )
    search_fields = (
        'daily_checklist__employee__user__username',
        'daily_checklist__store__name',
    )
    readonly_fields = editable_field_names(DailyChecklistStage)


@admin.register(ChecklistAnswer)
class ChecklistAnswerAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ('daily_item', 'status', 'answered_by', 'answered_at')
    list_filter = (
        'status',
        'daily_item__daily_checklist__store',
        'daily_item__daily_checklist__checklist_date',
        'daily_item__daily_checklist__employee',
    )
    search_fields = (
        'daily_item__item_text',
        'daily_item__daily_checklist__employee__user__username',
        'comment',
    )
    readonly_fields = editable_field_names(ChecklistAnswer)


@admin.register(ChecklistNotification)
class ChecklistNotificationAdmin(admin.ModelAdmin):
    list_display = (
        'store_name',
        'employee_name',
        'stage_name',
        'notification_type',
        'scheduled_for',
        'status',
        'attempts',
        'sent_at',
    )
    list_filter = ('notification_type', 'status', 'stage__daily_checklist__store')
    search_fields = (
        'stage__daily_checklist__store__name',
        'stage__daily_checklist__employee__user__username',
    )
    readonly_fields = editable_field_names(ChecklistNotification)
    actions = ('retry_failed_notifications',)

    @admin.display(description='Магазин')
    def store_name(self, obj):
        return obj.stage.daily_checklist.store

    @admin.display(description='Сотрудник')
    def employee_name(self, obj):
        return obj.stage.daily_checklist.employee

    @admin.display(description='Этап')
    def stage_name(self, obj):
        return obj.stage.get_section_code_display()

    @admin.action(description='Повторить неудачные уведомления')
    def retry_failed_notifications(self, request, queryset):
        updated = queryset.filter(
            status=ChecklistNotification.Status.FAILED
        ).update(
            status=ChecklistNotification.Status.PENDING,
            sending_started_at=None,
            last_error=None,
        )
        self.message_user(request, f'Поставлено в очередь: {updated}.')

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(AuditLog)
class AuditLogAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = (
        'created_at',
        'store',
        'actor',
        'action',
        'object_type',
        'object_id',
    )
    list_filter = ('store', 'action', 'object_type', 'created_at')
    search_fields = (
        'actor__username',
        'object_type',
        'object_id',
        'field_name',
    )
    readonly_fields = editable_field_names(AuditLog)
    date_hierarchy = 'created_at'


@admin.register(TelegramSystemSettings)
class TelegramSystemSettingsAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = (
        'masked_token',
        'alternative_api_base_url',
        'use_alternative_gateway',
        'fallback_to_official_api',
        'is_enabled',
        'updated_at',
    )
    exclude = ('bot_token',)
    readonly_fields = tuple(
        field
        for field in editable_field_names(TelegramSystemSettings)
        if field != 'bot_token'
    )


for telegram_model in (
    TelegramOutboundMessage,
    TelegramStoreChat,
    TelegramPendingBinding,
    TelegramStoreBinding,
    TelegramUpdateLog,
    TelegramUserProfile,
    UserStoreMembership,
    TelegramConversationState,
    StoreAdHocTask,
    TelegramMessageTemplate,
    TelegramInboundJob,
):
    admin.site.register(
        telegram_model,
        type(
            f'{telegram_model.__name__}Admin',
            (ReadOnlyAdminMixin, admin.ModelAdmin),
            {'readonly_fields': editable_field_names(telegram_model)},
        ),
    )
