from django.contrib import admin

from warranty.models import WarrantyAttachment, WarrantyBitrixOutbox, WarrantyBitrixSyncState, WarrantyClaim, WarrantyHistoryEvent, WarrantyTelegramMessage, WarrantyTelegramSettings, WarrantyTelegramThread, WarrantyWorkItem

for model in (WarrantyClaim, WarrantyAttachment, WarrantyHistoryEvent, WarrantyWorkItem, WarrantyTelegramSettings, WarrantyTelegramThread, WarrantyTelegramMessage, WarrantyBitrixOutbox, WarrantyBitrixSyncState):
    admin.site.register(model)
