from django.urls import path

from warranty import views
from warranty.customer_bot import customer_bot_webhook
from warranty.service_views import service_home

app_name = 'warranty'

urlpatterns = [
    path('service/', service_home, name='service_home'),
    path('customer-bot/webhook/', customer_bot_webhook, name='customer_bot_webhook'),
    path('settings/bitrix/', views.bitrix_settings, name='bitrix_settings'),
    path('', views.claim_list, name='claim_list'),
    path('<int:claim_id>/', views.claim_detail, name='claim_detail'),
    path('<int:claim_id>/update/', views.claim_update, name='claim_update'),
    path('<int:claim_id>/work-items/add/', views.work_item_add, name='work_item_add'),
]
