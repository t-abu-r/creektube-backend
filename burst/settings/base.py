"""
Base settings for burst project.
Shared settings for all environments.
"""

import os
from pathlib import Path
from datetime import timedelta
import dj_database_url

BASE_DIR = Path(__file__).resolve().parent.parent.parent

# Application definition
INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'channels',
    'corsheaders',
    'rest_framework',
    'accounts',
    "media",
    "directchat",
    "rest_framework_simplejwt.token_blacklist",
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'corsheaders.middleware.CorsMiddleware',
    'burst.middleware.RangeFileMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'burst.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'burst.wsgi.application'
ASGI_APPLICATION = 'burst.asgi.application'

# Redis is optional. A loopback/localhost REDIS_URL is never reachable from
# Vercel serverless functions (each instance is ephemeral with no local Redis),
# so treat it as unconfigured and fall back to in-process layers.
def _redis_url():
    url = (os.environ.get("REDIS_URL") or "").strip()
    if not url:
        return ""
    for marker in ("localhost", "127.0.0.1", "0.0.0.0", "::1"):
        if marker in url:
            return ""
    return url


_REDIS_URL = _redis_url()

# Channels configuration
CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels_redis.core.RedisChannelLayer",
        "CONFIG": {
            "hosts": [_REDIS_URL],
        },
    },
} if _REDIS_URL else {
    "default": {
        "BACKEND": "channels.layers.InMemoryChannelLayer",
    },
}

# Password validation
AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]

def _int_env(key, default):
    val = os.environ.get(key)
    if val is None:
        return default
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=_int_env("JWT_ACCESS_TOKEN_LIFETIME_MINUTES", 15)),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=_int_env("JWT_REFRESH_TOKEN_LIFETIME_DAYS", 30)),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
}

# Internationalization
LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True

# Static files
STATIC_URL = 'static/'
STATIC_ROOT = os.path.join(BASE_DIR, 'staticfiles')
STATICFILES_STORAGE = "django.contrib.staticfiles.storage.StaticFilesStorage"

# Media files
MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'

# Default primary key field type
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# REST Framework
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "accounts.authentication.CookieJWTAuthentication",
    ),
}

# Email settings
EMAIL_HOST = os.environ.get("EMAIL_HOST", "smtp-relay.brevo.com")
EMAIL_PORT = _int_env("EMAIL_PORT", 587)
EMAIL_USE_TLS = os.environ.get("EMAIL_USE_TLS", "True").lower() == "true"
EMAIL_HOST_USER = os.environ.get("EMAIL_HOST_USER")
EMAIL_HOST_PASSWORD = os.environ.get("EMAIL_HOST_PASSWORD")
DEFAULT_FROM_EMAIL = os.environ.get("DEFAULT_FROM_EMAIL", f"OutReach <outreach@germanypathway.com>" if EMAIL_HOST_USER else None)
PASSWORD_RESET_TIMEOUT = 60 * 15

# Cloudinary settings
CLOUDINARY_STORAGE = {
    'CLOUD_NAME': os.environ.get('CLOUDINARY_CLOUD_NAME'),
    'API_KEY': os.environ.get('CLOUDINARY_API_KEY'),
    'API_SECRET': os.environ.get('CLOUDINARY_API_SECRET')
}

# YouTube Data API (optional). Used to enrich metadata when creators add
# YouTube videos. Missing key = graceful fallback, native behavior unchanged.
YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY", "")

# LiveKit (real-time video calls). Missing keys = the call feature is disabled
# and the token endpoint returns a 503 rather than crashing.
LIVEKIT_API_KEY = os.environ.get("LIVEKIT_API_KEY", "")
LIVEKIT_API_SECRET = os.environ.get("LIVEKIT_API_SECRET", "")
LIVEKIT_WS_URL = os.environ.get("LIVEKIT_WS_URL", "")

# Shared YouTube result cache. Redis is used when REDIS_URL points at a
# reachable (non-loopback) host so feed/search results survive worker restarts
# and are shared across serverless instances; otherwise the in-process cache
# keeps behavior unchanged.
YOUTUBE_SHARED_CACHE = bool(_REDIS_URL)
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
    },
    "youtube": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": _REDIS_URL,
    } if _REDIS_URL else {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
    },
}

# Jazzmin admin settings
JAZZMIN_SETTINGS = {
    "site_title": "CreekTube Admin",
    "site_header": "CreekTube",
    "site_brand": "CreekTube",
    "welcome_sign": "Welcome to CreekTube Admin",
    "copyright": "CreekTube",
}

# File upload limits
DATA_UPLOAD_MAX_MEMORY_SIZE = _int_env("DATA_UPLOAD_MAX_MEMORY_SIZE", 524288000)
FILE_UPLOAD_MAX_MEMORY_SIZE = _int_env("FILE_UPLOAD_MAX_MEMORY_SIZE", 524288000)
WHITENOISE_USE_FINDERS = True

# Secrets
SECRET_KEY = os.environ.get('SECRET_KEY', 'django-insecure-fallback-key-change-in-production')

# Frontend URL for email links
FRONTEND_URL = os.environ.get('FRONTEND_URL', 'http://localhost:3000')

# Database - use DATABASE_URL if provided, fallback to SQLite
_database_url = os.environ.get('DATABASE_URL', '')
if _database_url and '://' in _database_url:
    DATABASES = {
        'default': dj_database_url.config(
            default=_database_url,
            conn_max_age=600,
        )
    }
else:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': BASE_DIR / 'db.sqlite3',
        }
    }
