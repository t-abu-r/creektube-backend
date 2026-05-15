from django.urls import path

from . import consumers
from . import presence

websocket_urlpatterns = [
    path('ws/direct-chat/<int:user2_pk>/', consumers.DirectChatConsumer.as_asgi()),
    path('ws/presence/', presence.PresenceConsumer.as_asgi())
]
