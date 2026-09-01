from django.contrib.auth.models import User
from rest_framework import serializers
from .models import ChatModel, SenderModel, ReceiverModel, ChatKeyModel


def _user_avatar(user):
    """Resolve a user's profile picture, preferring the real avatar.

    CreekTube stores user photos on ``accounts.Profile.avatar`` (the same
    source comments/snips avatars come from); fall back to the
    ``media.MediaProfile.banner`` only when no avatar is set.
    """
    try:
        profile = user.profile
        if profile and profile.avatar:
            return profile.avatar.url
    except Exception:
        pass
    try:
        media_profile = user.mediaprofile
        if media_profile and media_profile.banner:
            return media_profile.banner.url
    except Exception:
        pass
    return None


class UserSerializer(serializers.ModelSerializer):
    """Serializer for User model."""
    avatar = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ['id', 'username', 'first_name', 'last_name', 'avatar']

    def get_avatar(self, obj):
        return _user_avatar(obj)


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
