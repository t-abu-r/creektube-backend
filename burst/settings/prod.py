"""
Production settings for burst project.
"""

import os
from .base import *
import dj_database_url

DEBUG = False

ALLOWED_HOSTS = ['*']

frontend_url = os.getenv('FRONTEND_URL', 'https://ahmadateeb.pythonanywhere.com').rstrip('/')

# Production CORS settings
CORS_ALLOWED_ORIGINS = [
    frontend_url,
]

CORS_ALLOWED_ORIGIN_REGEXES = [
    r"^https://.*\.vercel\.app$",
    r"^https://.*\.pythonanywhere\.com$",
]

CSRF_TRUSTED_ORIGINS = [
    frontend_url,
]

# Use local filesystem storage for media in production
# DEFAULT_FILE_STORAGE = 'cloudinary_storage.storage.MediaCloudinaryStorage'  # Commented out - using local storage

# Security settings for production
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
USE_X_FORWARDED_HOST = True

# Database - use SQLite from base settings
# DATABASES already set in base.py

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
        'level': 'ERROR',
    },
}
