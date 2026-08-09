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

from .content import classify_content_type, SNIP, VIDEO

logger = logging.getLogger(__name__)

# YouTube video IDs are 11 characters from [A-Za-z0-9_-].
YOUTUBE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
WATCH_URL_RE = re.compile(r"youtube\.com/watch[^#]*\b[?&]v=([A-Za-z0-9_-]{11})")
SHORTS_URL_RE = re.compile(r"youtube\.com/shorts/([A-Za-z0-9_-]{11})")
EMBED_URL_RE = re.compile(r"youtube\.com/embed/([A-Za-z0-9_-]{11})")
YOUDOTBE_URL_RE = re.compile(r"youtu\.be/([A-Za-z0-9_-]{11})")

# YouTube Data API video category IDs mapped to CreekTube category slugs.
# When a user watches a YouTube video, its category feeds the same interest
# scoring that drives the CreekTube home feed, so unknown IDs fall back to a
# sensible generic category instead of silently dropping the signal.
YOUTUBE_CATEGORY_MAP = {
    "1": {"slug": "film-animation", "name": "Film & Animation"},
    "2": {"slug": "autos-vehicles", "name": "Autos & Vehicles"},
    "10": {"slug": "music", "name": "Music"},
    "15": {"slug": "pets-animals", "name": "Pets & Animals"},
    "17": {"slug": "sports", "name": "Sports"},
    "19": {"slug": "travel-events", "name": "Travel & Events"},
    "20": {"slug": "gaming", "name": "Gaming"},
    "21": {"slug": "vlogs", "name": "Vlogs"},
    "22": {"slug": "vlogs", "name": "People & Blogs"},
    "23": {"slug": "comedy", "name": "Comedy"},
    "24": {"slug": "entertainment", "name": "Entertainment"},
    "25": {"slug": "news", "name": "News & Politics"},
    "26": {"slug": "howto", "name": "Howto & Style"},
    "27": {"slug": "education", "name": "Education"},
    "28": {"slug": "science-tech", "name": "Science & Technology"},
    "29": {"slug": "nonprofits", "name": "Nonprofits & Activism"},
    "30": {"slug": "movies", "name": "Movies"},
    "31": {"slug": "animation", "name": "Animation"},
    "41": {"slug": "shortform-videos", "name": "Shorts"},
}

# Generic fallback so every YouTube video maps to *some* CreekTube category.
YOUTUBE_CATEGORY_FALLBACK = {"slug": "entertainment", "name": "Entertainment"}


def youtube_category_for(category_id):
    """Map a YouTube ``categoryId`` to a CreekTube ``{slug, name}`` dict.

    Unknown/missing IDs fall back to the generic entertainment category so a
    watch always produces a usable interest signal. Never raises.
    """
    try:
        return dict(YOUTUBE_CATEGORY_MAP.get(str(category_id or "").strip(), YOUTUBE_CATEGORY_FALLBACK))
    except Exception:
        return dict(YOUTUBE_CATEGORY_FALLBACK)

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


def youtube_duration_seconds(iso8601_duration):
    """Convert a YouTube ``contentDetails.duration`` (ISO 8601) to seconds.

    Supports ``PT#H#M#S``-style durations including decimal seconds, plus a
    plain ``None``/empty fallback of 0 (unknown). Never raises.
    """
    if not iso8601_duration:
        return 0
    match = re.fullmatch(
        r"P(?:(\d+(?:\.\d+)?)D)?(?:T(?:(\d+(?:\.\d+)?)H)?(?:(\d+(?:\.\d+)?)M)?(?:(\d+(?:\.\d+)?)S)?)?",
        str(iso8601_duration).strip(),
    )
    if not match:
        return 0
    days, hours, minutes, seconds = match.groups()
    total = 0.0
    if days:
        total += float(days) * 86400
    if hours:
        total += float(hours) * 3600
    if minutes:
        total += float(minutes) * 60
    if seconds:
        total += float(seconds)
    return int(round(total))


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
FEED_MAX_RESULTS_PER_QUERY = 20
FEED_TOTAL_ITEMS = 40
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


def _feed_queries_for(user, interest_categories=None, feed="following", history_keywords=None):
    """Build the list of YouTube search queries for a feed request.

    Logged-in users get queries from their strongest interests (category
    slugs the profile has actually engaged with). Recent ``history_keywords``
    are folded in for discovery. Everyone else gets the default topic pool.
    """
    queries = []
    if interest_categories:
        ranked_cats = sorted(
            ((slug, score) for slug, score in interest_categories.items() if score > 0),
            key=lambda pair: pair[1],
            reverse=True,
        )
        queries = [_category_to_query(slug) for slug, _ in ranked_cats[:4]]
    for keyword in (history_keywords or [])[:3]:
        keyword = (keyword or "").strip()
        if keyword and keyword not in queries:
            queries.append(keyword)
    if feed == "following":
        # Follow feeds still benefit from a little discovery outside the pool.
        queries = (queries or []) + ["popular right now"]
    return [q for q in (queries or DEFAULT_FEED_QUERIES) if q]


def youtube_search_results(query, max_results=FEED_MAX_RESULTS_PER_QUERY, extra_params=None):
    """Search YouTube for ``query`` and return raw API items (cached).

    ``extra_params`` are merged into the search request (e.g.
    ``{"videoDuration": "short"}`` to only surface YouTube Shorts).
    """
    query = (query or "").strip()
    if not query or not _api_key():
        return []
    cache_key = ("search", query, max_results, tuple(sorted((extra_params or {}).items())))
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    params = {
        "part": "snippet",
        "type": "video",
        "q": query,
        "maxResults": max_results,
        "safeSearch": "moderate",
    }
    if extra_params:
        params.update(extra_params)
    data = _youtube_request("search", params)
    items = (data or {}).get("items") or []
    _cache_set(cache_key, items)
    return items


def youtube_search_videos(query, limit=10):
    """Search YouTube videos and return feed-ready dicts (with stats+durations).

    Used by the search endpoint so YouTube results render exactly like native
    feed items. Returns an empty list when the key is missing or calls fail.
    """
    items = []
    seen = set()
    for raw in youtube_search_results(query, max_results=limit + 2):
        item = youtube_feed_item(raw)
        if not item or item["id"] in seen:
            continue
        seen.add(item["id"])
        items.append(item)
        if len(items) >= limit:
            break
    if items:
        ids = [item["id"] for item in items]
        counts = _view_counts_for(ids)
        durations = _durations_for(ids)
        for item in items:
            item["view_count"] = counts.get(item["id"], 0)
            item["duration"] = durations.get(item["id"], 0)
            item["content_type"] = classify_content_type(item["duration"])
        _attach_youtube_enrichment(items)
    return items


YOUTUBE_CHANNEL_ID_RE = re.compile(r"UC[A-Za-z0-9_-]{22}")


def validate_youtube_channel_id(value):
    """Return True if ``value`` is a plausible 22-character channel ID."""
    return bool(value) and bool(YOUTUBE_CHANNEL_ID_RE.fullmatch(value))


def _channel_item(raw):
    """Shape a raw YouTube channel search item into a channel dict."""
    snippet = raw.get("snippet") or {}
    channel_id = (snippet.get("channelId") or "").strip()
    if not validate_youtube_channel_id(channel_id):
        return None
    thumbnails = snippet.get("thumbnails") or {}
    thumbnail = (thumbnails.get("default") or {}).get("url") or ""
    subscribers = None
    try:
        stats = raw.get("statistics") or {}
        if stats.get("subscriberCount"):
            subscribers = int(stats["subscriberCount"])
    except (TypeError, ValueError):
        subscribers = None
    return {
        "channel_id": channel_id,
        "channel_name": (snippet.get("title") or "YouTube channel").strip(),
        "channel_handle": (snippet.get("customUrl") or "").strip().lstrip("@"),
        "channel_thumbnail": thumbnail,
        "channel_description": (snippet.get("description") or "").strip(),
        "subscriber_count": subscribers,
        "video_count": None,
    }


def youtube_search_channels(query, limit=12):
    """Search YouTube channels by name/handle (cached, never raises)."""
    query = (query or "").strip()
    if not query or not _api_key():
        return []
    cache_key = ("channels", query, limit)
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    data = _youtube_request("search", {
        "part": "snippet",
        "type": "channel",
        "q": query,
        "maxResults": limit,
        "safeSearch": "moderate",
    })
    items = (data or {}).get("items") or []
    result = []
    for raw in items:
        item = _channel_item(raw)
        if item:
            result.append(item)
        if len(result) >= limit:
            break
    if result:
        _cache_set(cache_key, result)
    return result


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


def _durations_for(video_ids):
    """Fetch duration seconds for up to 50 video IDs in a single call.

    Returns ``{video_id: seconds}``. Unknown/missing durations default to 0
    so ``classify_content_type`` treats them as plain videos (never snips).
    """
    video_ids = [vid for vid in video_ids if validate_youtube_id(vid)]
    if not video_ids or not _api_key():
        return {}
    cache_key = ("durations", ",".join(sorted(video_ids)))
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    data = _youtube_request("videos", {
        "part": "contentDetails",
        "id": ",".join(video_ids[:50]),
    })
    durations = {}
    for item in (data or {}).get("items") or []:
        details = item.get("contentDetails") or {}
        durations[item["id"]] = youtube_duration_seconds(details.get("duration"))
    _cache_set(cache_key, durations)
    return durations


def youtube_like_counts_for(video_ids):
    """Fetch YouTube like counts for up to 50 video IDs in a single call.

    Returns ``{video_id: like_count}``. Unknown/missing counts default to 0.
    """
    video_ids = [vid for vid in video_ids if validate_youtube_id(vid)]
    if not video_ids or not _api_key():
        return {}
    cache_key = ("likes", ",".join(sorted(video_ids)))
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
        try:
            counts[item["id"]] = int(stats.get("likeCount") or 0)
        except (TypeError, ValueError):
            counts[item["id"]] = 0
    _cache_set(cache_key, counts)
    return counts


def youtube_channel_avatars_for(channel_ids):
    """Fetch channel avatar URLs for up to 50 channel IDs (cached).

    Returns ``{channel_id: thumbnail_url}``. Missing avatars are skipped.
    """
    channel_ids = [cid for cid in channel_ids if validate_youtube_channel_id(cid)]
    if not channel_ids or not _api_key():
        return {}
    cache_key = ("chanavatars", ",".join(sorted(channel_ids)))
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    data = _youtube_request("channels", {
        "part": "snippet",
        "id": ",".join(channel_ids[:50]),
    })
    result = {}
    for item in (data or {}).get("items") or []:
        snippet = item.get("snippet") or {}
        cid = (item.get("id") or "").strip()
        if not cid:
            continue
        thumbnails = snippet.get("thumbnails") or {}
        url = (
            (thumbnails.get("medium") or {}).get("url")
            or (thumbnails.get("default") or {}).get("url")
            or ""
        )
        if url:
            result[cid] = url
    if result:
        _cache_set(cache_key, result)
    return result


def _attach_youtube_enrichment(items):
    """Attach like counts and channel avatars to feed-ready item dicts."""
    if not items:
        return items
    ids = [item.get("id") for item in items]
    likes = youtube_like_counts_for(ids)
    avatars = youtube_channel_avatars_for(
        [item.get("youtube_channel_id") for item in items]
    )
    for item in items:
        vid = item.get("id")
        if vid:
            item["like_count"] = likes.get(vid, 0)
        channel_id = item.get("youtube_channel_id")
        if channel_id and avatars.get(channel_id):
            item["author_avatar"] = avatars[channel_id]
    return items


def youtube_feed_item(item, category_slug="", category_name="", view_count=None, duration=None):
    """Shape a raw YouTube search API item into a feed-ready video dict.

    The shape mirrors what ``VideoSerializer`` already returns so the
    frontend (VideoCard + WatchVideo) can render YouTube and CreekTube
    content uniformly. ``duration`` (seconds) is set when known; otherwise
    the item is left as a plain VIDEO.
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
        "like_count": 0,
        "source_type": "YOUTUBE",
        "content_type": classify_content_type(duration),
        "duration": duration or 0,
        "youtube_video_id": video_id,
        "youtube_channel_id": (snippet.get("channelId") or "").strip(),
        "youtube_channel_name": (snippet.get("channelTitle") or "").strip(),
        "embed_url": youtube_embed_url(video_id),
    }


def build_youtube_feed(user=None, interest_categories=None, limit=FEED_TOTAL_ITEMS, feed=None, history_keywords=None):
    """Build a list of live YouTube feed items.

    ``user`` may be None (guest). Interests come from the user's MediaProfile
    categories (``{category_slug: score}``). ``history_keywords`` (a list of
    recent search/history terms) are mixed in for discovery. Returns an empty
    list whenever the API key is missing or the calls fail, so the native
    feed is never harmed.
    """
    if not _api_key() or limit <= 0:
        return []

    queries = _feed_queries_for(user, interest_categories, feed=feed, history_keywords=history_keywords)
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
        durations = _durations_for([item["id"] for item in items])
        for item in items:
            item["view_count"] = counts.get(item["id"], 0)
            item["duration"] = durations.get(item["id"], 0)
            item["content_type"] = classify_content_type(item["duration"])
        _attach_youtube_enrichment(items)
        # YouTube Shorts belong in the Snips feed, not the main video feed.
        items = [item for item in items if item.get("content_type") != SNIP]
    return items


def build_youtube_snips_feed(user=None, interest_categories=None, limit=FEED_TOTAL_ITEMS, feed=None, history_keywords=None):
    """Build a list of live YouTube Shorts for the Snips feed.

    Same shape as ``build_youtube_feed`` but the search restricts to short
    video durations, so only short-form YouTube content is mixed into the
    Snips feed. Durations are still double-checked so an item is only ever
    labelled ``SNIP`` when it is actually under the Snip threshold.
    """
    if not _api_key() or limit <= 0:
        return []

    queries = _feed_queries_for(user, interest_categories, feed=feed, history_keywords=history_keywords)
    items = []
    seen_ids = set()
    for query in queries:
        for raw in youtube_search_results(query, extra_params={"videoDuration": "short"}):
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
        durations = _durations_for([item["id"] for item in items])
        for item in items:
            item["view_count"] = counts.get(item["id"], 0)
            item["duration"] = durations.get(item["id"], 0)
            item["content_type"] = classify_content_type(item["duration"])
        _attach_youtube_enrichment(items)
    return items


def get_youtube_video_details(video_id):
    """Fetch one video's snippet + statistics + contentDetails.

    Returns a feed-ready dict (including ``duration`` and ``content_type``)
    so a YouTube ID that isn't stored as a CreekTube row can still be played
    through the iframe embed.
    """
    if not validate_youtube_id(video_id) or not _api_key():
        return None
    cache_key = ("video", video_id)
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    data = _youtube_request("videos", {"part": "snippet,statistics,contentDetails", "id": video_id})
    items = (data or {}).get("items") or []
    if not items:
        return None
    snippet = items[0].get("snippet") or {}
    stats = items[0].get("statistics") or {}
    details = items[0].get("contentDetails") or {}
    try:
        view_count = int(stats.get("viewCount") or 0)
    except (TypeError, ValueError):
        view_count = 0
    try:
        like_count = int(stats.get("likeCount") or 0)
    except (TypeError, ValueError):
        like_count = 0
    duration = youtube_duration_seconds(details.get("duration"))
    category = youtube_category_for(snippet.get("categoryId"))
    item = {
        "id": video_id,
        "snippet": snippet,
    }
    shaped = youtube_feed_item(
        item,
        category_slug=category["slug"],
        category_name=category["name"],
        view_count=view_count,
        duration=duration,
    )
    if shaped:
        shaped["content_type"] = classify_content_type(duration)
        shaped["category"] = category["slug"]
        shaped["category_name"] = category["name"]
        shaped["like_count"] = like_count
        _attach_youtube_enrichment([shaped])
        _cache_set(cache_key, shaped)
    return shaped


def youtube_related_videos(video_id, limit=12):
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
    _attach_youtube_enrichment(related)
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
        category = youtube_category_for(snippet.get("categoryId"))
        return {
            "title": (snippet.get("title") or "").strip(),
            "description": (snippet.get("description") or "").strip(),
            "thumbnail": youtube_thumbnail_url(video_id),
            "channel_id": (snippet.get("channelId") or "").strip(),
            "channel_name": (snippet.get("channelTitle") or "").strip(),
            "category_id": (snippet.get("categoryId") or "").strip(),
            "category": category["slug"],
            "category_name": category["name"],
        }
    except Exception as exc:
        logger.warning("YouTube metadata fetch failed for %s: %s", video_id, exc)
        return None


def _youtube_comment_dict(item, parent=None):
    """Shape a single commentThread/comment resource into a comment dict."""
    snippet = item.get("snippet") or {}
    text = (snippet.get("textDisplay") or "").strip()
    if not text:
        return None
    author = snippet.get("authorDisplayName") or ""
    author_channel_id = (snippet.get("authorChannelId") or {}).get("value") or ""
    avatars = youtube_channel_avatars_for([author_channel_id]) if author_channel_id else {}
    author_avatar = (
        (snippet.get("authorProfileImageUrl") or "").strip()
        or avatars.get(author_channel_id, "")
    )
    try:
        like_count = int(snippet.get("likeCount") or 0)
    except (TypeError, ValueError):
        like_count = 0
    result = {
        "id": str(item.get("id") or ""),
        "text": text,
        "author": author or "Anonymous",
        "author_avatar": author_avatar,
        "author_id": None,
        "user_id": None,
        "timestamp": _youtube_comment_time(snippet.get("publishedAt")),
        "edited": False,
        "parent": parent,
        "likes_count": like_count,
        "is_liked": False,
        "is_pinned": bool(snippet.get("isPinned")),
        "source": "youtube",
        "read_only": True,
        "replies": [],
    }
    return result


def _youtube_comment_time(value):
    """Parse an ISO-8601 YouTube timestamp into an epoch millisecond value."""
    if not value:
        return 0
    try:
        from datetime import datetime, timezone

        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp() * 1000)
    except Exception:
        return 0


def youtube_comments(video_id, max_results=20):
    """Fetch top YouTube comments (with replies) for a video.

    Returns a flat list of comment dicts shaped like CreekTube comments but
    flagged ``source="youtube"`` / ``read_only=True`` so the frontend knows
    they are read-only. Threads are flattened with top-level comments first,
    each followed by its replies (``parent`` = top-level id).
    """
    if not validate_youtube_id(video_id) or not _api_key():
        return []
    cache_key = ("comments", video_id)
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    data = _youtube_request("commentThreads", {
        "part": "snippet,replies",
        "videoId": video_id,
        "maxResults": min(max_results, 100),
        "order": "relevance",
        "textFormat": "plainText",
    })
    comments = []
    for item in (data or {}).get("items") or []:
        thread = item.get("snippet") or {}
        # The actual top-level comment lives inside ``topLevelComment``; the
        # thread snippet itself has no ``textDisplay``.
        top_level = thread.get("topLevelComment") or {}
        top = _youtube_comment_dict(top_level, parent=None)
        if not top:
            continue
        top_id = top["id"]
        replies = []
        for reply in (item.get("replies") or {}).get("comments") or []:
            shaped = _youtube_comment_dict(reply, parent=top_id)
            if shaped:
                replies.append(shaped)
        top["replies"] = replies
        comments.append(top)
    if comments:
        _cache_set(cache_key, comments, ttl=300)
    return comments


def youtube_channel(channel_id):
    """Fetch YouTube channel details (snippet + statistics + branding).

    Returns a dict with ``channel_id``, ``channel_name``, ``channel_handle``,
    ``channel_thumbnail``, ``channel_banner``, ``channel_description``,
    ``subscriber_count``, ``video_count``, ``view_count``, ``country`` and
    ``published_at``, or ``None``.
    """
    if not validate_youtube_channel_id(channel_id) or not _api_key():
        return None
    cache_key = ("channel", channel_id)
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    data = _youtube_request("channels", {
        "part": "snippet,statistics,brandingSettings",
        "id": channel_id,
    })
    items = (data or {}).get("items") or []
    if not items:
        return None
    snippet = items[0].get("snippet") or {}
    stats = items[0].get("statistics") or {}
    branding = items[0].get("brandingSettings") or {}
    image = branding.get("image") or {}
    thumbnails = snippet.get("thumbnails") or {}
    def _safe_int(value):
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0
    handle = (snippet.get("customUrl") or "").strip()
    if handle and not handle.startswith("@"):
        handle = "@" + handle
    result = {
        "channel_id": channel_id,
        "channel_name": (snippet.get("title") or "").strip(),
        "channel_handle": handle,
        "channel_thumbnail": (
            (thumbnails.get("high") or {}).get("url")
            or (thumbnails.get("medium") or {}).get("url")
            or (thumbnails.get("default") or {}).get("url")
            or ""
        ),
        "channel_banner": (image.get("bannerImageUrl") or "").strip(),
        "channel_description": (snippet.get("description") or "").strip(),
        "subscriber_count": _safe_int(stats.get("subscriberCount")),
        "video_count": _safe_int(stats.get("videoCount")),
        "view_count": _safe_int(stats.get("viewCount")),
        "country": (snippet.get("country") or "").strip(),
        "published_at": _youtube_comment_time(snippet.get("publishedAt")),
        "source_type": "YOUTUBE",
    }
    _cache_set(cache_key, result, ttl=900)
    return result


def youtube_channel_videos(channel_id, limit=24, duration="any"):
    """List a YouTube channel's recent uploads as feed-ready item dicts.

    ``duration`` mirrors the Data API ``videoDuration`` filter ("any",
    "short", "long", "medium") so a channel's Snips can be listed separately
    from its regular uploads.
    """
    if duration not in ("any", "short", "long", "medium"):
        duration = "any"
    if not validate_youtube_channel_id(channel_id) or not _api_key():
        return []
    cache_key = ("chanvideos", channel_id, limit, duration)
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    params = {
        "part": "snippet",
        "channelId": channel_id,
        "type": "video",
        "order": "date",
        "maxResults": min(limit + 4, 50),
    }
    if duration != "any":
        params["videoDuration"] = duration
    data = _youtube_request("search", params)
    items = []
    for raw in (data or {}).get("items") or []:
        item = youtube_feed_item(raw)
        if item:
            items.append(item)
        if len(items) >= limit:
            break
    if items:
        counts = _view_counts_for([item["id"] for item in items])
        durations = _durations_for([item["id"] for item in items])
        for item in items:
            item["view_count"] = counts.get(item["id"], 0)
            item["duration"] = durations.get(item["id"], 0)
            item["content_type"] = classify_content_type(item["duration"])
        _attach_youtube_enrichment(items)
        _cache_set(cache_key, items, ttl=600)
    return items
