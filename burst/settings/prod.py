"""
Production settings for burst project.
"""
# ASGI_APPLICATION = None

import os
from .base import *
import dj_database_url

# Remove daphne (ASGI server) for PythonAnywhere WSGI deployment
INSTALLED_APPS = [app for app in INSTALLED_APPS if app != 'daphne']

# Remove channels-related apps for WSGI deployment (PythonAnywhere doesn't support WebSockets)
INSTALLED_APPS = [app for app in INSTALLED_APPS if app not in ['channels', 'channels_redis']]

# Remove directchat app since it depends on channels
INSTALLED_APPS = [app for app in INSTALLED_APPS if app != 'directchat']
STORAGES = {
    "default": {
        "BACKEND": "cloudinary_storage.storage.MediaCloudinaryStorage",
    },
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}
# Remove channel layers configuration since channels is not installed
CHANNEL_LAYERS = None

DEBUG = False

ALLOWED_HOSTS = os.environ.get('ALLOWED_HOSTS', '*').split(',')

frontend_url = os.getenv('FRONTEND_URL', '').rstrip('/')

# Production CORS settings
CORS_ALLOWED_ORIGINS = [origin for origin in [
    frontend_url,
    os.environ.get('ADDITIONAL_CORS_ORIGINS', ''),
].split(',') if origin]

CORS_ALLOWED_ORIGIN_REGEXES = [
    r"^https://.*\.vercel\.app$",
    r"^https://.*\.pythonanywhere\.com$",
]

CSRF_TRUSTED_ORIGINS = [origin for origin in [
    frontend_url,
    os.environ.get('ADDITIONAL_CSRF_ORIGINS', ''),
].split(',') if origin]

# Use local filesystem storage for media in production
# DEFAULT_FILE_STORAGE = 'cloudinary_storage.storage.MediaCloudinaryStorage'  # Commented out - using local storage

# Security settings for production
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
USE_X_FORWARDED_HOST = True

# Database - Postgres (Neon) in production
DATABASES = {
    'default': dj_database_url.config(
        default=os.environ.get('DATABASE_URL'),
        conn_max_age=600,
        conn_health_checks=True,
        ssl_require=True,
    )
}

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
