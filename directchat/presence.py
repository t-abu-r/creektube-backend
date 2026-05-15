import json
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from django.contrib.auth.models import User
from .models import OnlineUser


class PresenceConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.user = self.scope["user"]
        if not self.user.is_authenticated:
            await self.close()
            return

        print(f"[DEBUG] Presence connect: {self.user.username} ({self.user.id})")

        # Mark user as online
        await self.mark_user_online(self.user)

        await self.accept()
        
        # Send current online status after accepting
        await self.send_online_users_list()

    async def disconnect(self, close_code):
        if hasattr(self, 'user') and self.user.is_authenticated:
            print(f"[DEBUG] Presence disconnect: {self.user.username} ({self.user.id})")
            
            # Mark user as offline
            await self.mark_user_offline(self.user)

    async def send_online_users_list(self):
        # Send current online users to this client
        online_users = await self.get_online_users()
        for user in online_users:
            await self.send(text_data=json.dumps({
                "type": "presence",
                "user_id": user.id,
                "username": user.username,
                "status": "online"
            }))

    @database_sync_to_async
    def mark_user_online(self, user):
        online_user, created = OnlineUser.objects.get_or_create(
            user=user,
            defaults={"is_online": True}
        )
        print(f"[DEBUG] mark_user_online: {user.username}, created={created}, is_online={online_user.is_online}")
        if not created and not online_user.is_online:
            online_user.is_online = True
            online_user.save()
            print(f"[DEBUG] Updated {user.username} to online")

    @database_sync_to_async
    def mark_user_offline(self, user):
        try:
            online_user = OnlineUser.objects.get(user=user)
            print(f"[DEBUG] mark_user_offline: {user.username}, current status={online_user.is_online}")
            online_user.is_online = False
            online_user.save()
            print(f"[DEBUG] Updated {user.username} to offline")
        except OnlineUser.DoesNotExist:
            print(f"[DEBUG] OnlineUser not found for {user.username}")
            pass

    @database_sync_to_async
    def get_online_users(self):
        return list(OnlineUser.objects.filter(is_online=True))
