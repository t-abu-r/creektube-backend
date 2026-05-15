from django.contrib.auth.models import User
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated

from .models import SenderModel, ReceiverModel, ChatModel
from .serializers import UserSerializer, ChatModelSerializer


class UserListView(APIView):
    """API endpoint to list all users for direct chat."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        users = User.objects.all().exclude(id=request.user.id)
        serializer = UserSerializer(users, many=True)
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

        return Response({
            "other_user": {
                "id": user2.id,
                "username": user2.username,
            },
            "messages": messages
        })


# Keep template views for backwards compatibility (optional)
from django.views.generic import View
from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import render


class HomeView(LoginRequiredMixin, View):
    http_method_names = ['get']

    def get(self, request, *args, **kwargs):
        context = {'users': User.objects.all()}
        return render(request, 'directchat/home.html', context)


class ChatView(LoginRequiredMixin, View):
    http_method_names = ['get']

    def get(self, request, user2_pk, *args, **kwargs):
        user1 = User.objects.get(pk=request.user.pk)
        user2 = User.objects.get(pk=user2_pk)
        user1_sender = SenderModel.objects.get(pk=user1.sendermodel.pk)
        user2_sender = SenderModel.objects.get(pk=user2.sendermodel.pk)
        user1_receiver = ReceiverModel.objects.get(pk=user1.receivermodel.pk)
        user2_receiver = ReceiverModel.objects.get(pk=user2.receivermodel.pk)
        user1_chats = ChatModel.objects.filter(sender=user1_sender, receiver=user2_receiver)
        user2_chats = ChatModel.objects.filter(sender=user2_sender, receiver=user1_receiver)
        chats = [chat1 for chat1 in user1_chats]
        if not user1.get_username() == user2.get_username():
            chats.extend([chat2 for chat2 in user2_chats])
            chats.sort(key=lambda x: x.pk)
        context = {'chats': chats, 'user2': user2}
        return render(request, 'directchat/chat.html', context)
