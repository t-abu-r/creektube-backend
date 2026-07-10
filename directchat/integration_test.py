"""
Integration test - Auto login and test directchat.
Usage: python manage.py shell < directchat/integration_test.py
"""
import os
import sys
import django

# Setup Django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'burst.settings.dev')
django.setup()

from django.contrib.auth.models import User
from rest_framework_simplejwt.tokens import AccessToken
import requests
import websocket
import json
import threading
import time

# Configuration
BASE_URL = os.environ.get("TEST_BASE_URL", "http://localhost:8000")
WS_URL = os.environ.get("TEST_WS_URL", "ws://localhost:8000")
ADMIN_USERNAME = os.environ.get("TEST_ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("TEST_ADMIN_PASSWORD", "password")


def setup_admin_user():
    """Create or get admin user with password 'password'."""
    from directchat.models import SenderModel, ReceiverModel, ChatKeyModel

    user, created = User.objects.get_or_create(
        username=ADMIN_USERNAME,
        defaults={'email': 'admin@example.com', 'is_staff': True}
    )
    if created or not user.check_password(ADMIN_PASSWORD):
        user.set_password(ADMIN_PASSWORD)
        user.save()
        print(f"[OK] Created admin user: {ADMIN_USERNAME}")
    else:
        print(f"[OK] Admin user exists: {ADMIN_USERNAME}")

    # Ensure sender/receiver models exist
    SenderModel.objects.get_or_create(user=user)
    ReceiverModel.objects.get_or_create(user=user)
    ChatKeyModel.objects.get_or_create(usernames=[user.username])

    return user


def get_jwt_token(user):
    """Generate JWT token for user."""
    token = str(AccessToken.for_user(user))
    print(f"[OK] Generated JWT token: {token[:20]}...")
    return token


def test_api_list_users(token):
    """Test API: List users endpoint."""
    print("\n[TEST] API: List users")
    headers = {"Authorization": f"Bearer {token}"}

    try:
        response = requests.get(
            f"{BASE_URL}/direct-chat/api/users/",
            headers=headers,
            timeout=5
        )
        print(f"[OK] Status: {response.status_code}")
        print(f"[OK] Users: {response.json()}")
        return response.status_code == 200
    except Exception as e:
        print(f"[FAIL] Error: {e}")
        return False


def test_api_chat_history(token, user_id):
    """Test API: Chat history endpoint."""
    print(f"\n[TEST] API: Chat history with user {user_id}")
    headers = {"Authorization": f"Bearer {token}"}

    try:
        response = requests.get(
            f"{BASE_URL}/direct-chat/api/chat/{user_id}/history/",
            headers=headers,
            timeout=5
        )
        print(f"[OK] Status: {response.status_code}")
        data = response.json()
        print(f"[OK] Other user: {data.get('other_user')}")
        print(f"[OK] Messages count: {len(data.get('messages', []))}")
        return response.status_code == 200
    except Exception as e:
        print(f"[FAIL] Error: {e}")
        return False


def test_websocket(token, other_user_id):
    """Test WebSocket connection and messaging."""
    print(f"\n[TEST] WebSocket: Connect and send message")

    ws_url = f"{WS_URL}/ws/direct-chat/{other_user_id}/?token={token}"
    print(f"[INFO] Connecting to: {ws_url}")

    messages_received = []
    connection_open = [False]

    def on_message(ws, message):
        data = json.loads(message)
        messages_received.append(data)
        print(f"[OK] Received: {data}")

    def on_error(ws, error):
        print(f"[FAIL] WebSocket error: {error}")

    def on_close(ws, close_status_code, close_msg):
        print(f"[INFO] WebSocket closed: {close_status_code}")

    def on_open(ws):
        print("[OK] WebSocket connected!")
        connection_open[0] = True
        # Send a test message
        ws.send(json.dumps({"message": "Hello from admin!"}))
        print("[OK] Sent message: Hello from admin!")

    ws = websocket.WebSocketApp(
        ws_url,
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close
    )

    # Run WebSocket in separate thread
    ws_thread = threading.Thread(target=ws.run_forever)
    ws_thread.daemon = True
    ws_thread.start()

    # Wait for connection and message
    time.sleep(3)
    ws.close()

    return connection_open[0]


def run_all_tests():
    """Run all tests automatically."""
    print("=" * 50)
    print("DirectChat Integration Test - Auto Login")
    print("=" * 50)

    # Step 1: Setup admin user
    print("\n[SETUP] Creating admin user...")
    admin_user = setup_admin_user()

    # Step 2: Get JWT token
    print("\n[SETUP] Getting JWT token...")
    token = get_jwt_token(admin_user)

    # Step 3: Test API endpoints
    results = []
    results.append(("API List Users", test_api_list_users(token)))
    results.append(("API Chat History", test_api_chat_history(token, admin_user.id)))

    # Step 4: Test WebSocket
    results.append(("WebSocket Connect", test_websocket(token, admin_user.id)))

    # Summary
    print("\n" + "=" * 50)
    print("TEST SUMMARY")
    print("=" * 50)
    for name, passed in results:
        status = "PASS" if passed else "FAIL"
        print(f"  {name}: {status}")

    all_passed = all(r[1] for r in results)
    print("=" * 50)
    if all_passed:
        print("All tests PASSED!")
    else:
        print("Some tests FAILED!")
    print("=" * 50)

    return all_passed


if __name__ == "__main__":
    # When run directly
    success = run_all_tests()
    sys.exit(0 if success else 1)
