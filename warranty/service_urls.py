from django.urls import path

from warranty.service_views import service_home


urlpatterns = [path('', service_home, name='warranty_service')]
