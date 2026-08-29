from django.contrib.auth.models import User
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated

from .models import SenderModel, ReceiverModel, ChatModel
from .serializers import UserSerializer


class UserListView(APIView):
    """API endpoint to list all users for direct chat."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        # accounts the user is following and the user themselves are excluded from the list
        users_following = User.objects.filter(following__follower=request.user)
        users_all = users_following.union(User.objects.all().exclude(id=request.user.id))

        # Add so youtube_system and deactivated users are not shown in the list and remove emails
        users_list = users_all.filter(is_active=False).order_by('username').exclude(username='youtube_system')
        

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
            if other_user.profile and other_user.profile.avatar:
                avatar_url = other_user.profile.avatar.url
        except Exception:
            avatar_url = None

        return Response({
            "other_user": {
                "id": user2.id,
                "username": user2.username,
                "email": user2.email,
                "avatar": avatar_url,
            },
            "messages": messages
        })
