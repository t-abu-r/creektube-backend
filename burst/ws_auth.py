"""
JWT WebSocket authentication middleware.

The frontend connects to WebSocket endpoints with an access token supplied
as a query parameter (``?token=<jwt>``), e.g. ``ws/presence/?token=...`` and
``ws/direct-chat/<id>/?token=...``. Standard Channels auth (cookies/sessions)
cannot see that token, so this middleware decodes it with SimpleJWT and sets
``scope["user"]`` accordingly.
"""

from urllib.parse import parse_qs

from channels.db import database_sync_to_async
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from rest_framework_simplejwt.tokens import AccessToken
from rest_framework_simplejwt.exceptions import TokenError


@database_sync_to_async
def get_user_from_token(token):
    """Resolve a JWT access token to an authenticated user, or AnonymousUser."""
    if not token:
        return AnonymousUser()
    try:
        validated = AccessToken(token)
        user_id = validated.get("user_id")
        if user_id is None:
            return AnonymousUser()
        user = get_user_model().objects.get(id=user_id)
        request_user = get_user_model()()
        request_user.id = user.id
        request_user.username = user.username
        request_user.is_active = user.is_active
        request_user.password = user.password
        request_user.email = user.email
        request_user.is_staff = user.is_staff
        request_user.is_superuser = user.is_superuser
        return request_user
    except (TokenError, KeyError, ValueError, TypeError, get_user_model().DoesNotExist):
        return AnonymousUser()


class JWTAuthMiddleware:
    """Populate ``scope['user']`` from a JWT access token in the query string."""

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        query_string = scope.get("query_string", b"").decode()
        params = parse_qs(query_string)
        token = (params.get("token") or [None])[0]
        scope["user"] = await get_user_from_token(token)
        return await self.inner(scope, receive, send)
