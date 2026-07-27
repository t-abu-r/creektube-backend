"""
Production settings for burst project.
"""

import os
from .base import *
import dj_database_url

INSTALLED_APPS = INSTALLED_APPS + ['cloudinary_storage']

STORAGES = {
    "default": {
        "BACKEND": "cloudinary_storage.storage.MediaCloudinaryStorage",
    },
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedStaticFilesStorage",
    },
}
WHITENOISE_USE_FINDERS = True

DEBUG = False

ALLOWED_HOSTS = os.environ.get('ALLOWED_HOSTS', '*').split(',')

frontend_url = os.getenv('FRONTEND_URL', '').rstrip('/')

# Production CORS settings
CORS_ALLOWED_ORIGINS = [origin for origin in [
    frontend_url,
    *os.environ.get('ADDITIONAL_CORS_ORIGINS', '').split(','),
] if origin]

CORS_ALLOW_CREDENTIALS = True

CORS_ALLOWED_ORIGIN_REGEXES = [
    r"^https://.*\.vercel\.app$",
    r"^https://.*\.pythonanywhere\.com$",
]

CSRF_TRUSTED_ORIGINS = [origin for origin in [
    frontend_url,
    *os.environ.get('ADDITIONAL_CSRF_ORIGINS', '').split(','),
] if origin]



# Security settings for production
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
USE_X_FORWARDED_HOST = True

# Database - Postgres (Neon) in production
_database_url = os.environ.get('DATABASE_URL', '')
if _database_url and '://' in _database_url:
    DATABASES = {
        'default': dj_database_url.config(
            default=_database_url,
            conn_max_age=600,
            conn_health_checks=True,
        )
    }
else:
    # Build-time fallback — Vercel build has no DATABASE_URL
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'db.sqlite3'),
        }
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
