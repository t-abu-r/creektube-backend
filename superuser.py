# setup_admin.py
import os
import django

# Manually point to your settings
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "burst.settings")
django.setup()

from django.contrib.auth import get_user_model

User = get_user_model()
username = 'admin'
email = 'admin@example.com'
password = 'password123'

if not User.objects.filter(username=username).exists():
    print(f"Creating superuser: {username}")
    User.objects.create_superuser(username, email, password)
else:
    print("Superuser already exists, skipping.")
