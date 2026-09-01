import json
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from django.contrib.auth.models import User
from .models import OnlineUser

GROUP_NAME = "presence"


class PresenceConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.user = self.scope.get("user")
        if not self.user or not self.user.is_authenticated:
            await self.close()
            return

        # Mark user as online
        await self.mark_user_online(self.user)

        await self.accept()

        # Join the presence broadcast group
        await self.channel_layer.group_add(GROUP_NAME, self.channel_name)

        # Send the full current online list to this client
        await self.send_online_users_list()

        # Notify everyone else that this user is now online
        await self.channel_layer.group_send(
            GROUP_NAME,
            {
                "type": "presence.user",
                "user_id": self.user.id,
                "username": self.user.username,
                "status": "online",
            },
        )

    async def disconnect(self, close_code):
        if getattr(self, "user", None) and self.user.is_authenticated:
            # Mark user as offline
            await self.mark_user_offline(self.user)

            await self.channel_layer.group_discard(GROUP_NAME, self.channel_name)

            # Notify everyone else that this user is now offline
            await self.channel_layer.group_send(
                GROUP_NAME,
                {
                    "type": "presence.user",
                    "user_id": self.user.id,
                    "username": self.user.username,
                    "status": "offline",
                },
            )

    async def presence_user(self, event):
        await self.send(text_data=json.dumps({
            "type": "presence",
            "user_id": event["user_id"],
            "username": event["username"],
            "status": event["status"],
        }))

    async def send_online_users_list(self):
        online_records = await self.get_online_users()
        for record in online_records:
            user = record.user
            await self.send(text_data=json.dumps({
                "type": "presence",
                "user_id": user.id,
                "username": user.username,
                "status": "online",
            }))

    @database_sync_to_async
    def mark_user_online(self, user):
        online_user, _ = OnlineUser.objects.get_or_create(
            user=user,
            defaults={"is_online": True},
        )
        if not online_user.is_online:
            online_user.is_online = True
            online_user.save()

    @database_sync_to_async
    def mark_user_offline(self, user):
        try:
            online_user = OnlineUser.objects.get(user=user)
            online_user.is_online = False
            online_user.save()
        except OnlineUser.DoesNotExist:
            pass

    @database_sync_to_async
    def get_online_users(self):
        return list(
            OnlineUser.objects.filter(is_online=True).select_related("user")
        )
