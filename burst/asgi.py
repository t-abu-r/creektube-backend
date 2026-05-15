"""
ASGI config for burst project.

It exposes the ASGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/5.1/howto/deployment/asgi/
"""

import os
import sys

# Add the project directory to the path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault('DJANGO_SETTINGS_MODULE', os.environ.get('DJANGO_SETTINGS_MODULE', 'burst.settings.prod'))

try:
    from django.core.asgi import get_asgi_application

    # Initialize Django ASGI application early to ensure the AppRegistry
    # is populated before importing code that may import ORM models.
    django_asgi_app = get_asgi_application()

    # Import channels stuff after Django is initialized
    from channels.routing import ProtocolTypeRouter, URLRouter
    from chat.middleware import JWTAuthMiddleware
    from chat.routing import websocket_urlpatterns as chat_websocket_urlpatterns
    from directchat.routing import websocket_urlpatterns as directchat_websocket_urlpatterns

    # Combine websocket patterns
    combined_websocket_urlpatterns = chat_websocket_urlpatterns + directchat_websocket_urlpatterns

    application = ProtocolTypeRouter({
        "http": django_asgi_app,
        "websocket": JWTAuthMiddleware(
            URLRouter(combined_websocket_urlpatterns)
        ),
    })
except Exception as e:
    import traceback
    print(f"Error loading ASGI application: {e}")
    print(traceback.format_exc())
    raise
