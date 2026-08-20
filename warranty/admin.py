from django.contrib import admin

from warranty.models import WarrantyAttachment, WarrantyBitrixOutbox, WarrantyBitrixSyncState, WarrantyClaim, WarrantyHistoryEvent, WarrantyTelegramMessage, WarrantyTelegramSettings, WarrantyTelegramStatusButton, WarrantyTelegramThread, WarrantyWorkItem

for model in (WarrantyClaim, WarrantyAttachment, WarrantyHistoryEvent, WarrantyWorkItem, WarrantyTelegramSettings, WarrantyTelegramThread, WarrantyTelegramMessage, WarrantyBitrixOutbox, WarrantyBitrixSyncState):
    admin.site.register(model)


@admin.register(WarrantyTelegramStatusButton)
class WarrantyTelegramStatusButtonAdmin(admin.ModelAdmin):
    list_display = ('label', 'source_status', 'target_status', 'position', 'is_enabled')
    list_editable = ('position', 'is_enabled')
    list_filter = ('source_status', 'target_status', 'is_enabled')
    search_fields = ('label',)
    ordering = ('source_status', 'position', 'id')
