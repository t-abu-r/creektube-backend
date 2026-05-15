"""
Auto-login test for directchat.
Run with: python manage.py test directchat.test_auto_login
"""
import asyncio
import json
from channels.testing import WebsocketCommunicator
from django.test import TestCase, TransactionTestCase
from django.contrib.auth.models import User
from django.urls import reverse
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from burst.asgi import application
from directchat.models import SenderModel, ReceiverModel, ChatKeyModel


class AutoLoginAPITests(TestCase):
    """Test API endpoints with auto-login as admin/password."""

    def setUp(self):
        """Create admin user and authenticate automatically."""
        # Create admin user
        self.user = User.objects.create_user(
            username='admin',
            email='admin@example.com',
            password='password',
            is_staff=True
        )
        # Ensure sender/receiver models exist (created via signals, but use get_or_create to be safe)
        SenderModel.objects.get_or_create(user=self.user)
        ReceiverModel.objects.get_or_create(user=self.user)
        ChatKeyModel.objects.get_or_create(usernames=[self.user.username])

        # Authenticate automatically
        self.client = APIClient()
        self.token = str(AccessToken.for_user(self.user))
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.token}')

    def test_auto_login_works(self):
        """Test that auto-login is working."""
        response = self.client.get('/direct-chat/api/users/')
        self.assertEqual(response.status_code, 200)

    def test_list_users_empty(self):
        """Test listing users when no other users exist."""
        response = self.client.get('/direct-chat/api/users/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 0)  # Only admin, so empty after exclude

    def test_list_users_with_other(self):
        """Test listing users when another user exists."""
        # Create another user
        other = User.objects.create_user(username='testuser', password='testpass')
        SenderModel.objects.get_or_create(user=other)
        ReceiverModel.objects.get_or_create(user=other)
        ChatKeyModel.objects.get_or_create(usernames=[other.username])

        response = self.client.get('/direct-chat/api/users/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]['username'], 'testuser')

    def test_chat_history_with_self(self):
        """Test chat history endpoint (self-chat)."""
        response = self.client.get(f'/direct-chat/api/chat/{self.user.id}/history/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['other_user']['username'], 'admin')
        self.assertEqual(len(response.data['messages']), 0)

    def test_chat_history_with_other_user(self):
        """Test chat history with another user."""
        # Create another user
        other = User.objects.create_user(username='testuser', password='testpass')
        other_sender, _ = SenderModel.objects.get_or_create(user=other)
        other_receiver, _ = ReceiverModel.objects.get_or_create(user=other)
        ChatKeyModel.objects.get_or_create(usernames=[other.username])

        # Create some messages
        from directchat.models import ChatModel
        ChatModel.objects.create(
            sender=self.user.sendermodel,
            receiver=other_receiver,
            text='Hello from admin'
        )
        ChatModel.objects.create(
            sender=other_sender,
            receiver=self.user.receivermodel,
            text='Hi back from testuser'
        )

        response = self.client.get(f'/direct-chat/api/chat/{other.id}/history/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['other_user']['username'], 'testuser')
        self.assertEqual(len(response.data['messages']), 2)


class AutoLoginWebsocketTests(TransactionTestCase):
    """Test WebSocket with auto-login as admin/password."""

    def setUp(self):
        """Create admin user and authenticate automatically."""
        # Create admin user
        self.user = User.objects.create_user(
            username='admin',
            email='admin@example.com',
            password='password'
        )
        # Ensure sender/receiver models exist
        SenderModel.objects.get_or_create(user=self.user)
        ReceiverModel.objects.get_or_create(user=self.user)
        ChatKeyModel.objects.get_or_create(usernames=[self.user.username])

        # Create another user for chatting
        self.other = User.objects.create_user(username='testuser', password='testpass')
        SenderModel.objects.get_or_create(user=self.other)
        ReceiverModel.objects.get_or_create(user=self.other)
        ChatKeyModel.objects.get_or_create(usernames=[self.other.username])
        # Create chat key for both
        ChatKeyModel.objects.get_or_create(usernames=['admin', 'testuser'])

        # Get JWT token
        self.token = str(AccessToken.for_user(self.user))

    async def test_websocket_connect(self):
        """Test WebSocket connection with JWT auth."""
        from channels.testing import WebsocketCommunicator
        from django.conf import settings

        # Debug: Check ASGI application
        print(f"\n[DEBUG] Testing WebSocket connection...")
        print(f"[DEBUG] User: {self.user.username}, ID: {self.user.id}")
        print(f"[DEBUG] Other user: {self.other.username}, ID: {self.other.id}")
        print(f"[DEBUG] Token: {self.token[:20]}...")

        communicator = WebsocketCommunicator(
            application,
            f"/ws/direct-chat/{self.other.id}/?token={self.token}"
        )
        connected, response = await communicator.connect()
        print(f"[DEBUG] Connected: {connected}, Response: {response}")
        if not connected:
            print(f"[DEBUG] Connection failed - check ASGI routing and JWT middleware")
        self.assertTrue(connected)
        await communicator.disconnect()

    async def test_websocket_send_receive(self):
        """Test sending and receiving messages via WebSocket."""
        # Connect first user (admin)
        communicator1 = WebsocketCommunicator(
            application,
            f"/ws/direct-chat/{self.other.id}/?token={self.token}"
        )
        connected1, _ = await communicator1.connect()
        self.assertTrue(connected1)

        # Get token for second user
        token2 = str(AccessToken.for_user(self.other))
        communicator2 = WebsocketCommunicator(
            application,
            f"/ws/direct-chat/{self.user.id}/?token={token2}"
        )
        connected2, _ = await communicator2.connect()
        self.assertTrue(connected2)

        # Send message from admin
        await communicator1.send_json_to({"message": "Hello from admin!"})

        # Receive message on second communicator
        response = await communicator2.receive_json_from(timeout=5)
        self.assertIn('chatmodel', response)
        self.assertEqual(response['chatmodel']['text'], 'Hello from admin!')
        self.assertEqual(response['chatmodel']['sender_username'], 'admin')

        await communicator1.disconnect()
        await communicator2.disconnect()

    async def test_websocket_unauthorized(self):
        """Test WebSocket rejects connection without valid token."""
        communicator = WebsocketCommunicator(
            application,
            f"/ws/direct-chat/{self.other.id}/?token=invalid_token"
        )
        connected, _ = await communicator.connect()
        self.assertFalse(connected)


# Simple script to run tests manually
if __name__ == '__main__':
    import os
    import django
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'burst.settings.dev')
    django.setup()

    # Run with: python directchat/test_auto_login.py
    print("Run tests with: python manage.py test directchat.test_auto_login")
