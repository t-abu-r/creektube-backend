from django.contrib.auth.models import User
from rest_framework import serializers
from .models import ChatModel, SenderModel, ReceiverModel, ChatKeyModel


class UserSerializer(serializers.ModelSerializer):
    """Serializer for User model."""
    avatar = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ['id', 'username', 'email', 'first_name', 'last_name', 'avatar']

    def get_avatar(self, obj):
        try:
            profile = obj.profile
        except Exception:
            return None
        if profile and profile.avatar:
            try:
                return profile.avatar.url
            except Exception:
                return None
        return None


class ChatModelSerializer(serializers.ModelSerializer):
    """Serializer for chat messages."""
    sender_username = serializers.CharField(source='sender.user.username', read_only=True)
    receiver_username = serializers.CharField(source='receiver.user.username', read_only=True)
    timestamp = serializers.DateTimeField(source='log', read_only=True)

    class Meta:
        model = ChatModel
        fields = ['id', 'sender', 'sender_username', 'receiver', 'receiver_username', 'text', 'timestamp']
        read_only_fields = ['sender', 'receiver']


class ChatHistorySerializer(serializers.Serializer):
    """Serializer for chat history between two users."""
    other_user = UserSerializer(read_only=True)
    messages = ChatModelSerializer(many=True, read_only=True)
