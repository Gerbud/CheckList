from django.urls import path

from warranty import views

app_name = 'warranty'

urlpatterns = [
    path('settings/bitrix/', views.bitrix_settings, name='bitrix_settings'),
    path('', views.claim_list, name='claim_list'),
    path('<int:claim_id>/', views.claim_detail, name='claim_detail'),
    path('<int:claim_id>/update/', views.claim_update, name='claim_update'),
    path('<int:claim_id>/work-items/add/', views.work_item_add, name='work_item_add'),
]
