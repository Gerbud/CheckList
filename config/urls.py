from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.conf import settings
from django.conf.urls.static import static
from django.urls import include, path

from checklists.portal_views import RoleLoginView


urlpatterns = [
    path('warranty/', include('warranty.urls')),
    path('admin/', admin.site.urls),
    path(
        'login/',
        RoleLoginView.as_view(),
        name='login',
    ),
    path('logout/', auth_views.LogoutView.as_view(), name='logout'),
    path('', include('checklists.urls')),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

handler403 = 'checklists.views.permission_denied'
handler404 = 'checklists.views.page_not_found'
