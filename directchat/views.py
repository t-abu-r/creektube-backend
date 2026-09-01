from django.contrib.auth.models import User
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated

from media.models import Creek

from .models import SenderModel, ReceiverModel, ChatModel
from .serializers import UserSerializer


class UserListView(APIView):
    """API endpoint to list connected users for direct chat.

    Two users are "connected" when they have mutually Creeked each
    other — both have created a Creek pointing at the other user's
    MediaProfile.  Only connected users appear in this list.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        # Users I have Creeked
        my_creeked_ids = set(
            Creek.objects.filter(author=request.user)
            .values_list('account__user_id', flat=True)
        )

        # Users who have Creeked me
        creeked_me_ids = set(
            Creek.objects.filter(account__user=request.user)
            .values_list('author_id', flat=True)
        )

        # Mutual = connected
        connected_ids = my_creeked_ids & creeked_me_ids

        users_list = (
            User.objects.filter(id__in=connected_ids, is_active=True)
            .exclude(username="youtube_system")
            .order_by("username")
        )

        serializer = UserSerializer(users_list, many=True)

        return Response(serializer.data)


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
        try:
            avatar_url = None
            if other_user.mediaprofile and other_user.mediaprofile.banner:
                avatar_url = other_user.mediaprofile.banner.url
        except Exception:
            avatar_url = None

        return Response({
            "other_user": {
                "id": user2.id,
                "username": user2.username,
                "avatar": avatar_url,
            },
            "messages": messages
        })
