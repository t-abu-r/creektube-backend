"""
URL configuration for burst project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.1/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.conf.urls.static import static#
from django.contrib import admin
from django.urls import path, include, re_path
from rest_framework_simplejwt.views import TokenRefreshView
from django.http import FileResponse, Http404
from django.conf import settings
import os

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/', include('accounts.urls')),
    path("api/token/refresh/", TokenRefreshView.as_view(), name="token_refresh"),
    path("media/", include('media.urls')),
    # path("direct-chat/", include('directchat.urls')),

]

def serve_media(request, path):
    file_path = os.path.join(settings.MEDIA_ROOT, path)
    if not os.path.exists(file_path):
        raise Http404

    response = FileResponse(open(file_path, 'rb'))
    response['Accept-Ranges'] = 'bytes'
    response['Content-Length'] = os.path.getsize(file_path)
    return response

if settings.DEBUG:
    urlpatterns += [
        re_path(r'^uploads/(?P<path>.*)$', serve_media),
    ]
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
