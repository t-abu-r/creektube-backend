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
    key = models.UUIDField(default=uuid4)
    usernames = models.JSONField(default=list)
    pair_key = models.CharField(max_length=300, unique=True, db_index=True)

    def save(self, *args, **kwargs):
        if not self.pair_key:
            self.pair_key = self.make_pair_key(self.usernames)
        super().save(*args, **kwargs)

    @staticmethod
    def make_pair_key(usernames):
        return "\x1f".join(sorted(usernames))  # \x1f = separator, avoids collisions if a username ever contains "|"

    def __str__(self):
        return f"Users: {', '.join(self.usernames)}"

    def __str__(self):
        text = 'Users: '
        for count, username in enumerate(self.usernames):
            text += username + ', '
        return text[:len(text) - 2]

    @classmethod
    def get_by_usernames(cls, usernames):
        return cls.objects.filter(pair_key=cls.make_pair_key(usernames)).first()


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



class GroupChat(models.Model):

    name = models.CharField(max_length=255)
    members = models.ManyToManyField(User, related_name='group_chats')

class GroupChatMessage(models.Model):
    group_chat = models.ForeignKey(GroupChat, on_delete=models.CASCADE, related_name='messages')
    sender = models.ForeignKey(User, on_delete=models.CASCADE)
    text = models.TextField(blank=False)
    timestamp = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.sender.username}: {self.text[:20]}... sent to {self.group_chat.name} at {self.timestamp}"