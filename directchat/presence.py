import asyncio
import json
import logging
import uuid
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from django.contrib.auth.models import User
from django.conf import settings
from .models import OnlineUser

logger = logging.getLogger("directchat.presence")

GROUP_NAME = "presence"
PRESENCE_TTL = 30  # seconds — auto-expires to "offline" without clean disconnect
REFRESH_EVERY = 15  # seconds between TTL refreshes (half of TTL)
PING_INTERVAL = 25
PONG_TIMEOUT = 10


def _redis():
    """Return the channel layer's underlying Redis connection, or None."""
    try:
        from channels_redis.core import RedisChannelLayer
        cl = None
        try:
            from channels.layers import get_channel_layer
            cl = get_channel_layer()
        except Exception:
            pass
        if cl and isinstance(cl, RedisChannelLayer) and cl.hosts:
            return cl.hosts[0][0] if cl.hosts[0] else None
    except Exception:
        pass
    return None


class PresenceConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.user = self.scope.get("user")
        if not self.user or not self.user.is_authenticated:
            await self.close()
            return

        self.visible = True
        self._pong_pending = False
        self._ping_task = None
        self._refresh_task = None

        status = await self.get_status(self.user)
        if status == OnlineUser.STATUS_INVISIBLE:
            self.visible = False

        await self.mark_user_status(self.user, status, online=True)
        await self._set_presence_redis(status)

        await self.accept()
        await self.channel_layer.group_add(GROUP_NAME, self.channel_name)
        await self.send_online_users_list()

        if self.visible:
            await self.broadcast_status(self.user, status)

        try:
            self._ping_task = asyncio.ensure_future(self._ping_loop())
            self._refresh_task = asyncio.ensure_future(self._refresh_loop())
        except Exception:
            logger.exception("Failed to start background tasks for user=%s",
                             self.user.id)

        logger.info("Presence connect user=%s status=%s", self.user.id, status)

    async def disconnect(self, close_code):
        if self._ping_task:
            self._ping_task.cancel()
            try:
                await self._ping_task
            except asyncio.CancelledError:
                pass
        if self._refresh_task:
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except asyncio.CancelledError:
                pass

        if getattr(self, "user", None) and self.user.is_authenticated:
            try:
                if getattr(self, "visible", True):
                    await self.channel_layer.group_discard(
                        GROUP_NAME, self.channel_name
                    )
                    await self.broadcast_status(self.user, "offline")
                await self.mark_user_status(self.user, None, online=False)
                await self._expire_presence_redis()
            except Exception:
                logger.exception("Disconnect cleanup failed user=%s",
                                 self.user.id)

        logger.info("Presence disconnect user=%s code=%s",
                     getattr(self, 'user', None) and self.user.id,
                     close_code)

    async def _ping_loop(self):
        try:
            while True:
                await asyncio.sleep(PING_INTERVAL)
                self._pong_pending = True
                try:
                    await self.send(text_data=json.dumps({
                        "type": "ping",
                        "ts": uuid.uuid4().hex,
                    }))
                except Exception:
                    await self.close(code=4000)
                    return
                await asyncio.sleep(PONG_TIMEOUT)
                if self._pong_pending:
                    logger.warning("Pong timeout user=%s", self.user.id)
                    await self.close(code=4000)
                    return
        except asyncio.CancelledError:
            return

    async def _refresh_loop(self):
        try:
            while True:
                await asyncio.sleep(REFRESH_EVERY)
                if getattr(self, "visible", True):
                    await self._set_presence_redis(
                        await self.get_status(self.user)
                    )
        except asyncio.CancelledError:
            return

    async def receive(self, text_data):
        try:
            data = json.loads(text_data)
        except json.JSONDecodeError:
            return

        if data.get("type") == "pong":
            self._pong_pending = False
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
        await self._set_presence_redis(status)
        self.visible = status != OnlineUser.STATUS_INVISIBLE

        if status == OnlineUser.STATUS_INVISIBLE:
            await self.broadcast_status(self.user, "offline")
        else:
            await self.broadcast_status(self.user, status)

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
            if record.status == OnlineUser.STATUS_INVISIBLE:
                continue
            user = record.user
            await self.send(text_data=json.dumps({
                "type": "presence",
                "user_id": user.id,
                "username": user.username,
                "status": record.status,
            }))

    async def _set_presence_redis(self, status):
        try:
            r = _redis()
            if r is None:
                return
            await r.set(
                f"presence:{self.user.id}",
                json.dumps({
                    "user_id": self.user.id,
                    "username": self.user.username,
                    "status": status,
                }),
                ex=PRESENCE_TTL,
            )
        except Exception:
            logger.debug("Redis presence set failed user=%s", self.user.id)

    async def _expire_presence_redis(self):
        try:
            r = _redis()
            if r is None:
                return
            await r.delete(f"presence:{self.user.id}")
        except Exception:
            logger.debug("Redis presence delete failed user=%s", self.user.id)

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
