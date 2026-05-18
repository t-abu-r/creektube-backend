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
MEDIA_URL = '/media/'
MEDIA_ROOT = os.path.join(BASE_DIR, 'media')

# Cloudinary disabled - using local storage
# CLOUDINARY_STORAGE = {
#     'CLOUD_NAME': None,
#     'API_KEY': None,
#     'API_SECRET': None
# }

# Cloudinary apps already removed in base.py, no need to filter here
INSTALLED_APPS = INSTALLED_APPS

# Database - use SQLite from base settings
# DATABASES already set in base.py

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
