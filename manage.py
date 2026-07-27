#!/usr/bin/env python
"""Django's command-line utility for administrative tasks."""
import os
import sys
from dotenv import load_dotenv

load_dotenv()

def main():
    """Run administrative tasks."""
    # Vercel build masks DJANGO_SETTINGS_MODULE as [SENSITIVE] via GitHub Actions
    # Force prod for production, set DJANGO_SETTINGS_MODULE=burst.settings.dev for local dev
    settings = os.environ.get('DJANGO_SETTINGS_MODULE', '')
    if not settings or '[SENSITIVE]' in settings:
        settings = 'burst.settings.prod'
    os.environ['DJANGO_SETTINGS_MODULE'] = settings
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "Couldn't import Django. Are you sure it's installed and "
            "available on your PYTHONPATH environment variable? Did you "
            "forget to activate a virtual environment?"
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == '__main__':
    main()
