import asyncio
import json
import logging
import uuid
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from django.contrib.auth.models import User
from django.db import models

from .models import SenderModel, ReceiverModel, ChatModel, ChatKeyModel

logger = logging.getLogger("directchat.consumer")


class DirectChatConsumer(AsyncWebsocketConsumer):
    PING_INTERVAL = 25  # seconds between pings
    PONG_TIMEOUT = 10   # seconds to wait for pong before counting miss

    async def connect(self):
        user1 = self.scope.get("user")
        if not user1 or not user1.is_authenticated:
            await self.close(code=403)
            return

        user2_pk = self.scope['url_route']['kwargs']['user2_pk']
        user2 = await self.get_usermodel(pk=user2_pk)
        if not user2:
            await self.close(code=404)
            return

        self.user1_username = user1.get_username()
        self.user2_username = user2.get_username()
        self.user1_sender = await self.get_or_create_sendermodel(user=user1)
        self.user2_receiver = await self.get_or_create_receivermodel(user=user2)

        usernames = [self.user1_username]
        if not self.user1_username == self.user2_username:
            usernames.append(self.user2_username)
        usernames.sort()

        chatkey = await database_sync_to_async(ChatKeyModel.get_by_usernames)(
            usernames
        )
        if not chatkey:
            chatkey = await database_sync_to_async(ChatKeyModel.objects.create)(
                usernames=usernames
            )
        self.room_name = f'direct_chat_room_{chatkey.key}'

        await self.channel_layer.group_add(
            self.room_name,
            self.channel_name
        )

        await self.accept()

        self._pong_pending = False
        self._ping_task = None
        try:
            self._ping_task = asyncio.ensure_future(self._ping_loop())
        except Exception:
            logger.exception("Failed to start ping loop for user=%s room=%s",
                             user1.id, self.room_name)

        logger.info("WS connect user=%s room=%s", user1.id, self.room_name)

    async def disconnect(self, close_code):
        if self._ping_task:
            self._ping_task.cancel()
            try:
                await self._ping_task
            except asyncio.CancelledError:
                pass

        if hasattr(self, 'room_name'):
            try:
                await self.channel_layer.group_discard(
                    self.room_name,
                    self.channel_name
                )
            except Exception:
                logger.exception("Failed to leave group room=%s user=%s",
                                 self.room_name,
                                 getattr(self, 'user1_username', '?'))

        logger.info("WS disconnect user=%s room=%s code=%s",
                     getattr(self, 'user1_username', '?'),
                     getattr(self, 'room_name', '?'),
                     close_code)

    async def _ping_loop(self):
        try:
            while True:
                await asyncio.sleep(self.PING_INTERVAL)
                self._pong_pending = True
                try:
                    await self.send(text_data=json.dumps({
                        "type": "ping",
                        "ts": uuid.uuid4().hex,
                    }))
                except Exception:
                    logger.debug("Ping send failed for room=%s, "
                                 "closing", getattr(self, 'room_name', '?'))
                    await self.close(code=4000)
                    return
                await asyncio.sleep(self.PONG_TIMEOUT)
                if self._pong_pending:
                    logger.warning("Pong timeout for user=%s room=%s, "
                                   "closing", self.user1_username,
                                   self.room_name)
                    await self.close(code=4000)
                    return
        except asyncio.CancelledError:
            return

    async def receive(self, text_data):
        data = json.loads(text_data)

        if data.get("type") == "pong":
            self._pong_pending = False
            return

        if data.get("type") == "fetch_missed":
            last_id = data.get("last_message_id")
            if last_id is not None:
                missed = await self._get_messages_after(last_id)
                for msg in missed:
                    msg_id = uuid.uuid4().hex
                    await self.send(text_data=json.dumps({
                        "type": "missed_message",
                        "message_id": msg_id,
                        "chatmodel": msg,
                    }))
            return

        message = data.get("message", "")
        if not message:
            return

        message_obj = {
            'sender': self.user1_sender,
            'receiver': self.user2_receiver,
            'text': message
        }

        chatmodel = await self.create_chatmodel(**message_obj)

        log = f'{chatmodel.log.date()} - {str(chatmodel.log.time())[:8]}'
        chatmodel_data = {
            'sender_username': self.user1_username,
            'receiver_username': self.user2_username,
            'text': chatmodel.text,
            'log': log,
            'id': chatmodel.id,
            'timestamp': chatmodel.log.isoformat(),
        }

        msg_id = uuid.uuid4().hex
        await self.channel_layer.group_send(
            self.room_name,
            {
                'type': 'send.message',
                'chatmodel': chatmodel_data,
                'message_id': msg_id,
            }
        )

    async def send_message(self, event):
        message = json.dumps({
            'chatmodel': event['chatmodel'],
            'message_id': event.get('message_id', uuid.uuid4().hex),
        })
        await self.send(text_data=message)

    @database_sync_to_async
    def _get_messages_after(self, after_id):
        try:
            after_id = int(after_id)
        except (TypeError, ValueError):
            return []
        me_sender_id = self.user1_sender.id
        me_receiver_id = self.user2_receiver.id
        other_sender_id = self.user2_receiver.id
        other_receiver_id = self.user1_sender.id
        chats = list(
            ChatModel.objects.filter(id__gt=after_id).filter(
                models.Q(sender_id=me_sender_id, receiver_id=me_receiver_id)
                | models.Q(sender_id=other_sender_id, receiver_id=other_receiver_id)
            ).select_related("sender__user", "receiver__user").order_by("id")
        )
        return [
            {
                'sender_username': c.sender.user.username,
                'receiver_username': c.receiver.user.username,
                'text': c.text,
                'log': f'{c.log.date()} - {str(c.log.time())[:8]}',
                'id': c.id,
                'timestamp': c.log.isoformat(),
            }
            for c in chats
        ]

    @database_sync_to_async
    def get_usermodel(self, **kwargs):
        try:
            return User.objects.get(**kwargs)
        except User.DoesNotExist:
            return None

    @database_sync_to_async
    def get_or_create_sendermodel(self, **kwargs):
        sender, _ = SenderModel.objects.get_or_create(**kwargs)
        return sender

    @database_sync_to_async
    def get_or_create_receivermodel(self, **kwargs):
        receiver, _ = ReceiverModel.objects.get_or_create(**kwargs)
        return receiver

    @database_sync_to_async
    def get_latest_chatmodel(self, **kwargs):
        return ChatModel.objects.filter(**kwargs).last()

    @database_sync_to_async
    def create_chatmodel(self, **kwargs):
        return ChatModel.objects.create(**kwargs)
