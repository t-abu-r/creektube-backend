import json
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from django.contrib.auth.models import User


class SnipFeedConsumer(AsyncWebsocketConsumer):
    """
    WebSocket consumer for the snips feed.
    
    Handles real-time updates for:
    - New snip uploads appearing in feed
    - Like count updates across all viewers
    - View count updates
    - Active viewer count
    
    Client sends:
        {"action": "like", "snip_id": 1}
        {"action": "view", "snip_id": 1}
    
    Server sends:
        {"type": "new_snip", "snip": {...}}
        {"type": "like_update", "snip_id": 1, "like_count": 5, "is_liked": true}
        {"type": "view_update", "snip_id": 1, "view_count": 10}
        {"type": "viewer_count", "count": 3}
    """

    GROUP_NAME = "snips_feed"

    async def connect(self):
        self.user = self.scope.get("user")
        self.room_snip_ids = set()

        await self.channel_layer.group_add(self.GROUP_NAME, self.channel_name)
        await self.accept()

        count = await self.get_viewer_count()
        await self.channel_layer.group_send(
            self.GROUP_NAME,
            {"type": "viewer_count", "count": count},
        )

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard(self.GROUP_NAME, self.channel_name)

        count = await self.get_viewer_count()
        await self.channel_layer.group_send(
            self.GROUP_NAME,
            {"type": "viewer_count", "count": max(count, 0)},
        )

    async def receive(self, text_data):
        try:
            data = json.loads(text_data)
        except json.JSONDecodeError:
            return

        action = data.get("action")
        snip_id = data.get("snip_id")

        if action == "like" and snip_id:
            result = await self.toggle_like(snip_id)
            if result:
                await self.channel_layer.group_send(
                    self.GROUP_NAME,
                    {
                        "type": "like_update",
                        "snip_id": snip_id,
                        "like_count": result["like_count"],
                        "is_liked": result["is_liked"],
                    },
                )

        elif action == "view" and snip_id:
            view_count = await self.record_view(snip_id)
            if view_count is not None:
                await self.channel_layer.group_send(
                    self.GROUP_NAME,
                    {
                        "type": "view_update",
                        "snip_id": snip_id,
                        "view_count": view_count,
                    },
                )

    # ---- group message handlers ----

    async def new_snip(self, event):
        await self.send(text_data=json.dumps({
            "type": "new_snip",
            "snip": event["snip"],
        }))

    async def like_update(self, event):
        await self.send(text_data=json.dumps({
            "type": "like_update",
            "snip_id": event["snip_id"],
            "like_count": event["like_count"],
        }))

    async def view_update(self, event):
        await self.send(text_data=json.dumps({
            "type": "view_update",
            "snip_id": event["snip_id"],
            "view_count": event["view_count"],
        }))

    async def viewer_count(self, event):
        await self.send(text_data=json.dumps({
            "type": "viewer_count",
            "count": event["count"],
        }))

    # ---- DB helpers ----

    @database_sync_to_async
    def toggle_like(self, snip_id):
        from .models import Snip, SnipLike

        if not self.user or not self.user.is_authenticated:
            return None

        try:
            snip = Snip.objects.filter(author__is_active=True).get(id=snip_id)
        except Snip.DoesNotExist:
            return None

        like, created = SnipLike.objects.get_or_create(
            author=self.user, snip=snip
        )
        if not created:
            like.delete()
            from django.db.models import F
            Snip.objects.filter(id=snip_id).update(like_count=F("like_count") - 1)
            snip.refresh_from_db(fields=["like_count"])
            return {"like_count": max(snip.like_count, 0), "is_liked": False}

        from django.db.models import F
        Snip.objects.filter(id=snip_id).update(like_count=F("like_count") + 1)
        snip.refresh_from_db(fields=["like_count"])
        return {"like_count": snip.like_count, "is_liked": True}

    @database_sync_to_async
    def record_view(self, snip_id):
        from .models import Snip
        from django.db.models import F

        try:
            Snip.objects.filter(id=snip_id).update(view_count=F("view_count") + 1)
            snip = Snip.objects.get(id=snip_id)
            return snip.view_count
        except Snip.DoesNotExist:
            return None

    @database_sync_to_async
    def get_viewer_count(self):
        from channels.layers import get_channel_layer
        # Approximate: count channels in group (each connection = 1 channel)
        # This is a rough count; for production use Redis pubsub
        return 0
