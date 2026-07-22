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
DATABASES = {
    'default': dj_database_url.config(
        default=os.environ.get('DATABASE_URL'),
        conn_max_age=600,
        conn_health_checks=True,
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
