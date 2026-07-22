from django.urls import re_path
from . import consumers

websocket_urlpatterns = [
    re_path(r"^(?:wss?|ws)/snips/feed/?$", consumers.SnipFeedConsumer.as_asgi()),
    re_path(r"^snips/feed/?$", consumers.SnipFeedConsumer.as_asgi()),
]
