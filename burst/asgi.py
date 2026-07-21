import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault('DJANGO_SETTINGS_MODULE', os.environ.get('DJANGO_SETTINGS_MODULE', 'burst.settings.prod'))

try:
    from django.core.asgi import get_asgi_application

    django_asgi_app = get_asgi_application()

    from channels.routing import ProtocolTypeRouter, URLRouter
    from media.routing import websocket_urlpatterns
    from channels.auth import AuthMiddlewareStack

    application = ProtocolTypeRouter({
        "http": django_asgi_app,
        "websocket": AuthMiddlewareStack(
            URLRouter(websocket_urlpatterns)
        ),
    })
except Exception as e:
    import traceback
    print(f"Error loading ASGI application: {e}")
    print(traceback.format_exc())
    raise
