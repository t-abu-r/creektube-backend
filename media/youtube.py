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
import time

import requests

from django.utils import timezone

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


# ---------------------------------------------------------------------------
# Feed mixing.
#
# The hybrid feed blends native CreekTube uploads with live YouTube results.
# YouTube items are never persisted: they are fetched from the Data API per
# feed request (with a short TTL cache) and shaped into the same JSON the
# frontend already understands. A missing API key or a quota/network error
# degrades to the native-only feed, so CreekTube behavior is never worse.
# ---------------------------------------------------------------------------

API_BASE = "https://www.googleapis.com/youtube/v3"
# YouTube Data API v3 quotas: a search costs 100 units, a videos/details
# lookup costs 1 unit. These knobs keep a single feed request well within
# the daily 10k-unit budget.
FEED_MAX_RESULTS_PER_QUERY = 12
FEED_TOTAL_ITEMS = 18
FEED_CACHE_TTL_SECONDS = 600

# Fallback topics used when a user has no recorded interests yet.
DEFAULT_FEED_QUERIES = ["music", "gaming", "vlogs", "science", "tech"]

# Simple in-process TTL cache keyed by (kind, value). Safe across concurrent
# requests and never raises.
_cache = {}


def _cache_get(key):
    entry = _cache.get(key)
    if not entry:
        return None
    if time.monotonic() - entry["ts"] > FEED_CACHE_TTL_SECONDS:
        _cache.pop(key, None)
        return None
    return entry["value"]


def _cache_set(key, value, ttl=FEED_CACHE_TTL_SECONDS):
    _cache[key] = {"ts": time.monotonic(), "value": value, "ttl": ttl}


def _api_key():
    from django.conf import settings

    return (getattr(settings, "YOUTUBE_API_KEY", "") or "").strip()


def _youtube_request(endpoint, params):
    """Fire a YouTube Data API request. Returns the parsed JSON or ``None``."""
    key = _api_key()
    if not key:
        return None
    params = dict(params)
    params["key"] = key
    try:
        response = requests.get(f"{API_BASE}/{endpoint}", params=params, timeout=8)
        response.raise_for_status()
        return response.json()
    except Exception as exc:
        logger.warning("YouTube %s request failed: %s", endpoint, exc)
        return None


def _category_to_query(category_slug):
    """Turn a CreekTube category slug into a YouTube search query."""
    if not category_slug:
        return ""
    return category_slug.replace("-", " ").strip()


def _feed_queries_for(user, interest_categories=None, feed="following"):
    """Build the list of YouTube search queries for a feed request.

    Logged-in users get queries from their strongest interests (category
    slugs the profile has actually engaged with). Everyone else gets the
    default topic pool.
    """
    queries = []
    if interest_categories:
        ranked_cats = sorted(
            ((slug, score) for slug, score in interest_categories.items() if score > 0),
            key=lambda pair: pair[1],
            reverse=True,
        )
        queries = [_category_to_query(slug) for slug, _ in ranked_cats[:4]]
    if feed == "following":
        # Follow feeds still benefit from a little discovery outside the pool.
        queries = (queries or []) + ["popular right now"]
    return [q for q in (queries or DEFAULT_FEED_QUERIES[:2]) if q]


def youtube_search_results(query, max_results=FEED_MAX_RESULTS_PER_QUERY):
    """Search YouTube for ``query`` and return raw API items (cached)."""
    query = (query or "").strip()
    if not query or not _api_key():
        return []
    cache_key = ("search", query, max_results)
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    data = _youtube_request("search", {
        "part": "snippet",
        "type": "video",
        "q": query,
        "maxResults": max_results,
        "safeSearch": "moderate",
    })
    items = (data or {}).get("items") or []
    _cache_set(cache_key, items)
    return items


def _view_counts_for(video_ids):
    """Fetch view counts for up to 50 video IDs in a single API call."""
    video_ids = [vid for vid in video_ids if validate_youtube_id(vid)]
    if not video_ids or not _api_key():
        return {}
    cache_key = ("stats", ",".join(sorted(video_ids)))
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    data = _youtube_request("videos", {
        "part": "statistics",
        "id": ",".join(video_ids[:50]),
    })
    counts = {}
    for item in (data or {}).get("items") or []:
        stats = item.get("statistics") or {}
        raw = stats.get("viewCount") or "0"
        try:
            counts[item["id"]] = int(raw)
        except (TypeError, ValueError):
            counts[item["id"]] = 0
    _cache_set(cache_key, counts)
    return counts


def youtube_feed_item(item, category_slug="", category_name="", view_count=None):
    """Shape a raw YouTube search API item into a feed-ready video dict.

    The shape mirrors what ``VideoSerializer`` already returns so the
    frontend (VideoCard + WatchVideo) can render YouTube and CreekTube
    content uniformly.
    """
    raw_id = item.get("id")
    if isinstance(raw_id, dict):
        video_id = ((raw_id or {}).get("videoId") or "").strip()
    else:
        video_id = (raw_id or "").strip()
    if not validate_youtube_id(video_id):
        return None
    snippet = item.get("snippet") or {}
    now = timezone.now().isoformat()
    return {
        "id": video_id,
        "category": category_slug or "",
        "category_name": category_name or "",
        "title": (snippet.get("title") or "YouTube video").strip(),
        "description": (snippet.get("description") or "").strip(),
        "thumbnail": youtube_thumbnail_url(video_id),
        "video": None,
        "timestamp": (snippet.get("publishedAt") or now),
        "is_approved": True,
        "author": (snippet.get("channelTitle") or "YouTube").strip(),
        "author_id": None,
        "author_avatar": None,
        "author_active": True,
        "comments": [],
        "view_count": view_count or 0,
        "source_type": "YOUTUBE",
        "youtube_video_id": video_id,
        "youtube_channel_id": (snippet.get("channelId") or "").strip(),
        "youtube_channel_name": (snippet.get("channelTitle") or "").strip(),
        "embed_url": youtube_embed_url(video_id),
    }


def build_youtube_feed(user=None, interest_categories=None, limit=FEED_TOTAL_ITEMS, feed=None):
    """Build a list of live YouTube feed items.

    ``user`` may be None (guest). Interests come from the user's MediaProfile
    categories (``{category_slug: score}``). Returns an empty list whenever the
    API key is missing or the calls fail, so the native feed is never harmed.
    """
    if not _api_key() or limit <= 0:
        return []

    queries = _feed_queries_for(user, interest_categories, feed=feed)
    items = []
    seen_ids = set()
    for query in queries:
        for raw in youtube_search_results(query):
            item = youtube_feed_item(raw, category_slug=query, category_name=query.title())
            if not item or item["id"] in seen_ids:
                continue
            seen_ids.add(item["id"])
            items.append(item)
        if len(items) >= limit:
            break

    items = items[:limit]
    if items:
        counts = _view_counts_for([item["id"] for item in items])
        for item in items:
            item["view_count"] = counts.get(item["id"], 0)
    return items


def get_youtube_video_details(video_id):
    """Fetch one video's snippet + statistics and return a feed-ready dict.

    Used by the watch endpoints so a YouTube ID that isn't stored as a
    CreekTube row can still be played through the iframe embed.
    """
    if not validate_youtube_id(video_id) or not _api_key():
        return None
    cache_key = ("video", video_id)
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    data = _youtube_request("videos", {"part": "snippet,statistics", "id": video_id})
    items = (data or {}).get("items") or []
    if not items:
        return None
    snippet = items[0].get("snippet") or {}
    stats = items[0].get("statistics") or {}
    try:
        view_count = int(stats.get("viewCount") or 0)
    except (TypeError, ValueError):
        view_count = 0
    item = {
        "id": video_id,
        "snippet": snippet,
    }
    shaped = youtube_feed_item(
        item,
        category_slug="",
        category_name="",
        view_count=view_count,
    )
    if shaped:
        _cache_set(cache_key, shaped)
    return shaped


def youtube_related_videos(video_id, limit=5):
    """Suggest related YouTube videos for a YouTube watch page.

    Uses the video's own title as the search query, so results are topical.
    """
    if not validate_youtube_id(video_id) or not _api_key():
        return []
    details = get_youtube_video_details(video_id) or {}
    title = details.get("title", "")
    if not title:
        return []
    related = []
    seen = {video_id}
    for raw in youtube_search_results(title, max_results=limit + 2):
        item = youtube_feed_item(raw)
        if not item or item["id"] in seen:
            continue
        seen.add(item["id"])
        related.append(item)
        if len(related) >= limit:
            break
    return related


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
