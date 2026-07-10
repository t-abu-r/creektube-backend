import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

os.environ.setdefault(
    "DJANGO_SETTINGS_MODULE",
    "burst.settings.prod"
)

import django
django.setup()

from django.core.management import call_command
call_command("migrate", "--noinput")

from django.core.wsgi import get_wsgi_application

app = get_wsgi_application()
