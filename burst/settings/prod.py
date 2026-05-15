"""
Production settings for burst project.
"""

import os
from .base import *
import dj_database_url

DEBUG = False

ALLOWED_HOSTS = ['*']

# Production CORS settings
CORS_ALLOWED_ORIGINS = [
    "https://creektube-frontend.vercel.app",
    "https://creektube-production.up.railway.app",
]

CORS_ALLOWED_ORIGIN_REGEXES = [
    r"^https://.*\.vercel\.app$",
    r"^https://.*\.railway\.app$",
]

CSRF_TRUSTED_ORIGINS = [
    "https://creektube-frontend.vercel.app",
    "https://creektube-production.up.railway.app",
]

# Use Cloudinary for media storage in production
DEFAULT_FILE_STORAGE = 'cloudinary_storage.storage.MediaCloudinaryStorage'

# Security settings for production
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
USE_X_FORWARDED_HOST = True

# Database - must be set via environment variable
DATABASES = {
    'default': dj_database_url.config(
        default=os.environ.get('DATABASE_URL'),
        conn_max_age=600
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
