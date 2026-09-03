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

        self.visible = True

        # Load stored status (persists across reconnects).
        status = await self.get_status(self.user)

        if status == OnlineUser.STATUS_INVISIBLE:
            self.visible = False

        # Mark user as online (is_online) regardless of visibility.
        await self.mark_user_status(self.user, status, online=True)

        await self.accept()

        # Join the presence broadcast group
        await self.channel_layer.group_add(GROUP_NAME, self.channel_name)

        # Send the full current online list to this client
        await self.send_online_users_list()

        # Notify everyone else that this user is now visible/online.
        if self.visible:
            await self.broadcast_status(self.user, status)

    async def disconnect(self, close_code):
        if getattr(self, "user", None) and self.user.is_authenticated:
            # If we were visible, notify others that we went offline.
            if getattr(self, "visible", True):
                status = await self.get_status(self.user)
                await self.channel_layer.group_discard(GROUP_NAME, self.channel_name)
                await self.broadcast_status(self.user, "offline")

            # Mark offline in DB.
            await self.mark_user_status(self.user, None, online=False)

    async def receive(self, text_data):
        try:
            data = json.loads(text_data)
        except json.JSONDecodeError:
            return

        if data.get("type") != "set_status":
            return

        status = data.get("status")
        if status not in {
            OnlineUser.STATUS_ONLINE,
            OnlineUser.STATUS_INVISIBLE,
            OnlineUser.STATUS_DND,
        }:
            return

        await self.mark_user_status(self.user, status, online=True)
        self.visible = status != OnlineUser.STATUS_INVISIBLE

        # When going invisible, tell others this user is offline.
        if status == OnlineUser.STATUS_INVISIBLE:
            await self.broadcast_status(self.user, "offline")
        else:
            await self.broadcast_status(self.user, status)

        # echo back to self so the client can confirm its own status
        await self.send(text_data=json.dumps({
            "type": "presence",
            "user_id": self.user.id,
            "username": self.user.username,
            "status": status,
        }))

    async def presence_user(self, event):
        await self.send(text_data=json.dumps({
            "type": "presence",
            "user_id": event["user_id"],
            "username": event["username"],
            "status": event["status"],
        }))

    async def broadcast_status(self, user, status):
        await self.channel_layer.group_send(
            GROUP_NAME,
            {
                "type": "presence.user",
                "user_id": user.id,
                "username": user.username,
                "status": status,
            },
        )

    async def send_online_users_list(self):
        online_records = await self.get_online_users()
        for record in online_records:
            # Skip invisible users entirely.
            if record.status == OnlineUser.STATUS_INVISIBLE:
                continue
            user = record.user
            await self.send(text_data=json.dumps({
                "type": "presence",
                "user_id": user.id,
                "username": user.username,
                "status": record.status,
            }))

    @database_sync_to_async
    def get_status(self, user):
        try:
            return OnlineUser.objects.get(user=user).status
        except OnlineUser.DoesNotExist:
            return OnlineUser.STATUS_ONLINE

    @database_sync_to_async
    def mark_user_status(self, user, status, online):
        online_user, _ = OnlineUser.objects.get_or_create(
            user=user,
            defaults={
                "is_online": True,
                "status": status or OnlineUser.STATUS_ONLINE,
            },
        )
        online_user.is_online = online
        if status is not None:
            online_user.status = status
        online_user.save()

    @database_sync_to_async
    def get_online_users(self):
        return list(
            OnlineUser.objects.filter(is_online=True).select_related("user")
        )
