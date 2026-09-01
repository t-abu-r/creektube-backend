from django.contrib.auth.models import User
from django.db.models import Count, Q
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated

from media.models import Creek

from .models import SenderModel, ReceiverModel, ChatModel
from .serializers import UserSerializer, _user_avatar


def _connected_users_ids(user):
    """User ids with whom ``user`` has a mutual Creek (a "connection")."""
    my_creeked_ids = set(
        Creek.objects.filter(author=user)
        .values_list('account__user_id', flat=True)
    )
    creeked_me_ids = set(
        Creek.objects.filter(account__user=user)
        .values_list('author_id', flat=True)
    )
    return my_creeked_ids & creeked_me_ids


class UserListView(APIView):
    """API endpoint to list connected users for direct chat.

    Two users are "connected" when they have mutually Creeked each
    other — both have created a Creek pointing at the other user's
    MediaProfile.  Only connected users appear in this list.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        connected_ids = _connected_users_ids(request.user)

        users_list = (
            User.objects.filter(id__in=connected_ids, is_active=True)
            .exclude(username="youtube_system")
            .order_by("username")
        )

        serializer = UserSerializer(users_list, many=True)

        return Response(serializer.data)


class ConversationsView(APIView):
    """Recent conversations for the current user's chat sidebar.

    Returns connected users with their last message preview and unread
    status, ordered most-recently-active first.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        connected_ids = _connected_users_ids(request.user)
        users = (
            User.objects.filter(id__in=connected_ids, is_active=True)
            .exclude(username="youtube_system")
        )

        me_sender, _ = SenderModel.objects.get_or_create(user=request.user)
        me_receiver, _ = ReceiverModel.objects.get_or_create(user=request.user)

        # Unread counts keyed by "other user's sender" id
        unread_map = {
            row["sender_id"]: row["count"]
            for row in ChatModel.objects.filter(
                receiver=me_receiver, is_read=False,
            )
            .values("sender_id")
            .annotate(count=Count("id"))
        }

        conversations = []
        for other in users:
            other_sender, _ = SenderModel.objects.get_or_create(user=other)
            other_receiver, _ = ReceiverModel.objects.get_or_create(user=other)

            last = (
                ChatModel.objects.filter(
                    Q(sender=me_sender, receiver=other_receiver)
                    | Q(sender=other_sender, receiver=me_receiver)
                )
                .select_related("sender__user")
                .order_by("-log")
                .first()
            )

            conversations.append({
                "id": other.id,
                "username": other.username,
                "avatar": _user_avatar(other),
                "last_message": last.text if last else None,
                "last_message_at": last.log.isoformat() if last else None,
                "last_sender_username": (
                    last.sender.user.username if last else None
                ),
                "unread": unread_map.get(other_sender.id, 0) > 0,
                "unread_count": unread_map.get(other_sender.id, 0),
            })

        conversations.sort(
            key=lambda c: c["last_message_at"] or "",
            reverse=True,
        )

        return Response(conversations)


class ChatHistoryView(APIView):
    """API endpoint to get chat history between current user and another user."""
    permission_classes = [IsAuthenticated]

    def get(self, request, user2_pk):
        try:
            user1 = request.user
            user2 = User.objects.get(pk=user2_pk)
        except User.DoesNotExist:
            return Response(
                {"error": "User not found"},
                status=status.HTTP_404_NOT_FOUND
            )

        # Setup sender and receiver models (create if missing)
        user1_sender, _ = SenderModel.objects.get_or_create(user=user1)
        user2_sender, _ = SenderModel.objects.get_or_create(user=user2)
        user1_receiver, _ = ReceiverModel.objects.get_or_create(user=user1)
        user2_receiver, _ = ReceiverModel.objects.get_or_create(user=user2)

        # Mark messages received from user2 as read
        ChatModel.objects.filter(
            sender=user2_sender, receiver=user1_receiver, is_read=False,
        ).update(is_read=True)

        # Get chats between both users
        user1_chats = ChatModel.objects.filter(sender=user1_sender, receiver=user2_receiver)
        user2_chats = ChatModel.objects.filter(sender=user2_sender, receiver=user1_receiver)

        # Combine and sort chats
        chats = list(user1_chats) + list(user2_chats)
        if user1.username != user2.username:
            chats.sort(key=lambda x: x.pk)

        # Serialize the chat history
        messages = []
        for chat in chats:
            messages.append({
                "id": chat.id,
                "sender_username": chat.sender.user.username,
                "receiver_username": chat.receiver.user.username,
                "text": chat.text,
                "timestamp": chat.log.isoformat(),
            })

        other_user = User.objects.get(pk=user2_pk)
        avatar_url = _user_avatar(other_user)

        return Response({
            "other_user": {
                "id": user2.id,
                "username": user2.username,
                "avatar": avatar_url,
            },
            "messages": messages
        })
