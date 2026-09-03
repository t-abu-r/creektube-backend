from uuid import uuid4
from secrets import token_hex

from django.db import models
from django.contrib.auth.models import User
from django.utils.timezone import now


class SenderModel(models.Model):
    """Model for the sender of chat"""
    user = models.OneToOneField(User, on_delete=models.CASCADE)

    def __str__(self):
        return self.user.get_username()


class ReceiverModel(models.Model):
    """Model for the receiver of chat"""
    user = models.OneToOneField(User, on_delete=models.CASCADE)

    def __str__(self):
        return self.user.get_username()


class ChatModel(models.Model):
    """Model for chat"""
    sender = models.ForeignKey(SenderModel, on_delete=models.CASCADE)
    receiver = models.ForeignKey(ReceiverModel, on_delete=models.CASCADE)
    text = models.TextField()
    log = models.DateTimeField(default=now)
    is_read = models.BooleanField(default=False)

    def __str__(self):
        return f'{self.sender.user.get_username()} chats {self.receiver.user.get_username()}'


class ChatKeyModel(models.Model):
    """Contains the chat key that will be used for group name on channel layer"""
    key = models.UUIDField(default=uuid4)
    usernames = models.JSONField(default=list)

    def __str__(self):
        text = 'Users: '
        for count, username in enumerate(self.usernames):
            text += username + ', '
        return text[:len(text) - 2]

    @classmethod
    def get_by_usernames(cls, usernames):
        chatkeys = cls.objects.all()
        usernames.sort()
        for chatkey in chatkeys:
            chatkey.usernames.sort()
            if chatkey.usernames == usernames:
                return chatkey
        return None


class OnlineUser(models.Model):
    """Track online users with last seen timestamp + presence status"""
    STATUS_ONLINE = "online"
    STATUS_INVISIBLE = "invisible"
    STATUS_DND = "dnd"

    STATUS_CHOICES = [
        (STATUS_ONLINE, "Online"),
        (STATUS_INVISIBLE, "Invisible"),
        (STATUS_DND, "Do not disturb"),
    ]

    user = models.OneToOneField(User, on_delete=models.CASCADE)
    last_seen = models.DateTimeField(auto_now=True)
    is_online = models.BooleanField(default=False)
    status = models.CharField(
        max_length=16,
        choices=STATUS_CHOICES,
        default=STATUS_ONLINE,
    )

    def __str__(self):
        return f"{self.user.username} - {self.status}"
