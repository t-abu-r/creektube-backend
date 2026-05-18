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
