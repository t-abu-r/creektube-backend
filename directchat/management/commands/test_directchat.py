"""
Management command to test directchat with auto-login.
Usage: python manage.py test_directchat
"""
from django.core.management.base import BaseCommand
from django.contrib.auth.models import User
from rest_framework_simplejwt.tokens import AccessToken
from directchat.models import SenderModel, ReceiverModel, ChatKeyModel


class Command(BaseCommand):
    help = 'Test directchat with auto-login as admin/password'

    def handle(self, *args, **kwargs):
        self.stdout.write("=" * 50)
        self.stdout.write(self.style.SUCCESS("DirectChat Auto-Login Test"))
        self.stdout.write("=" * 50)

        # Create admin user
        user, created = User.objects.get_or_create(
            username='admin',
            defaults={'email': 'admin@example.com', 'is_staff': True}
        )
        if created or not user.check_password('password'):
            user.set_password('password')
            user.save()
            self.stdout.write(self.style.SUCCESS("[OK] Created admin user: admin / password"))
        else:
            self.stdout.write(self.style.SUCCESS("[OK] Admin user exists: admin / password"))

        # Ensure sender/receiver models exist
        SenderModel.objects.get_or_create(user=user)
        ReceiverModel.objects.get_or_create(user=user)
        ChatKeyModel.objects.get_or_create(usernames=[user.username])

        # Generate JWT token
        token = str(AccessToken.for_user(user))
        self.stdout.write(self.style.SUCCESS(f"[OK] JWT Token: {token[:30]}..."))

        # Create a test user for chatting
        test_user, created = User.objects.get_or_create(
            username='testuser',
            defaults={'email': 'test@example.com'}
        )
        if created:
            test_user.set_password('testpass')
            test_user.save()
            SenderModel.objects.get_or_create(user=test_user)
            ReceiverModel.objects.get_or_create(user=test_user)
            ChatKeyModel.objects.get_or_create(usernames=[test_user.username])
            ChatKeyModel.objects.get_or_create(usernames=['admin', 'testuser'])
            self.stdout.write(self.style.SUCCESS("[OK] Created test user: testuser"))
        else:
            self.stdout.write(self.style.SUCCESS("[OK] Test user exists: testuser"))

        # Output test URLs
        self.stdout.write("\n" + "=" * 50)
        self.stdout.write(self.style.NOTICE("Test URLs (with JWT token):"))
        self.stdout.write("=" * 50)
        self.stdout.write(f"\n1. API - List Users:")
        self.stdout.write(f"   GET http://localhost:8000/direct-chat/api/users/")
        self.stdout.write(f"   Authorization: Bearer {token[:50]}...")

        self.stdout.write(f"\n2. API - Chat History with testuser:")
        self.stdout.write(f"   GET http://localhost:8000/direct-chat/api/chat/{test_user.id}/history/")
        self.stdout.write(f"   Authorization: Bearer {token[:50]}...")

        self.stdout.write(f"\n3. WebSocket - Connect to testuser:")
        self.stdout.write(f"   ws://localhost:8000/ws/direct-chat/{test_user.id}/?token={token}")

        self.stdout.write("\n" + "=" * 50)
        self.stdout.write(self.style.SUCCESS("Ready to test! Start the server with:"))
        self.stdout.write(self.style.NOTICE("   python manage.py runserver"))
        self.stdout.write("=" * 50)

        # Option to run API tests
        self.stdout.write("\n")
        self.test_api(token, user.id, test_user.id)

    def test_api(self, token, admin_id, testuser_id):
        """Run quick API tests."""
        import requests

        self.stdout.write(self.style.NOTICE("\n[TEST] Running API tests..."))
        headers = {"Authorization": f"Bearer {token}"}

        # Test 1: List users
        try:
            response = requests.get(
                "http://localhost:8000/direct-chat/api/users/",
                headers=headers,
                timeout=5
            )
            if response.status_code == 200:
                users = response.json()
                self.stdout.write(self.style.SUCCESS(f"[PASS] List Users: {len(users)} users found"))
            else:
                self.stdout.write(self.style.ERROR(f"[FAIL] List Users: Status {response.status_code}"))
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"[FAIL] List Users: {e}"))

        # Test 2: Chat history
        try:
            response = requests.get(
                f"http://localhost:8000/direct-chat/api/chat/{testuser_id}/history/",
                headers=headers,
                timeout=5
            )
            if response.status_code == 200:
                data = response.json()
                self.stdout.write(self.style.SUCCESS(
                    f"[PASS] Chat History: {len(data.get('messages', []))} messages"
                ))
            else:
                self.stdout.write(self.style.ERROR(f"[FAIL] Chat History: Status {response.status_code}"))
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"[FAIL] Chat History: {e}"))

        self.stdout.write("\n" + "=" * 50)
