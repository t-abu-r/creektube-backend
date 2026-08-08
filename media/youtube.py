"""YouTube source support for CreekTube.

Native CreekTube videos are uploaded and hosted on Cloudinary; YouTube
videos are referenced by their 11-character video ID and played through
the official YouTube iframe embed. CreekTube never downloads, stores, or
proxies YouTube media, and never injects ads of its own.

This module is purely additive: all helpers degrade gracefully so that
existing CreekTube behavior is unchanged even when no YouTube API key is
configured.
"""

import logging
import re

import requests

logger = logging.getLogger(__name__)

# YouTube video IDs are 11 characters from [A-Za-z0-9_-].
YOUTUBE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
WATCH_URL_RE = re.compile(r"youtube\.com/watch[^#]*\b[?&]v=([A-Za-z0-9_-]{11})")
SHORTS_URL_RE = re.compile(r"youtube\.com/shorts/([A-Za-z0-9_-]{11})")
EMBED_URL_RE = re.compile(r"youtube\.com/embed/([A-Za-z0-9_-]{11})")
YOUDOTBE_URL_RE = re.compile(r"youtu\.be/([A-Za-z0-9_-]{11})")

URL_PATTERNS = (WATCH_URL_RE, SHORTS_URL_RE, EMBED_URL_RE, YOUDOTBE_URL_RE)


def validate_youtube_id(value):
    """Return True if ``value`` is a plausible 11-character YouTube ID."""
    return bool(value) and bool(YOUTUBE_ID_RE.match(value))


def normalize_youtube_url(value):
    """Extract a YouTube video ID from a URL or a raw ID.

    Accepts ``watch?v=``, ``youtu.be/``, ``/embed/``, ``/shorts/`` links and
    plain 11-character IDs. Returns the canonical video ID or ``None`` if the
    input does not look like a YouTube video reference.
    """
    if not value or not isinstance(value, str):
        return None
    value = value.strip()
    if validate_youtube_id(value):
        return value
    for pattern in URL_PATTERNS:
        match = pattern.search(value)
        if match:
            return match.group(1)
    return None


def youtube_thumbnail_url(video_id):
    """Standard YouTube thumbnail URL for a video ID (no download involved)."""
    if not validate_youtube_id(video_id):
        return ""
    return f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"


def youtube_embed_url(video_id):
    """Official YouTube iframe embed URL used by the frontend player."""
    if not validate_youtube_id(video_id):
        return ""
    return f"https://www.youtube.com/embed/{video_id}"


def get_video_metadata(video_id):
    """Fetch YouTube video metadata through the Data API (v3 ``videos``).

    Requires ``YOUTUBE_API_KEY`` in the environment. Returns a dict with
    ``title``, ``description``, ``thumbnail``, ``channel_id`` and
    ``channel_name``, or ``None`` when the key is missing or the call fails.
    Never raises: errors are logged and the caller falls back gracefully.
    """
    from django.conf import settings

    api_key = getattr(settings, "YOUTUBE_API_KEY", "")
    if not api_key or not validate_youtube_id(video_id):
        return None

    try:
        response = requests.get(
            "https://www.googleapis.com/youtube/v3/videos",
            params={"part": "snippet", "id": video_id, "key": api_key},
            timeout=8,
        )
        response.raise_for_status()
        items = response.json().get("items") or []
        if not items:
            return None
        snippet = items[0].get("snippet") or {}
        return {
            "title": (snippet.get("title") or "").strip(),
            "description": (snippet.get("description") or "").strip(),
            "thumbnail": youtube_thumbnail_url(video_id),
            "channel_id": (snippet.get("channelId") or "").strip(),
            "channel_name": (snippet.get("channelTitle") or "").strip(),
        }
    except Exception as exc:
        logger.warning("YouTube metadata fetch failed for %s: %s", video_id, exc)
        return None
