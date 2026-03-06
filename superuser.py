import os
import django
from django.contrib.auth import get_user_model

# Set up Django environment
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "burst.settings")
django.setup()

User = get_user_model()

# Superuser details
username = "admin"
email = "admin@example.com"
password = "temporarypassword123"

# Check if superuser already exists
if not User.objects.filter(username=username).exists():
    print("Creating superuser...")
    User.objects.create_superuser(username=username, email=email, password=password)
    print("Superuser created!")
else:
    print("Superuser already exists.")