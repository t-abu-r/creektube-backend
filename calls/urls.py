from django.urls import path
from .views import create_call_token

urlpatterns = [
    path("calls/token/", create_call_token, name="create_call_token"),
]
