from django.conf import settings
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework import status
from django.contrib.auth import get_user_model

User = get_user_model()


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def create_call_token(request):
    """
    Mint a short-lived LiveKit access token for a 1:1 call.

    Body: { "callee_id": <user_id> }
    Room name: call_<id1>_<id2> (sorted) so both participants derive the
    same room without coordination.
    """
    if not (settings.LIVEKIT_API_KEY and settings.LIVEKIT_API_SECRET):
        return Response(
            {"error": "LiveKit is not configured on the server."},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    callee_id = request.data.get("callee_id")
    try:
        callee_id = int(callee_id)
    except (TypeError, ValueError):
        return Response(
            {"error": "callee_id is required"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    if callee_id == request.user.id:
        return Response(
            {"error": "Cannot start a call with yourself"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        User.objects.get(pk=callee_id, is_active=True)
    except User.DoesNotExist:
        return Response(
            {"error": "Callee not found"},
            status=status.HTTP_404_NOT_FOUND,
        )

    if not getattr(settings, "LIVEKIT_WS_URL", ""):
        return Response(
            {"error": "LiveKit websocket URL is not configured."},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    room_id = "_".join(sorted([str(request.user.id), str(callee_id)]))
    room_name = f"call_{room_id}"

    try:
        from livekit import api

        token = (
            api.AccessToken(
                settings.LIVEKIT_API_KEY,
                settings.LIVEKIT_API_SECRET,
                ttl="1h",
            )
            .with_identity(str(request.user.id))
            .with_name(request.user.username)
            .with_grants(
                api.VideoGrants(
                    room_join=True,
                    room=room_name,
                    can_publish=True,
                    can_subscribe=True,
                )
            )
        )
        jwt = token.to_jwt()
    except Exception:
        return Response(
            {"error": "Failed to issue LiveKit token."},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    return Response(
        {
            "token": jwt,
            "room": room_name,
            "url": settings.LIVEKIT_WS_URL,
        }
    )
