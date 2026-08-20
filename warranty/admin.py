from django.contrib import admin

from warranty.models import WarrantyAttachment, WarrantyClaim, WarrantyHistoryEvent, WarrantyTelegramMessage, WarrantyTelegramSettings, WarrantyTelegramThread, WarrantyWorkItem

for model in (WarrantyClaim, WarrantyAttachment, WarrantyHistoryEvent, WarrantyWorkItem, WarrantyTelegramSettings, WarrantyTelegramThread, WarrantyTelegramMessage):
    admin.site.register(model)
