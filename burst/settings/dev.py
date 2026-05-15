"""
Development settings for burst project.
"""

import os
from .base import *
import dj_database_url

DEBUG = True

ALLOWED_HOSTS = ['localhost', '127.0.0.1']

CORS_ALLOWED_ORIGINS = [
    "http://localhost:3000",
]

CSRF_TRUSTED_ORIGINS = [
    "http://localhost:3000",
]

# Use local filesystem storage for media in development
DEFAULT_FILE_STORAGE = 'django.core.files.storage.FileSystemStorage'
MEDIA_URL = '/uploads/'
MEDIA_ROOT = os.path.join(BASE_DIR, 'media')

# Disable Cloudinary in development - use local storage instead
CLOUDINARY_STORAGE = {
    'CLOUD_NAME': None,
    'API_KEY': None,
    'API_SECRET': None
}

# Ensure we don't use Cloudinary storage in development
INSTALLED_APPS = [
    app for app in INSTALLED_APPS 
    if app not in ['cloudinary_storage', 'cloudinary']
]

# Database - fallback to SQLite if no DATABASE_URL
DATABASES = {
    'default': dj_database_url.config(
        default=os.environ.get('DATABASE_URL', f'sqlite:///{os.path.join(BASE_DIR, "db.sqlite3")}'),
        conn_max_age=600
    )
}

# Email backend for development (prints to console)
EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'

# Logging
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
        },
    },
    'root': {
        'handlers': ['console'],
        'level': 'DEBUG',
    },
}

# Ensure media directory exists
import os
if not os.path.exists(MEDIA_ROOT):
    os.makedirs(MEDIA_ROOT, exist_ok=True)
