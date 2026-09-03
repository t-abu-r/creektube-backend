from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from accounts.models import Profile
from rest_framework.response import Response
from rest_framework.status import HTTP_400_BAD_REQUEST
from rest_framework.views import APIView
from django.utils import timezone
from .permissions import IsModerator, IsSuperUser
from .Serializers import *
from accounts.serializers import ProfileSerializer
from .models import (Video, Comment, CommentLike, CategoryVideo, MediaProfile, Like,
                     DisPike, Creek, WatchEvent, UploadRateLimit, Notification, Snip, SnipLike,
                     SnipDislike, SnipSave, SnipFeedback,
                     ModActionLog, UserTitle, YouTubeChannelFollow, Tag)
from . import snips_rank
from django.db.models import Count, Q, Sum, Avg, F, Max
from django.db import IntegrityError
from . import ranking
from .youtube import (normalize_youtube_url, get_video_metadata, youtube_thumbnail_url,
                      validate_youtube_id, build_youtube_feed, get_youtube_video_details,
                      youtube_related_videos, youtube_search_channels,
                      youtube_duration_seconds, youtube_search_videos,
                      youtube_comments, youtube_like_counts_for, youtube_channel,
                      youtube_channel_videos, validate_youtube_channel_id,
                      build_youtube_snips_feed, youtube_embed_url, youtube_system_user,
                      youtube_search_results, youtube_feed_item, _attach_youtube_enrichment,
                      youtube_api_status, YOUTUBE_SYSTEM_USERNAME)
from .content import classify_content_type
from .tags import apply_tags, extract_hashtags, tag_names_for
import logging
import re
from django.db.models.functions import TruncDate
logger = logging.getLogger(__name__)
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser
from datetime import timedelta


# ---------------------------
# Spam Prevention Helpers
# ---------------------------
UPLOAD_RATE_LIMIT = 3  # max uploads per hour
UPLOAD_RATE_WINDOW = timedelta(hours=1)
VIEW_GAP = timedelta(minutes=2)  # min time between views from same user on same video
VIEW_MAX_PER_USER = 6  # max views per user per video/snip
COMMENT_SPAM_LIMIT = 6  # max comments per video
COMMENT_SPAM_WINDOW = timedelta(minutes=2)

# ---------------------------------------------------------------------------
# Admin titles: the permission catalog that titles can carry.
# ---------------------------------------------------------------------------
ADMIN_PERMISSIONS = [
    {"code": "mod.approve", "label": "Approve / reject videos & snips"},
    {"code": "mod.deactivate", "label": "Deactivate / reactivate accounts"},
    {"code": "mod.view_logs", "label": "View moderation logs"},
    {"code": "official.badge", "label": "Official account badge"},
]


def _sync_legacy_title_flags(profile):
    """Keep the legacy ``moderator``/``official`` booleans in sync with titles."""
    perms = profile.granted_permissions()
    profile.moderator = bool(perms & {"mod.approve", "mod.deactivate", "mod.view_logs"})
    profile.official = "official.badge" in perms
    profile.save(update_fields=["moderator", "official"])


def _title_payload(title):
    return {
        "id": title.pk,
        "name": title.name,
        "color": title.color,
        "symbol": title.symbol,
        "permissions": title.permissions or [],
        "holder_count": title.holders.count(),
    }


def check_upload_rate_limit(user):
    """Returns True if user can upload (under rate limit), False if blocked."""
    cutoff = timezone.now() - UPLOAD_RATE_WINDOW
    recent_uploads = UploadRateLimit.objects.filter(
        user=user, uploaded_at__gte=cutoff
    ).count()
    return recent_uploads < UPLOAD_RATE_LIMIT


def record_upload(user):
    """Record an upload event for rate limiting."""
    UploadRateLimit.objects.create(user=user)


def record_view(user, video=None, snip=None):
    """
    Create a watch event + update view_count atomically.
    Anti-spam: 2min gap between views, max 6 per user per item.
    Returns the created WatchEvent or None if blocked.
    """
    if not user or not user.is_authenticated:
        return None

    if video:
        events = WatchEvent.objects.filter(user=user, video=video)
    elif snip:
        events = WatchEvent.objects.filter(user=user, snip=snip)
    else:
        return None

    now = timezone.now()

    # Time gap check
    last = events.order_by('-timestamp').first()
    if last and (now - last.timestamp) < VIEW_GAP:
        return None

    # Max views check — delete oldest if exceeded
    if events.count() >= VIEW_MAX_PER_USER:
        events.order_by('timestamp').first().delete()

    # Group events into ~30-minute session buckets so co-watch/session
    # signals can be derived from shared session ids.
    epoch_minutes = int(now.timestamp() // 1800)
    session_id = f"{user.id}:{epoch_minutes}"
    event = WatchEvent.objects.create(user=user, video=video, snip=snip, session_id=session_id)

    # Recompute unique viewers for the denormalized counter
    if video:
        unique = WatchEvent.objects.filter(video=video).values('user').distinct().count()
        Video.objects.filter(pk=video.pk).update(view_count=unique)
    elif snip:
        unique = WatchEvent.objects.filter(snip=snip).values('user').distinct().count()
        Snip.objects.filter(pk=snip.pk).update(view_count=unique)

    return event


def check_comment_spam(user, video):
    """Returns True if user is under limit, False if spam detected."""
    cutoff = timezone.now() - COMMENT_SPAM_WINDOW
    recent = Comment.objects.filter(
        author=user, video=video, timestamp__gte=cutoff
    ).count()
    return recent < COMMENT_SPAM_LIMIT


def ensure_category(slug, name=""):
    """Return the CategoryVideo row for ``slug``, creating it if needed.

    Safe under concurrent requests (serverless runs many Lambda workers): it
    falls back to an existing row with the same display name, and re-fetches
    when a racing create loses the unique constraint.
    """
    if not slug:
        return None
    name = (name or slug.replace("-", " ").title()).strip()
    category = CategoryVideo.objects.filter(slug=slug).first()
    if category:
        return category
    # A different slug may already carry this display name (e.g. the API
    # reporting "News & Politics" under several slugs at once).
    category = CategoryVideo.objects.filter(name__iexact=name).first()
    if category:
        return category
    try:
        return CategoryVideo.objects.create(slug=slug, name=name)
    except IntegrityError:
        # A concurrent request just created it; fetch whichever row won.
        return (
            CategoryVideo.objects.filter(slug=slug).first()
            or CategoryVideo.objects.filter(name__iexact=name).first()
        )


def record_category_interest(user, category_slug, boost=ranking.WATCH_BOOST):
    """Boost a user's interest score for ``category_slug`` after a watch.

    This is what makes the home feed adapt: ``profile.categories`` drives both
    the native ranking and the YouTube query pool, so watching content in a
    category surfaces more of that category on the homepage.
    """
    if not user or not user.is_authenticated or not category_slug:
        return
    profile, _ = MediaProfile.objects.get_or_create(user=user)
    profile.categories = ranking.adjust_category_score(
        profile.categories, category_slug, boost
    )
    profile.save()


def record_tag_interest(user, tag_names, boost=ranking.TAG_BOOST):
    """Boost a user's tag-interest scores for ``tag_names`` after a watch.

    Tag interests outrank category interests in the feed, so a Music viewer
    who watches a specific artist (#theneighbourhood) keeps seeing that artist
    on top of the generic Music bucket.
    """
    if not user or not user.is_authenticated:
        return
    names = [n for n in (tag_names or []) if n]
    if not names:
        return
    profile, _ = MediaProfile.objects.get_or_create(user=user)
    for name in names:
        profile.tags = ranking.adjust_tag_score(profile.tags, name, boost)
    profile.save()


def record_tag_interest_from_watch(user, video=None, snip=None):
    """Learn tag interests from a just-watched video/snip."""
    if video is not None:
        record_tag_interest(user, tag_names_for(video))
    elif snip is not None:
        record_tag_interest(user, tag_names_for(snip))


def boost_youtube_watch_interest(user, video):
    """Learn the YouTube category for a stored but uncategorized YouTube row."""
    if not video or video.source_type != "YOUTUBE" or not video.youtube_video_id:
        return
    yt_item = get_youtube_video_details(video.youtube_video_id) or {}
    if not yt_item.get("category"):
        return
    category = ensure_category(yt_item["category"], yt_item.get("category_name") or "")
    if category:
        Video.objects.filter(pk=video.pk).update(category=category)
        video.category = category
        record_category_interest(user, category.slug)


def ensure_youtube_video(video_id, user=None):
    """Get-or-create a lightweight stored Video row for a live YouTube ID.

    CreekTube likes/comments are stored against a Video row, so a YouTube
    video that isn't already part of CreekTube is materialized as a public,
    pre-approved YOUTUBE row the first time a user interacts with it. The
    video itself is never downloaded: only metadata + the ID are persisted.
    """
    if not validate_youtube_id(video_id):
        return None
    video = Video.objects.filter(youtube_video_id=video_id).first()
    if video:
        return video
    metadata = get_video_metadata(video_id) or {}
    details = get_youtube_video_details(video_id) or {}
    category = None
    if metadata.get("category"):
        category = ensure_category(
            metadata["category"],
            metadata.get("category_name") or "",
        )
    published = _epoch(details.get("timestamp")) or None
    if published:
        from datetime import datetime, timezone as dt_timezone
        timestamp = datetime.fromtimestamp(published, tz=dt_timezone.utc)
    else:
        timestamp = timezone.now()
    try:
        video = Video.objects.create(
            author=youtube_system_user(),
            category=category,
            title=metadata.get("title") or details.get("title") or "YouTube video",
            description=metadata.get("description") or "",
            thumbnail=youtube_thumbnail_url(video_id),
            video="",
            source_type="YOUTUBE",
            youtube_video_id=video_id,
            youtube_channel_id=metadata.get("channel_id") or details.get("youtube_channel_id") or "",
            youtube_channel_name=metadata.get("channel_name") or details.get("author") or "",
            visibility="public",
            timestamp=timestamp,
            is_approved=True,
            duration=details.get("duration") or 0,
            content_type=classify_content_type(details.get("duration") or 0),
        )
    except Exception as exc:
        logger.warning("ensure_youtube_video failed for %s: %s", video_id, exc)
        return None
    apply_tags(video, video.title, video.description)
    return video


def youtube_snip_like_state(request, video_id):
    """Return ``(is_liked, like_count, youtube_like_count, creek_like_count)``.

    CreekTube likes on YouTube Shorts are stored against the lightweight
    YOUTUBE Video row (via the ``Like`` model) so they persist, while the
    heart count always mirrors the real YouTube like count.
    """
    user = request.user if getattr(request.user, "is_authenticated", False) else None
    video = ensure_youtube_video(video_id, user)
    if not video:
        return False, 0, 0, 0
    creek_like_count = Like.objects.filter(video=video).count()
    yt_like_count = youtube_like_counts_for([video_id]).get(video_id, 0)
    is_liked = (
        user is not None
        and Like.objects.filter(video=video, author=request.user).exists()
    )
    return is_liked, yt_like_count + creek_like_count, yt_like_count, creek_like_count


# ---------------------------
# Hybrid feed mixing (CreekTube + YouTube)
# ---------------------------
HYBRID_YOUTUBE_RATIO = 4  # 1 YouTube item every 4 slots
HYBRID_YOUTUBE_DEFAULT_FRACTION = 0.25
HYBRID_YOUTUBE_MIN_FRACTION = 0.16
HYBRID_YOUTUBE_MAX_RATIO = 6


def recent_type_mix(user, window=40):
    """Fraction (0..1) of a user's recent watches that were YouTube.

    Drives the adaptive blend: the more of one type the user watches, the
    more of it appears on top — while the interleaver still guarantees the
    other type never disappears entirely.
    """
    if not user or not user.is_authenticated:
        return HYBRID_YOUTUBE_DEFAULT_FRACTION
    recent = list(
        WatchEvent.objects.filter(user=user, video__isnull=False)
        .select_related("video")
        .order_by("-timestamp")[:window]
    )
    if not recent:
        return HYBRID_YOUTUBE_DEFAULT_FRACTION
    youtube = sum(1 for event in recent if event.video.source_type == "YOUTUBE")
    fraction = youtube / len(recent)
    # Never fully exclude either type from the feed.
    return min(0.6, max(HYBRID_YOUTUBE_MIN_FRACTION, fraction))


def adaptive_youtube_ratio(user):
    """Native slots per YouTube slot, adapted to the user's watch mix."""
    fraction = recent_type_mix(user)
    if fraction <= 0:
        return HYBRID_YOUTUBE_RATIO
    ratio = int(round(1 / fraction))
    return max(1, min(HYBRID_YOUTUBE_MAX_RATIO, ratio))


def build_youtube_follows_feed(user, limit=20, shorts=False):
    """Live YouTube videos from channels the user creeks (read-only).

    Only works when the YouTube API key is configured; otherwise returns an
    empty list so the following feed keeps working with Creektube creators.
    """
    if not user or not user.is_authenticated:
        return []
    follows = list(YouTubeChannelFollow.objects.filter(user=user)[:20])
    if not follows:
        return []
    items = []
    seen = set()
    for follow in follows:
        for item in youtube_channel_videos(follow.channel_id, limit=5, duration="short" if shorts else "any"):
            if item["id"] in seen:
                continue
            seen.add(item["id"])
            items.append(item)
        if len(items) >= limit:
            break
    return items[:limit]


def interleave_feed(native_items, youtube_items, ratio=HYBRID_YOUTUBE_RATIO):
    """Blend native and live-YouTube items into a single ordered list.

    Every ``ratio`` positions a YouTube item is inserted between native
    items, so the mixed feed reads naturally and neither source type can
    disappear. Either input may be empty.
    """
    combined = []
    ct_idx = 0
    yt_idx = 0
    while ct_idx < len(native_items) or yt_idx < len(youtube_items):
        for _ in range(ratio):
            if ct_idx < len(native_items):
                combined.append(native_items[ct_idx])
                ct_idx += 1
        if yt_idx < len(youtube_items):
            combined.append(youtube_items[yt_idx])
            yt_idx += 1
    return combined


def resolve_video_by_id(video_id, queryset):
    """Resolve a video by primary key or stored YouTube ID.

    Returns ``None`` when the row doesn't exist. Non-numeric IDs (like live
    YouTube IDs that aren't stored) never raise a ValueError.
    """
    video = None
    if validate_youtube_id(video_id):
        video = queryset.filter(youtube_video_id=video_id).first()
    if video is None:
        try:
            video = queryset.filter(pk=video_id).first()
        except (ValueError, TypeError):
            return None
    return video


def related_youtube_for(native_video, limit=6):
    """Live YouTube videos related to a native CreekTube video.

    Queries by the video's hashtags first (strongest signal), then its
    category, so CreekTube and YouTube content cross-pollinate. A single
    query is used so the 100-unit search endpoint is never wasted on a low
    yield page.
    """
    if native_video is None:
        return []
    tags = [t.name for t in native_video.tags.all()[:3]]
    queries = tags[:1]
    if not queries and native_video.category:
        queries.append(native_video.category.slug.replace("-", " "))
    items = []
    seen = set()
    for query in queries:
        for raw in youtube_search_results(query, max_results=limit + 2):
            item = youtube_feed_item(raw)
            if not item or item["id"] in seen:
                continue
            seen.add(item["id"])
            items.append(item)
        if len(items) >= limit:
            break
    if items:
        _attach_youtube_enrichment(items)
    return items[:limit]


def related_native_for(video, category_hint=None, limit=6):
    """Native CreekTube videos related to a (possibly live) YouTube video.

    Matches the materialized row's tags first, then category; ``category_hint``
    is used for live YouTube videos that have no stored row yet.
    """
    qs = (
        Video.objects.filter(is_approved=True, visibility="public", author__is_active=True)
        .exclude(source_type="YOUTUBE", author__username=YOUTUBE_SYSTEM_USERNAME)
    )
    tag_ids = [t.id for t in video.tags.all()[:3]] if video is not None else []
    if tag_ids:
        matched = list(
            qs.filter(tags__in=tag_ids)
            .exclude(id=video.id)
            .distinct()
            .order_by("-timestamp")[:limit]
        )
        if matched:
            return matched
    category_slug = (
        video.category.slug if video is not None and video.category else category_hint
    )
    if category_slug:
        return list(
            qs.filter(category__slug=category_slug)
            .exclude(id=video.id if video is not None else -1)
            .order_by("-timestamp")[:limit]
        )
    return []


def mixed_related_videos(native_items, youtube_items, request):
    """Serialize a mixed related list (Video objects + YouTube dicts)."""
    objects = [item for item in native_items if isinstance(item, Video)]
    object_data = {}
    if objects:
        serializer = VideoSerializer(objects, many=True, context={"request": request})
        object_data = {obj.id: data for obj, data in zip(objects, serializer.data)}
    combined = []
    for item in native_items + youtube_items:
        if isinstance(item, Video):
            data = object_data.get(item.id)
        else:
            data = item
        if data:
            combined.append(data)
    return combined[:12]


def serialize_mixed_items(items, request):
    """Serialize a mixed list of Video objects and pre-shaped dicts."""
    objects = [item for item in items if isinstance(item, Video)]
    object_data = {}
    if objects:
        serializer = VideoSerializer(objects, many=True, context={'request': request})
        object_data = {obj.id: data for obj, data in zip(objects, serializer.data)}
    return [object_data.get(item.id) if isinstance(item, Video) else item for item in items]


def _epoch(value):
    """Parse a serialized timestamp (ISO string, epoch number, or datetime) into epoch seconds."""
    if not value:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    try:
        from datetime import datetime, timezone

        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except Exception:
        return 0.0


_WATCH_QUERY_STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "your", "you", "are", "was",
    "were", "not", "but", "from", "they", "their", "them", "video", "videos",
    "official", "lyrics", "lyric", "music", "audio", "song", "full", "hd",
    "4k", "new", "mix", "remix", "edit", "cover", "live", "album", "single",
    "track", "feat", "ft", "featuring", "part", "version", "visualizer",
    "clip", "movie", "slowed", "reverb", "best", "top", "ultimate",
}


def _watch_query_phrase(title):
    """Reduce a watched title to a useful YouTube discovery phrase.

    Keeps the leading title segment (before artist/suffix separators) and
    drops filler tokens, so "Sweater Weather - The NeighbourHood Lyrics Video"
    yields "sweater weather" instead of the naive bare word "sweater".
    """
    if not title:
        return ""
    lead = re.split(r"\s*[-–—|·]\s*", title)[0]
    words = [
        w for w in lead.split()
        if "".join(ch for ch in w.lower() if ch.isalnum()) not in _WATCH_QUERY_STOPWORDS
    ]
    return " ".join(words).strip().lower()[:64]


def recent_watch_keywords(user, limit=3):
    """Derive up to ``limit`` discovery phrases from a user's recent watches.

    The most recent video/snip titles are turned into full query phrases (not
    single words) so a watch of "Sweater Weather" surfaces the song rather
    than unrelated "sweater" results. Stored YouTube rows contribute their
    channel name, which is usually the strongest discovery signal.
    """
    if not user or not user.is_authenticated:
        return []
    events = (WatchEvent.objects.filter(user=user)
              .select_related("video", "snip")
              .order_by("-timestamp")[:12])
    keywords = []
    for event in events:
        video = event.video
        if video and video.source_type == "YOUTUBE" and video.youtube_channel_name:
            channel = video.youtube_channel_name.strip().lower()
            if channel and channel not in keywords:
                keywords.append(channel)
        title = video.title if video else (event.snip.title if event.snip else "")
        phrase = _watch_query_phrase(title)
        if phrase and phrase not in keywords:
            keywords.append(phrase)
        if len(keywords) >= limit:
            break
    return keywords[:limit]


# ---------------------------
# Set Interests API
# ---------------------------
class SetInterests(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        categories = request.data.get("categories")
        if not categories or not isinstance(categories, list):
            return Response({"detail": "Send a list of category slugs"}, status=status.HTTP_400_BAD_REQUEST)

        valid_slugs = set(
            CategoryVideo.objects.values_list("slug", flat=True)
        )
        for c in categories:
            if c not in valid_slugs:
                return Response({"detail": f"Unknown category: {c}"}, status=status.HTTP_400_BAD_REQUEST)

        profile, _ = MediaProfile.objects.get_or_create(user=request.user)
        existing = profile.categories or {}
        profile.categories = {c: existing.get(c, 10) for c in categories}
        profile.save()

        return Response({"detail": "Interests set successfully", "categories": profile.categories})


# ---------------------------
# Get Videos API (feed)
# ---------------------------
class LoginGetVideo(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        profile, _ = MediaProfile.objects.get_or_create(user=request.user)
        category_param = request.query_params.get('category', '').strip().lower()
        sort = request.query_params.get('sort', '').strip().lower()
        feed = request.query_params.get('feed', '').strip().lower()

        if category_param in ['shortform-videos', 'snips']:
            snips = Snip.objects.filter(is_approved=True, visibility="public", author__is_active=True).select_related('author')
            if feed == 'following':
                creeked_ids = set(
                    Creek.objects.filter(author=request.user)
                    .exclude(account__user__is_active=False)
                    .values_list('account__user_id', flat=True)
                )
                snips = snips.filter(author_id__in=creeked_ids)
            if sort == 'views':
                native_items = list(snips.order_by('-view_count'))
            else:
                native_items = list(snips.order_by('-timestamp'))

            results = [
                {
                    "id": s.id,
                    "title": s.title,
                    "description": s.description,
                    "thumbnail": s.video,
                    "video": s.video,
                    "timestamp": s.timestamp,
                    "is_approved": s.is_approved,
                    "author": s.author.username,
                    "author_id": getattr(getattr(s.author, "mediaprofile", None), "id", s.author.id),
                    "author_avatar": Profile.objects.filter(user=s.author).first().avatar.url if Profile.objects.filter(user=s.author).exists() else None,
                    "category": "shortform-videos",
                    "category_name": "Shortform Videos",
                    "view_count": s.view_count,
                    "is_snip": True,
                    "content_type": "SNIP",
                    "duration": s.duration,
                }
                for s in native_items
            ]

            # Mix in live YouTube Shorts (unless on the "following" feed,
            # where only creators the user actually follows make sense).
            if feed != 'following':
                results += build_youtube_snips_feed(request.user, limit=40)
                if sort == 'views':
                    results.sort(key=lambda item: item.get("view_count") or 0, reverse=True)
                else:
                    results.sort(key=lambda item: _epoch(item.get("timestamp")), reverse=True)

            total = len(results)
            try:
                page = max(int(request.query_params.get('page', 1)), 1)
            except (TypeError, ValueError):
                page = 1
            try:
                page_size = min(max(int(request.query_params.get('page_size', 20)), 1), 100)
            except (TypeError, ValueError):
                page_size = 20

            start = (page - 1) * page_size
            return Response({
                "results": results[start:start + page_size],
                "page": page,
                "page_size": page_size,
                "count": total,
                "youtube_error": youtube_api_status(),
            }, status=200)
        user_interest = profile.categories
        user_tag_interests = profile.tags
        creeked_author_ids = set(
            Creek.objects.filter(author=request.user)
            .exclude(account__user__is_active=False)
            .values_list('account__user_id', flat=True)
        )

        approved_videos = (
            Video.objects.filter(is_approved=True, visibility="public", author__is_active=True)
            # Materialized YouTube rows are bookkeeping for likes/comments; the
            # live YouTube items already carry that content into the feed.
            .exclude(source_type="YOUTUBE", author__username=YOUTUBE_SYSTEM_USERNAME)
            .select_related('category')
            .prefetch_related('tags')
            .annotate(
                num_likes=Count('likes', distinct=True),
                num_dislikes=Count('dispikes', distinct=True),
            )
        )

        if category_param:
            approved_videos = approved_videos.filter(category__slug=category_param)

        if feed == 'following':
            approved_videos = approved_videos.filter(author_id__in=creeked_author_ids)

        CANDIDATE_LIMIT = 500
        if sort == 'views':
            candidates = list(approved_videos.order_by('-view_count')[:CANDIDATE_LIMIT])
        else:
            candidates = list(approved_videos.order_by('-timestamp')[:CANDIDATE_LIMIT])

        # Build co-watch map from user's recent history
        user_recent_ids = ranking.get_user_recent_video_ids(request.user)
        candidate_ids = [v.id for v in candidates]
        cowatch_map = ranking.build_cowatch_map(user_recent_ids, candidate_ids) if user_recent_ids else {}

        # Tag interests outrank categories, so pass the per-video tag map to
        # the ranker (tags are prefetched above, no extra queries).
        video_tag_map = {
            v.id: [t.name for t in v.tags.all()]
            for v in candidates
        }
        ranked = ranking.rank_videos(
            candidates, user_interest, creeked_author_ids,
            cowatch_map=cowatch_map, user_recent_video_ids=user_recent_ids,
            user_tag_interests=user_tag_interests, video_tag_map=video_tag_map,
        )

        try:
            page = max(int(request.query_params.get('page', 1)), 1)
        except (TypeError, ValueError):
            page = 1
        try:
            page_size = min(max(int(request.query_params.get('page_size', 20)), 1), 100)
        except (TypeError, ValueError):
            page_size = 20

        youtube_items = build_youtube_feed(
            user=request.user,
            interest_categories=user_interest,
            feed=feed,
            history_keywords=recent_watch_keywords(request.user),
            tag_queries=list(user_tag_interests.keys()),
        )
        if feed == 'following':
            # Followed YouTube channels surface in the same following feed.
            youtube_items = build_youtube_follows_feed(request.user, limit=20) + youtube_items
        combined = interleave_feed(
            ranked, youtube_items,
            ratio=adaptive_youtube_ratio(request.user),
        )

        start = (page - 1) * page_size
        page_videos = combined[start:start + page_size]
        results = serialize_mixed_items(page_videos, request)
        return Response({
            "results": results,
            "page": page,
            "page_size": page_size,
            "count": len(combined),
            "youtube_error": youtube_api_status(),
        }, status=200)


class GuestGetVideo(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        # Single video by ID (used for SSR/metadata)
        video_id = request.query_params.get('video_id')
        if video_id:
            video = resolve_video_by_id(
                video_id,
                Video.objects.filter(is_approved=True, visibility="public", author__is_active=True).select_related('author'),
            )
            if not video and validate_youtube_id(video_id):
                # A live YouTube ID that isn't stored as a CreekTube row.
                yt_item = get_youtube_video_details(video_id)
                if yt_item:
                    return Response(yt_item, status=200)
            if not video:
                return Response({"detail": "Video not found"}, status=404)
            serializer = VideoSerializer(video, many=False, context={'request': request})
            return Response(serializer.data, status=200)

        category_param = request.query_params.get('category', '').strip().lower()
        sort = request.query_params.get('sort', '').strip().lower()

        if category_param in ['shortform-videos', 'snips']:
            snips = Snip.objects.filter(is_approved=True, visibility="public", author__is_active=True).select_related('author')
            if sort == 'views':
                native_items = list(snips.order_by('-view_count'))
            else:
                native_items = list(snips.order_by('-timestamp'))

            results = [
                {
                    "id": s.id,
                    "title": s.title,
                    "description": s.description,
                    "thumbnail": s.video,
                    "video": s.video,
                    "timestamp": s.timestamp,
                    "is_approved": s.is_approved,
                    "author": s.author.username,
                    "author_id": getattr(getattr(s.author, "mediaprofile", None), "id", s.author.id),
                    "author_avatar": Profile.objects.filter(user=s.author).first().avatar.url if Profile.objects.filter(user=s.author).exists() else None,
                    "category": "shortform-videos",
                    "category_name": "Shortform Videos",
                    "view_count": s.view_count,
                    "is_snip": True,
                    "content_type": "SNIP",
                    "duration": s.duration,
                }
                for s in native_items
            ]

            # Mix in live YouTube Shorts so the homepage Snips tab matches the
            # dedicated /snips feed.
            results += build_youtube_snips_feed(request.user, limit=40)
            if sort == 'views':
                results.sort(key=lambda item: item.get("view_count") or 0, reverse=True)
            else:
                results.sort(key=lambda item: _epoch(item.get("timestamp")), reverse=True)

            total = len(results)
            try:
                page = max(int(request.query_params.get('page', 1)), 1)
            except (TypeError, ValueError):
                page = 1
            try:
                page_size = min(max(int(request.query_params.get('page_size', 20)), 1), 100)
            except (TypeError, ValueError):
                page_size = 20

            start = (page - 1) * page_size
            return Response({
                "results": results[start:start + page_size],
                "page": page,
                "page_size": page_size,
                "count": total,
                "youtube_error": youtube_api_status(),
            }, status=200)
        approved_videos = (
            Video.objects.filter(is_approved=True, visibility="public", author__is_active=True)
            .exclude(source_type="YOUTUBE", author__username=YOUTUBE_SYSTEM_USERNAME)
            .select_related('category')
            .annotate(
                num_likes=Count('likes', distinct=True),
                num_dislikes=Count('dispikes', distinct=True),
            )
        )

        if category_param:
            approved_videos = approved_videos.filter(category__slug=category_param)

        CANDIDATE_LIMIT = 500
        if sort == 'views':
            candidates = list(approved_videos.order_by('-view_count')[:CANDIDATE_LIMIT])
        else:
            candidates = list(approved_videos.order_by('-timestamp')[:CANDIDATE_LIMIT])
        ranked = ranking.rank_videos(candidates, user_interests={}, creeked_author_ids=set())

        try:
            page = max(int(request.query_params.get('page', 1)), 1)
        except (TypeError, ValueError):
            page = 1
        try:
            page_size = min(max(int(request.query_params.get('page_size', 20)), 1), 100)
        except (TypeError, ValueError):
            page_size = 20

        youtube_items = build_youtube_feed(
            feed=None,
            history_keywords=recent_watch_keywords(request.user),
        )
        combined = interleave_feed(ranked, youtube_items)

        start = (page - 1) * page_size
        page_videos = combined[start:start + page_size]
        results = serialize_mixed_items(page_videos, request)
        return Response({
            "results": results,
            "page": page,
            "page_size": page_size,
            "count": len(combined),
            "youtube_error": youtube_api_status(),
        }, status=200)



# ---------------------------
# Studio
# ---------------------------
class GetOwnVideo(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        videos = Video.objects.filter(author=request.user).annotate(
            num_likes=Count('likes', distinct=True),
            num_dislikes=Count('dispikes', distinct=True),
        )
        serializer = VideoSerializer(videos, many=True, context={'request': request})
        return Response(serializer.data, status=200)


class Categories(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        CategoryVideo.objects.get_or_create(
            slug="shortform-videos",
            defaults={"name": "Shortform Videos"}
        )
        categories = CategoryVideo.objects.annotate(
            video_count=Count('videos', filter=Q(videos__author__is_active=True)),
            count_videos=Count('videos', filter=Q(videos__author__is_active=True)) + Count('snips', filter=Q(snips__author__is_active=True))
        )
        serializer = CategoryVideoSerializer(categories, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)


class CategoryManage(APIView):
    """Create or delete categories. Only moderators can manage."""
    permission_classes = [IsAuthenticated, IsModerator]

    def post(self, request):
        name = request.data.get("name", "").strip()
        slug = request.data.get("slug", "").strip().lower()
        if not name or not slug:
            return Response({"detail": "Name and slug are required"}, status=400)
        if CategoryVideo.objects.filter(slug=slug).exists():
            return Response({"detail": "Category with this slug already exists"}, status=400)
        cat = CategoryVideo.objects.create(name=name, slug=slug)
        return Response(CategoryVideoSerializer(cat).data, status=201)

    def delete(self, request):
        cat_id = request.query_params.get("id")
        if not cat_id:
            return Response({"detail": "Category ID required"}, status=400)
        try:
            cat = CategoryVideo.objects.get(id=cat_id)
            cat.delete()
            return Response(status=204)
        except CategoryVideo.DoesNotExist:
            return Response({"detail": "Category not found"}, status=404)


class Studio(APIView):
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def patch(self, request):
        item_id = request.data.get("id")
        item_type = request.data.get("type", "video")
        if not item_id:
            return Response({"detail": "ID required"}, status=400)

        title = request.data.get("title")
        description = request.data.get("description")
        visibility = request.data.get("visibility")

        if item_type == "snip":
            snip = get_object_or_404(Snip, id=item_id, author=request.user)
            if title:
                snip.title = title
            if description is not None:
                snip.description = description
            if visibility in ("public", "unlisted", "private"):
                snip.visibility = visibility
            snip.save()
            return Response(SnipSerializer(snip, context={'request': request}).data, status=200)

        video = get_object_or_404(Video, id=item_id, author=request.user)
        thumbnail_url = request.data.get("thumbnail_url")
        video_url = request.data.get("video_url")
        category = request.data.get("category")

        if title:
            video.title = title
        if description:
            video.description = description
        if thumbnail_url:
            video.thumbnail = thumbnail_url
        if video_url:
            video.video = video_url
            video.is_approved = False
        if category:
            category_obj, _ = CategoryVideo.objects.get_or_create(
                slug=category,
                defaults={"name": category.replace("-", " ").title()},
            )
            video.category = category_obj
        if visibility in ("public", "unlisted", "private"):
            video.visibility = visibility

        video.save()
        return Response(VideoSerializer(video, context={'request': request}).data, status=200)


class StudioVideoDelete(APIView):
    permission_classes = [IsAuthenticated]

    def delete(self, request, video_id):
        video = get_object_or_404(Video, id=video_id, author=request.user)
        video.delete()
        return Response(status=204)


# ---------------------------
# Watch Video API
# ---------------------------
class LoginWatchVideo(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        video_id = request.data.get("video_id")
        if not video_id:
            return Response({"detail": "Video ID not provided"}, status=status.HTTP_400_BAD_REQUEST)

        # Resolve a stored row first.
        video = resolve_video_by_id(
            video_id,
            Video.objects.filter(is_approved=True, author__is_active=True).prefetch_related("comments__author"),
        )

        if video is None:
            return Response({"detail": "Video not found"}, status=404)

        # Allow public + unlisted for everyone, private only for owner
        approved_videos = Video.objects.filter(is_approved=True, visibility="public", author__is_active=True)
        if video.visibility == "private" and video.author != request.user:
            return Response({"detail": "Video not found"}, status=404)

        # Boost category score.
        if video.category:
            record_category_interest(request.user, video.category.slug)

        # Record watch event with spam prevention
        record_view(request.user, video=video)
        # Learn tag interests so #hashtags outrank the plain category.
        record_tag_interest_from_watch(request.user, video=video)

        # Native related videos, co-watch powered when enough history exists.
        if video.category:
            # Co-watch powered related videos
            user_recent_ids = ranking.get_user_recent_video_ids(request.user)
            if user_recent_ids:
                cowatch_map = ranking.build_cowatch_map(user_recent_ids, [video.id])
                # Get co-watch related video IDs, sorted by score
                sorted_cowatch = sorted(cowatch_map.items(), key=lambda x: x[1], reverse=True)
                cowatch_video_ids = [vid for vid, _ in sorted_cowatch[:10]]
                if cowatch_video_ids:
                    cowatch_videos = list(
                        approved_videos.filter(id__in=cowatch_video_ids)
                        .select_related('category')
                        .annotate(
                            num_likes=Count('likes', distinct=True),
                            num_dislikes=Count('dispikes', distinct=True),
                        )
                    )
                    # Sort by co-watch score
                    cowatch_order = {vid: i for i, vid in enumerate(cowatch_video_ids)}
                    cowatch_videos.sort(key=lambda v: cowatch_order.get(v.id, 999))
                    # Fill remaining with category-based
                    remaining = 12 - len(cowatch_videos)
                    if remaining > 0:
                        cat_vids = list(
                            approved_videos.filter(category=video.category)
                            .exclude(id=video.id)
                            .exclude(id__in=cowatch_video_ids)
                            .order_by('-timestamp')[:remaining]
                        )
                        cowatch_videos.extend(cat_vids)
                    native_related = cowatch_videos[:12]
                else:
                    native_related = list(
                        approved_videos.filter(category=video.category)
                        .exclude(id=video.id)
                        .order_by('-timestamp')[:12]
                    )
            else:
                native_related = list(
                    approved_videos.filter(category=video.category)
                    .exclude(id=video.id)
                    .order_by('-timestamp')[:12]
                )
            related_videos = native_related
        else:
            related_videos = list(
                approved_videos.filter(category=video.category)
                .exclude(id=video.id)
                .order_by('-timestamp')[:12]
            )

        video_author_channel = MediaProfile.objects.filter(user=video.author).first()

        try:
            like = Like.objects.get(video=video, author=request.user)
            if_liked = True
        except Like.DoesNotExist:
            like = None
            if_liked = False

        try:
            dispike = DisPike.objects.get(video=video, author=request.user)
            if_dispiked = True
        except DisPike.DoesNotExist:
            dispike = None
            if_dispiked = False

        try:
            creek = Creek.objects.get(account=video_author_channel, author=request.user)
            if_creeked = True
        except Creek.DoesNotExist:
            creek = None
            if_creeked = False

        creek_like_count = Like.objects.filter(video=video).count()
        like_count = creek_like_count
        dispike_count = DisPike.objects.filter(video=video).count()
        creek_count = Creek.objects.filter(account=video_author_channel).count() if video_author_channel else 0

        return Response({
            "video": VideoSerializer(video, context={'request': request}).data,
            "related_videos": VideoSerializer(related_videos, many=True, context={'request': request}).data,
            "like": LikeSerializer(like).data if if_liked else False,
            "like_count": like_count,
            "creek_like_count": creek_like_count,
            "dispike": DisPikeSerializer(dispike).data if if_dispiked else False,
            "dispike_count": dispike_count,
            "creek": CreekSerializer(creek).data if if_creeked else False,
            "creek_count": creek_count,
        }, status=status.HTTP_200_OK)


class GuestWatchVideo(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        video_id = request.data.get("video_id")
        if not video_id:
            return Response({"detail": "Video ID not provided"}, status=status.HTTP_400_BAD_REQUEST)

        # Resolve a stored row first.
        video = resolve_video_by_id(
            video_id,
            Video.objects.filter(is_approved=True, visibility="public", author__is_active=True),
        )

        if video is None:
            return Response({"detail": "Video not found"}, status=404)

        approved_videos = Video.objects.filter(is_approved=True, visibility="public", author__is_active=True)
        video_category = video.category

        related_videos = list(
            approved_videos.filter(category=video_category)
            .exclude(id=video.id)
            .order_by('-timestamp')[:12]
        )

        creek_like_count = Like.objects.filter(video=video).count()
        like_count = creek_like_count
        dispike_count = DisPike.objects.filter(video=video).count()
        video_author_channel = MediaProfile.objects.filter(user=video.author).first()
        creek_count = Creek.objects.filter(account=video_author_channel).count() if video_author_channel else 0

        return Response({
            "video": VideoSerializer(video, context={'request': request}).data,
            "related_videos": VideoSerializer(related_videos, many=True, context={'request': request}).data,
            "like": False,
            "like_count": like_count,
            "creek_like_count": creek_like_count,
            "dispike": False,
            "dispike_count": dispike_count,
            "creek": False,
            "creek_count": creek_count,
        }, status=status.HTTP_200_OK)


class YouTubeChannel(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        channel_id = (request.query_params.get('channel_id') or '').strip()
        if not validate_youtube_channel_id(channel_id):
            return Response(
                {'detail': 'A valid YouTube channel ID is required'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        channel = youtube_channel(channel_id)
        if not channel:
            return Response({'detail': 'Channel not found'}, status=status.HTTP_404_NOT_FOUND)
        content_type = (request.query_params.get('type') or 'videos').strip().lower()
        duration = 'short' if content_type == 'snips' else 'any'
        videos = youtube_channel_videos(channel_id, limit=24, duration=duration)
        return Response({
            'channel': channel,
            'videos': videos,
            'type': 'snips' if duration == 'short' else 'videos',
            'youtube_error': youtube_api_status(),
        }, status=status.HTTP_200_OK)


# ---------------------------
# Watch Retention Tracking
# ---------------------------
class TrackRetention(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        video_id = request.data.get("video_id")
        duration = request.data.get("duration", 0)

        if not video_id:
            return Response({"detail": "Video ID required"}, status=400)

        try:
            duration = max(0, min(int(duration), 86400))
        except (TypeError, ValueError):
            duration = 0

        video = resolve_video_by_id(video_id, Video.objects.filter(author__is_active=True))
        if video is None:
            # Live YouTube videos aren't tracked in the retention tables.
            return Response({"status": "ok"}, status=200)

        event = (
            WatchEvent.objects.filter(user=request.user, video=video)
            .order_by('-timestamp')
            .first()
        )
        if event and event.duration_watched < duration:
            event.duration_watched = duration
            event.save(update_fields=['duration_watched'])

        return Response({"status": "ok"}, status=200)


class SearchVideo(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        title = request.query_params.get("q")
        if not title:
            return Response(
                {"detail": "Title not provided"},
                status=status.HTTP_400_BAD_REQUEST
            )

        videos = Video.objects.filter(
            Q(title__icontains=title) |
            Q(description__icontains=title),
            author__is_active=True
        ).order_by("-id")[:20]

        users = MediaProfile.objects.filter(
            Q(user__username__icontains=title),
            user__is_active=True
        ).select_related('user')[:10]

        video_serializer = VideoSerializer(videos, many=True, context={'request': request})
        user_serializer = MediaProfileSerializer(users, many=True, context={'request': request})

        # Live YouTube results (videos + channels) mixed into search.
        youtube_videos = []
        youtube_channels = []
        if validate_youtube_id(title):
            yt_item = get_youtube_video_details(title)
            if yt_item:
                youtube_videos.append(yt_item)
        else:
            youtube_videos = youtube_search_videos(title, limit=10)
        youtube_channels = youtube_search_channels(title, limit=10)

        return Response({
            "videos": video_serializer.data,
            "users": user_serializer.data,
            "youtube_videos": youtube_videos,
            "youtube_channels": youtube_channels,
            "youtube_error": youtube_api_status(),
        }, status=status.HTTP_200_OK)


class SearchUsers(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        q = request.query_params.get("q", "").strip()
        if not q:
            return Response([], status=status.HTTP_200_OK)

        users = MediaProfile.objects.filter(
            Q(user__username__icontains=q),
            user__is_active=True
        ).select_related('user')[:8]

        serializer = MediaProfileSerializer(users, many=True, context={'request': request})
        return Response(serializer.data, status=status.HTTP_200_OK)


class InterestTag(APIView):
    """Every CreekTube video/snip carrying a hashtag, plus YouTube results.

    Any user can write ``#whatever`` in a title or description; that hashtag
    gets its own page at ``/interests/<tag>``. YouTube videos for the same tag
    are mixed in when the hybrid (YouTube API) build is available.
    """

    permission_classes = [AllowAny]

    def get(self, request, tag):
        cleaned = str(tag).strip().lstrip("#").lower()
        if not cleaned:
            return Response({"detail": "Tag is required"}, status=400)

        videos = (
            Video.objects.filter(
                is_approved=True, visibility="public", author__is_active=True,
                tags__name=cleaned,
            )
            .exclude(source_type="YOUTUBE", author__username=YOUTUBE_SYSTEM_USERNAME)
            .select_related("category", "author")
            .distinct()
            .order_by("-timestamp")[:40]
        )
        snips = (
            Snip.objects.filter(
                is_approved=True, visibility="public", author__is_active=True,
                tags__name=cleaned,
            )
            .select_related("author")
            .distinct()
            .order_by("-timestamp")[:40]
        )

        return Response({
            "tag": cleaned,
            "videos": VideoSerializer(videos, many=True, context={"request": request}).data,
            "snips": SnipSerializer(snips, many=True, context={"request": request}).data,
            "youtube_videos": youtube_search_videos(cleaned, limit=20),
        }, status=200)


class SearchUsersMod(APIView):
    permission_classes = [IsModerator]

    def get(self, request):
        q = request.query_params.get("q", "").strip()
        if not q:
            return Response([], status=status.HTTP_200_OK)

        users = MediaProfile.objects.filter(
            Q(user__username__icontains=q)
        ).select_related('user')[:8]

        serializer = MediaProfileSerializer(users, many=True, context={'request': request})
        return Response(serializer.data, status=status.HTTP_200_OK)

class IfModerator(APIView):
    permission_classes = [IsModerator]

    def get(self, request):
        return Response({"detail": "You are a moderator"}, status=status.HTTP_200_OK)


class ModPanel(APIView):
    permission_classes = [IsModerator]

    def get(self, request):
        unapproved_videos = Video.objects.filter(is_approved=False)
        unapproved_snips = Snip.objects.filter(is_approved=False)
        video_serializer = VideoSerializer(unapproved_videos, many=True, context={'request': request})
        snip_serializer = SnipSerializer(unapproved_snips, many=True, context={'request': request})
        return Response({
            "videos": video_serializer.data,
            "snips": snip_serializer.data
        }, status=status.HTTP_200_OK)

    def post(self, request):
        profile = getattr(request.user, "mediaprofile", None)
        if not (request.user.is_superuser or (profile and profile.has_permission("mod.approve"))):
            return Response(
                {"detail": "Your moderator title cannot approve or reject content"},
                status=403,
            )
        video_id = request.data.get("id")
        if not video_id:
            return Response({"detail": "Video ID not provided"}, status=status.HTTP_400_BAD_REQUEST)
        try:
            video = Video.objects.get(id=video_id)
            video.is_approved = not video.is_approved
            video.save()
        except:
            snips = Snip.objects.filter(id=video_id)
            if snips.exists():
                video = snips.first()
                video.is_approved = not video.is_approved
                video.save()
                snip_serializer = SnipSerializer(video, context={'request': request})
                return Response(snip_serializer.data, status=status.HTTP_200_OK)
            else:
                return Response({"detail": "Video not found"}, status=status.HTTP_404_NOT_FOUND)
        # video = get_object_or_404(Video, id=video_id)
        
        serializer = VideoSerializer(video, context={'request': request})
        return Response(serializer.data, status=status.HTTP_200_OK)

    def delete(self, request):
        profile = getattr(request.user, "mediaprofile", None)
        if not (request.user.is_superuser or (profile and profile.has_permission("mod.approve"))):
            return Response(
                {"detail": "Your moderator title cannot remove content"},
                status=403,
            )
        video_id = request.data.get("id")
        if not video_id:
            return Response({"detail": "Video ID not provided"}, status=status.HTTP_400_BAD_REQUEST)
        try:
            video = Video.objects.get(id=video_id)
        except Video.DoesNotExist:
            snips = Snip.objects.filter(id=video_id)
            if snips.exists():
                video = snips.first()
                video.delete()
                return Response({"detail": "Snip deleted"}, status=status.HTTP_204_NO_CONTENT)
            else:
                return Response({"detail": "Video not found"}, status=status.HTTP_404_NOT_FOUND)
        video.delete()
        return Response({"detail": "Video deleted"}, status=status.HTTP_204_NO_CONTENT)


class SetAccountActive(APIView):
    """
    Moderator tool: deactivate or reactivate an account.
    Deactivated accounts still exist but their content is hidden
    everywhere and they can no longer authenticate.

    Requires the ``mod.deactivate`` permission, so a title restricted to
    content approval cannot touch account state.
    """
    permission_classes = [IsModerator]

    def post(self, request):
        profile = getattr(request.user, "mediaprofile", None)
        if not (request.user.is_superuser or (profile and profile.has_permission("mod.deactivate"))):
            return Response(
                {"error": "Your moderator title does not include account deactivation rights"},
                status=403,
            )

        account_id = request.data.get("id")
        active = request.data.get("active")
        if not account_id:
            return Response({"error": "Account ID required"}, status=400)
        if active is None:
            return Response({"error": "active boolean required"}, status=400)
        if isinstance(active, str):
            active = active.strip().lower() in ("1", "true", "yes", "on")

        try:
            profile_media = MediaProfile.objects.get(id=account_id)
        except MediaProfile.DoesNotExist:
            return Response({"error": "Account not found"}, status=404)

        user = profile_media.user
        if user.is_superuser:
            return Response({"error": "You cannot modify an admin account"}, status=400)

        if user == request.user:
            return Response({"error": "You cannot modify your own account"}, status=400)

        target_profile = getattr(user, "mediaprofile", None)
        if active is False and target_profile and target_profile.is_moderator():
            return Response({"error": "You cannot deactivate another moderator's account"}, status=400)

        if active is False:
            reason = (request.data.get("reason") or "").strip()
            if not reason:
                return Response({"error": "A reason is required when deactivating an account"}, status=400)

        user.is_active = bool(active)
        user.save(update_fields=["is_active"])

        if not user.is_active:
            # Immediately invalidate all outstanding refresh tokens
            from rest_framework_simplejwt.token_blacklist.models import OutstandingToken
            OutstandingToken.objects.filter(user=user).delete()

        ModActionLog.objects.create(
            target=user,
            moderator=request.user,
            action="deactivate" if not user.is_active else "reactivate",
            reason=reason if not user.is_active else (request.data.get("reason") or "").strip(),
        )

        return Response({
            "detail": "Account deactivated" if not user.is_active else "Account reactivated",
            "active": user.is_active,
        }, status=200)


class ModActionLogs(APIView):
    """
    Moderator tool: list every soft-ban/deactivation and reactivation,
    visible to all moderators.
    """
    permission_classes = [IsModerator]

    def get(self, request):
        logs = ModActionLog.objects.select_related("target", "moderator")[:200]
        data = [
            {
                "id": log.pk,
                "target": log.target.username,
                "target_id": log.target.pk,
                "moderator": log.moderator.username,
                "action": log.action,
                "reason": log.reason,
                "created_at": log.created_at.isoformat(),
            }
            for log in logs
        ]
        return Response(data, status=200)


# ---------------------------
# Superuser admin panel (titles, permissions, grants)
# ---------------------------
class AdminPanel(APIView):
    """Superuser overview: every title, the permission catalog, and stats."""

    permission_classes = [IsSuperUser]

    def get(self, request):
        titles = [_title_payload(t) for t in UserTitle.objects.all()]
        users = User.objects.select_related("mediaprofile").count()
        videos = Video.objects.count()
        snips = Snip.objects.count()
        mod_count = MediaProfile.objects.filter(moderator=True).count()
        return Response({
            "titles": titles,
            "permissions": ADMIN_PERMISSIONS,
            "stats": {
                "users": users,
                "videos": videos,
                "snips": snips,
                "moderators": mod_count,
            },
        }, status=200)


class AdminTitles(APIView):
    """Create, edit and delete titles (name, color, symbol, permissions)."""

    permission_classes = [IsSuperUser]

    def get(self, request):
        return Response([_title_payload(t) for t in UserTitle.objects.all()], status=200)

    def post(self, request):
        name = (request.data.get("name") or "").strip()
        if not name:
            return Response({"error": "Title name is required"}, status=400)
        color = (request.data.get("color") or "#e11d48").strip()
        symbol = (request.data.get("symbol") or "").strip()[:8]
        permissions = request.data.get("permissions") or []
        if isinstance(permissions, str):
            permissions = [p.strip() for p in permissions.split(",") if p.strip()]
        permissions = [p for p in permissions if p in {x["code"] for x in ADMIN_PERMISSIONS}]

        title, created = UserTitle.objects.get_or_create(
            name=name,
            defaults={"color": color, "symbol": symbol, "permissions": permissions},
        )
        if not created:
            return Response({"error": "A title with that name already exists"}, status=400)
        return Response(_title_payload(title), status=201)

    def put(self, request):
        title_id = request.data.get("id")
        if not title_id:
            return Response({"error": "Title ID required"}, status=400)
        try:
            title = UserTitle.objects.get(id=title_id)
        except UserTitle.DoesNotExist:
            return Response({"error": "Title not found"}, status=404)

        name = (request.data.get("name") or "").strip()
        if name:
            dup = UserTitle.objects.filter(name=name).exclude(id=title.id).exists()
            if dup:
                return Response({"error": "A title with that name already exists"}, status=400)
            title.name = name
        title.color = (request.data.get("color") or title.color).strip()
        title.symbol = (request.data.get("symbol", title.symbol) or "").strip()[:8]
        if "permissions" in request.data:
            permissions = request.data.get("permissions") or []
            if isinstance(permissions, str):
                permissions = [p.strip() for p in permissions.split(",") if p.strip()]
            title.permissions = [p for p in permissions if p in {x["code"] for x in ADMIN_PERMISSIONS}]
        title.save()

        # Re-sync every holder's legacy flags in case permissions changed.
        for profile in title.holders.all():
            _sync_legacy_title_flags(profile)
        return Response(_title_payload(title), status=200)

    def delete(self, request):
        title_id = request.data.get("id")
        if not title_id:
            return Response({"error": "Title ID required"}, status=400)
        try:
            title = UserTitle.objects.get(id=title_id)
        except UserTitle.DoesNotExist:
            return Response({"error": "Title not found"}, status=404)
        holders = list(title.holders.all())
        title.delete()
        for profile in holders:
            _sync_legacy_title_flags(profile)
        return Response({"detail": "Title deleted"}, status=200)


class AdminUsers(APIView):
    """Search users and grant/revoke titles."""

    permission_classes = [IsSuperUser]

    def get(self, request):
        q = (request.query_params.get("q") or "").strip()
        profiles = MediaProfile.objects.select_related("user").prefetch_related("titles")
        if q:
            profiles = profiles.filter(user__username__icontains=q)
        data = []
        for profile in profiles[:50]:
            data.append({
                "id": profile.id,
                "user_id": profile.user.id,
                "username": profile.user.username,
                "active": profile.user.is_active,
                "is_superuser": profile.user.is_superuser,
                "titles": profile.title_payloads(),
                "moderator": profile.is_moderator(),
                "official": profile.is_official(),
            })
        return Response(data, status=200)

    def post(self, request):
        profile_id = request.data.get("id")
        title_id = request.data.get("title_id")
        grant = request.data.get("active", True)
        if isinstance(grant, str):
            grant = grant.strip().lower() in ("1", "true", "yes", "on")

        if not profile_id or not title_id:
            return Response({"error": "Both a profile id and a title id are required"}, status=400)
        try:
            profile = MediaProfile.objects.get(id=profile_id)
        except MediaProfile.DoesNotExist:
            return Response({"error": "Account not found"}, status=404)
        try:
            title = UserTitle.objects.get(id=title_id)
        except UserTitle.DoesNotExist:
            return Response({"error": "Title not found"}, status=404)

        if grant:
            profile.titles.add(title)
        else:
            profile.titles.remove(title)
        _sync_legacy_title_flags(profile)
        return Response({
            "id": profile.id,
            "username": profile.user.username,
            "titles": profile.title_payloads(),
            "moderator": profile.is_moderator(),
            "official": profile.is_official(),
        }, status=200)


# ---------------------------
# Interactable Video Features
# ---------------------------
@method_decorator(csrf_exempt, name='dispatch')
class CommentVideo(APIView):
    def get(self, request):
        video_id = request.query_params.get('video_id')
        if not video_id:
            return Response({'message': 'No video ID provided'}, status=status.HTTP_400_BAD_REQUEST)

        video = resolve_video_by_id(video_id, Video.objects.filter(author__is_active=True))
        if video is None:
            # Live YouTube videos stream YouTube's own comments (read-only).
            if validate_youtube_id(video_id):
                return Response(youtube_comments(video_id), status=status.HTTP_200_OK)
            return Response({'message': 'Video not found'}, status=status.HTTP_404_NOT_FOUND)

        top_level = Comment.objects.filter(video=video, parent=None, author__is_active=True).select_related('author').order_by('-is_pinned', '-timestamp')

        serializer = CommentSerializer(top_level, many=True, context={'request': request})
        data = serializer.data

        # YouTube rows show live YouTube comments (read-only) alongside any
        # CreekTube comments the community has added.
        if video.source_type == "YOUTUBE" and video.youtube_video_id:
            yt_comments = youtube_comments(video.youtube_video_id)
            for comment in yt_comments:
                comment["creek_comment_id"] = None
            data = yt_comments + data
        return Response(data, status=status.HTTP_200_OK)


@method_decorator(csrf_exempt, name='dispatch')
class UploadCommentVideo(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        author = request.user
        comment_text = request.data.get('comment')
        video_id = request.data.get('video_id')
        parent_id = request.data.get('parent_id')

        if not comment_text:
            return Response({'message': 'No comment provided'}, status=status.HTTP_400_BAD_REQUEST)

        video = resolve_video_by_id(video_id, Video.objects.filter(author__is_active=True))
        if video is None and validate_youtube_id(video_id):
            # Live YouTube video: materialize a stored row so the CreekTube
            # comment lives in our database alongside the read-only YouTube
            # comments.
            video = ensure_youtube_video(video_id, user=author)
        if video is None:
            return Response(
                {'message': 'Comments are managed on YouTube for this video'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Spam check
        if not check_comment_spam(author, video):
            return Response(
                {'message': 'You are posting too many comments. Please wait before posting again.'},
                status=status.HTTP_429_TOO_MANY_REQUESTS
            )

        parent = None
        if parent_id:
            parent = get_object_or_404(Comment, id=parent_id, video=video)

        comment = Comment.objects.create(author=author, video=video, text=comment_text, parent=parent)

        return Response({
            'message': 'Comment added successfully',
            'comment': CommentSerializer(comment, context={'request': request}).data
        }, status=status.HTTP_201_CREATED)


class PikeVideo(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        video_id = request.data.get("id")
        if not video_id:
            return Response({"detail": "Video ID required"}, status=status.HTTP_400_BAD_REQUEST)

        video = resolve_video_by_id(video_id, Video.objects.filter(author__is_active=True))
        if video is None and validate_youtube_id(video_id):
            # Live YouTube video: materialize a stored row so the CreekTube
            # like is tracked against YouTube's real count.
            video = ensure_youtube_video(video_id, user=request.user)
        if video is None:
            return Response({"liked": False}, status=status.HTTP_200_OK)

        like, created = Like.objects.get_or_create(author=request.user, video=video)

        creek_like_count = Like.objects.filter(video=video).count()
        if video.source_type == "YOUTUBE" and video.youtube_video_id:
            like_count = youtube_like_counts_for([video.youtube_video_id]).get(video.youtube_video_id, 0)
            youtube_like_count = like_count
        else:
            like_count = creek_like_count
            youtube_like_count = None

        if not created:
            like.delete()
            creek_like_count = Like.objects.filter(video=video).count()
            return Response({
                "liked": False,
                "like_count": like_count,
                "youtube_like_count": youtube_like_count,
                "creek_like_count": creek_like_count,
            }, status=status.HTTP_200_OK)

        return Response({
            "liked": True,
            "like_count": like_count,
            "youtube_like_count": youtube_like_count,
            "creek_like_count": creek_like_count,
        }, status=status.HTTP_201_CREATED)


@method_decorator(csrf_exempt, name='dispatch')
class PinComment(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        comment_id = request.data.get("comment_id")
        if not comment_id:
            return Response({"detail": "Comment ID required"}, status=status.HTTP_400_BAD_REQUEST)

        comment = get_object_or_404(Comment, id=comment_id)

        if comment.video.author != request.user:
            return Response({"detail": "Only the video author can pin comments"}, status=status.HTTP_403_FORBIDDEN)

        comment.is_pinned = not comment.is_pinned
        comment.save(update_fields=['is_pinned'])

        return Response({"is_pinned": comment.is_pinned}, status=status.HTTP_200_OK)


class DisPikeVideo(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        video_id = request.data.get("id")
        if not video_id:
            return Response({"detail": "Video ID required"}, status=status.HTTP_400_BAD_REQUEST)

        video = resolve_video_by_id(video_id, Video.objects.filter(author__is_active=True))
        if video is None:
            # Live YouTube videos (not stored as CreekTube rows) have no dislike state.
            return Response({"dispike": False}, status=status.HTTP_200_OK)

        dispike, created = DisPike.objects.get_or_create(author=request.user, video=video)

        profile, _ = MediaProfile.objects.get_or_create(user=request.user)
        if video.category:
            delta = -ranking.DISLIKE_PENALTY if created else ranking.DISLIKE_PENALTY
            profile.categories = ranking.adjust_category_score(profile.categories, video.category.slug, delta)
            profile.save()

        if not created:
            dispike.delete()
            return Response({"dispike": False}, status=status.HTTP_200_OK)

        return Response({"dispike": True}, status=status.HTTP_201_CREATED)


def _resolve_profile(profile_id):
    """Resolve a MediaProfile from either a MediaProfile pk or a User pk.

    ``/account/[id]`` links and creek calls may carry a MediaProfile id
    (video cards, search, comments) or a User id (chat info link, header).
    """
    profile = MediaProfile.objects.filter(user__is_active=True).filter(id=profile_id).first()
    if profile is None:
        profile = MediaProfile.objects.filter(user__is_active=True).filter(user_id=profile_id).first()
    if profile is None:
        raise MediaProfile.DoesNotExist
    return profile


class CreekAccount(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        account_id = request.data.get("id")
        if not account_id:
            return Response({"detail": "Account ID required"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            account = _resolve_profile(account_id)
        except MediaProfile.DoesNotExist:
            return Response({"detail": "Account not found"}, status=status.HTTP_404_NOT_FOUND)

        if account.user == request.user:
            return Response({"detail": "You cannot creek your own channel"}, status=status.HTTP_400_BAD_REQUEST)

        creek, created = Creek.objects.get_or_create(author=request.user, account=account)

        if not created:
            creek.delete()
            return Response({"creek": False}, status=status.HTTP_200_OK)

        return Response({"creek": True}, status=status.HTTP_201_CREATED)


class CreekYouTubeChannel(APIView):
    """Creek (follow) a YouTube channel, shown read-only alongside Creektube creators."""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        channel_id = (request.data.get("channel_id") or "").strip()
        if not validate_youtube_channel_id(channel_id):
            return Response(
                {"detail": "A valid YouTube channel ID is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        follow, created = YouTubeChannelFollow.objects.get_or_create(
            user=request.user,
            channel_id=channel_id,
            defaults={
                "channel_name": (request.data.get("channel_name") or "").strip()[:255],
                "channel_thumbnail": (request.data.get("channel_thumbnail") or "").strip(),
                "channel_handle": (request.data.get("channel_handle") or "").strip().lstrip("@")[:255],
            },
        )
        if not created:
            follow.delete()
            return Response({"creek": False}, status=status.HTTP_200_OK)
        return Response({
            "creek": True,
            "channel": {
                "channel_id": follow.channel_id,
                "channel_name": follow.channel_name,
                "channel_thumbnail": follow.channel_thumbnail,
                "channel_handle": follow.channel_handle,
            },
        }, status=status.HTTP_201_CREATED)


class YouTubeFollows(APIView):
    """List the YouTube channels the user creeks (read-only subscriptions)."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        follows = YouTubeChannelFollow.objects.filter(user=request.user)
        return Response([
            {
                "channel_id": f.channel_id,
                "channel_name": f.channel_name,
                "channel_thumbnail": f.channel_thumbnail,
                "channel_handle": f.channel_handle,
                "created_at": f.created_at.isoformat(),
            }
            for f in follows
        ], status=200)


# ---------------------------
# Upload Video API (with spam prevention)
# ---------------------------
class UploadVideo(APIView):
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def post(self, request):
        # Rate limit check
        if not check_upload_rate_limit(request.user):
            return Response(
                {"message": "Upload rate limit reached. Please wait before uploading again."},
                status=429
            )

        author = request.user
        video_url = request.data.get("video_url")
        video_public_id = request.data.get("video_public_id", "")
        thumbnail_url = request.data.get("thumbnail_url")
        thumbnail_public_id = request.data.get("thumbnail_public_id", "")

        if video_url and video_public_id and thumbnail_url and thumbnail_public_id:
            video_file = video_url
            thumbnail_file = thumbnail_url
        else:
            video_file = request.data.get("video")
            thumbnail_file = request.data.get("thumbnail")

        category = request.data.get('category')
        title = request.data.get("title")
        description = request.data.get("description")
        visibility = request.data.get("visibility", "public")
        if visibility not in ("public", "unlisted", "private"):
            visibility = "public"

        if not video_file:
            return Response({"message": "No video provided"}, status=400)

        # Auto-generate thumbnail from Cloudinary video URL if none provided
        if not thumbnail_file and video_url and video_public_id:
            from django.conf import settings
            cloud_name = getattr(settings, 'CLOUDINARY_STORAGE', {}).get('CLOUD_NAME', '')
            if cloud_name:
                thumbnail_file = f"https://res.cloudinary.com/{cloud_name}/video/upload/so_0,e_preview/{video_public_id}.jpg"

        category_obj, _ = CategoryVideo.objects.get_or_create(
            slug=category,
            defaults={"name": category.replace("-", " ").title()},
        )

        try:
            video_instance = Video.objects.create(
                video=video_file,
                author=author,
                title=title,
                category=category_obj,
                description=description,
                thumbnail=thumbnail_file or "",
                video_public_id=video_public_id,
                thumbnail_public_id=thumbnail_public_id,
                visibility=visibility,
                timestamp=timezone.now(),
                is_approved=False,
            )
        except Exception as e:
            # Cleanup orphaned Cloudinary uploads if DB save fails
            from .cloudinary_utils import delete_cloudinary_resource
            delete_cloudinary_resource(video_public_id, "video")
            delete_cloudinary_resource(thumbnail_public_id, "image")
            logger.error("UploadVideo DB save failed for %s: %s", author.username, e)
            return Response({"message": "Upload failed. Please try again."}, status=500)

        record_upload(request.user)

        apply_tags(video_instance, title, description)
        serializer = VideoSerializer(video_instance, context={'request': request})
        return Response(serializer.data, status=201)


class AddYouTubeVideo(APIView):
    """
    Creators add a YouTube video to CreekTube by URL or video ID.

    The source_type is ALWAYS set server-side to YOUTUBE — the client can
    never forge a source. YouTube media is never downloaded or stored; only
    the 11-character video ID (plus optional channel attribution) is kept.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        if not check_upload_rate_limit(request.user):
            return Response(
                {"message": "Upload rate limit reached. Please wait before uploading again."},
                status=429,
            )

        source = (
            request.data.get("youtube_url")
            or request.data.get("youtube_video_id")
            or ""
        ).strip()
        video_id = normalize_youtube_url(source)
        if not video_id:
            return Response(
                {"detail": "A valid YouTube video URL or ID is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        title = (request.data.get("title") or "").strip()
        description = (request.data.get("description") or "").strip()
        visibility = request.data.get("visibility", "public")
        if visibility not in ("public", "unlisted", "private"):
            visibility = "public"

        metadata = get_video_metadata(video_id) or {}
        details = get_youtube_video_details(video_id) or {}
        duration = details.get("duration") or 0

        category_obj = None
        category = request.data.get("category")
        if category:
            category_obj, _ = CategoryVideo.objects.get_or_create(
                slug=category,
                defaults={"name": category.replace("-", " ").title()},
            )
        elif metadata.get("category"):
            # No explicit category: derive one from the YouTube video's own
            # category so stored YouTube rows are categorized from day one.
            category_obj, _ = CategoryVideo.objects.get_or_create(
                slug=metadata["category"],
                defaults={"name": metadata.get("category_name") or metadata["category"].replace("-", " ").title()},
            )

        video = Video.objects.create(
            author=request.user,
            category=category_obj,
            title=title or metadata.get("title") or "YouTube video",
            description=description or metadata.get("description") or "",
            thumbnail=youtube_thumbnail_url(video_id),
            video="",
            source_type="YOUTUBE",
            youtube_video_id=video_id,
            youtube_channel_id=metadata.get("channel_id", ""),
            youtube_channel_name=metadata.get("channel_name", ""),
            visibility=visibility,
            timestamp=timezone.now(),
            is_approved=True,
            duration=duration,
            content_type=classify_content_type(duration),
        )

        record_upload(request.user)

        apply_tags(video, video.title, video.description)
        serializer = VideoSerializer(video, context={'request': request})
        return Response(serializer.data, status=201)


class StudioComments(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        videos = Video.objects.filter(author=request.user)
        snips = Snip.objects.filter(author=request.user)
        video_comments = Comment.objects.filter(video__in=videos).select_related('author', 'video')
        snip_comments = Comment.objects.filter(snip__in=snips).select_related('author', 'snip')
        comments = (video_comments | snip_comments).order_by('-timestamp')
        data = [{
            'id': c.id,
            'text': c.text,
            'author': c.author.username,
            'video_id': c.video.id if c.video else c.snip.id,
            'video_title': c.video.title if c.video else c.snip.title,
            'timestamp': c.timestamp,
            'is_pinned': c.is_pinned,
            'is_snip': c.snip is not None,
        } for c in comments]
        return Response(data, status=200)

    def delete(self, request):
        comment_id = request.query_params.get('id')
        if not comment_id:
            return Response({'detail': 'Comment ID required'}, status=400)
        try:
            comment = Comment.objects.select_related('video', 'snip').get(
                Q(video__author=request.user) | Q(snip__author=request.user),
                id=comment_id,
            )
            comment.delete()
            return Response(status=204)
        except Comment.DoesNotExist:
            return Response({'detail': 'Comment not found'}, status=404)


class Account(APIView):
    permission_classes = [AllowAny]

    def _handle(self, request):
        id = request.query_params.get("id") or request.data.get("id")
        if not id:
            return Response({"error": "ID is required"}, status=400)

        try:
            profile_media = _resolve_profile(id)
        except MediaProfile.DoesNotExist:
            return Response({"error": "Profile not found"}, status=404)

        user = profile_media.user
        if not user.is_active:
            return Response({"error": "Profile not found"}, status=404)

        videos = Video.objects.filter(author=user, is_approved=True, author__is_active=True)
        snips = Snip.objects.filter(author=user, is_approved=True, author__is_active=True)
        creek_count = Creek.objects.filter(account=profile_media).count()

        is_creeked = False
        if request.user.is_authenticated:
            is_creeked = Creek.objects.filter(author=request.user, account=profile_media).exists()

        try:
            user_profile = Profile.objects.get(user=user)
            profile_data = ProfileSerializer(user_profile, context={"request": request}).data
        except Profile.DoesNotExist:
            profile_data = {"avatar_url": None, "bio": None}

        total_views = 0
        for video in videos:
            total_views += video.view_count
        for snip in snips:
            total_views += snip.view_count

        # Featured video (most viewed public video)
        featured = videos.filter(visibility="public").order_by('-view_count').first()

        return Response({
            "profile": profile_data,
            "account": MediaProfileSerializer(profile_media, context={"request": request}).data,
            "stats": {
                "videos": videos.count(),
                "snips": snips.count(),
                "total_views": total_views,
                "creeks": creek_count,
                "joined": user.date_joined.strftime("%b %Y") if user.date_joined else None,
            },
            "featured": VideoSerializer(featured, context={'request': request}).data if featured else None,
            "videos": VideoSerializer(videos, many=True, context={'request': request}).data,
            "snips": SnipSerializer(snips, many=True, context={'request': request}).data,
            "creek_count": creek_count,
            "creek": is_creeked,
        }, status=200)

    def get(self, request):
        return self._handle(request)

    def post(self, request):
        return self._handle(request)


class SetBanner(APIView):
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]

    def put(self, request):
        banner_file = request.FILES.get("banner")
        if not banner_file:
            return Response({"error": "No banner uploaded"}, status=400)

        profile, _ = MediaProfile.objects.get_or_create(user=request.user)
        profile.banner = banner_file
        profile.save()

        return Response({
            "banner": MediaProfileSerializer(profile, context={"request": request}).data["banner"]
        }, status=200)

    def delete(self, request):
        profile, _ = MediaProfile.objects.get_or_create(user=request.user)
        profile.banner.delete(save=False)
        profile.banner = None
        profile.save()
        return Response({"banner": None}, status=200)


class NotificationList(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        limit = request.query_params.get("limit", 50)
        try:
            limit = min(int(limit), 100)
        except (TypeError, ValueError):
            limit = 50

        notifications = Notification.objects.filter(recipient=request.user)[:limit]
        serializer = NotificationSerializer(notifications, many=True, context={'request': request})
        return Response(serializer.data, status=status.HTTP_200_OK)


class NotificationUnreadCount(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        count = Notification.objects.filter(recipient=request.user, is_read=False).count()
        return Response({"count": count}, status=status.HTTP_200_OK)


class NotificationMarkRead(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        notification_id = request.data.get("id")
        if notification_id:
            Notification.objects.filter(id=notification_id, recipient=request.user).update(is_read=True)
        return Response({"status": "ok"}, status=status.HTTP_200_OK)


class NotificationMarkAllRead(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        Notification.objects.filter(recipient=request.user, is_read=False).update(is_read=True)
        return Response({"status": "ok"}, status=status.HTTP_200_OK)


@method_decorator(csrf_exempt, name='dispatch')
class CommentLikeToggle(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        comment_id = request.data.get("comment_id")
        if not comment_id:
            return Response({"detail": "Comment ID required"}, status=status.HTTP_400_BAD_REQUEST)

        comment = get_object_or_404(Comment, id=comment_id)
        like, created = CommentLike.objects.get_or_create(author=request.user, comment=comment)

        if not created:
            like.delete()
            return Response({"liked": False, "likes_count": comment.likes.count()}, status=status.HTTP_200_OK)

        return Response({"liked": True, "likes_count": comment.likes.count()}, status=status.HTTP_201_CREATED)


@method_decorator(csrf_exempt, name='dispatch')
class CommentEdit(APIView):
    permission_classes = [IsAuthenticated]

    def patch(self, request):
        comment_id = request.data.get("comment_id")
        new_text = request.data.get("text")

        if not comment_id or not new_text:
            return Response({"detail": "Comment ID and text required"}, status=status.HTTP_400_BAD_REQUEST)

        comment = get_object_or_404(Comment, id=comment_id, author=request.user)
        comment.text = new_text
        comment.edited = True
        comment.save(update_fields=['text', 'edited'])

        return Response({
            'message': 'Comment updated',
            'comment': CommentSerializer(comment, context={'request': request}).data
        }, status=status.HTTP_200_OK)


@method_decorator(csrf_exempt, name='dispatch')
class CommentDelete(APIView):
    permission_classes = [IsAuthenticated]

    def delete(self, request, comment_id):
        comment = get_object_or_404(Comment, id=comment_id, author=request.user)
        comment.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class UserSettings(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        profile, _ = Profile.objects.get_or_create(user=request.user)
        return Response({
            "username": request.user.username,
            "email": request.user.email,
            "bio": profile.bio or "",
            "avatar": request.build_absolute_uri(profile.avatar.url) if profile.avatar else None,
            "notification_reply": profile.notification_reply,
        }, status=status.HTTP_200_OK)

    def put(self, request):
        profile, _ = Profile.objects.get_or_create(user=request.user)

        bio = request.data.get("bio")
        if bio is not None:
            profile.bio = bio

        notification_reply = request.data.get("notification_reply")
        if notification_reply is not None:
            profile.notification_reply = bool(notification_reply)

        profile.save(update_fields=['bio', 'notification_reply'])

        return Response({
            "message": "Settings updated",
            "notification_reply": profile.notification_reply,
        }, status=status.HTTP_200_OK)


# ---------------------------
# Snips (Short-form videos)
# ---------------------------
class UploadSnip(APIView):
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def post(self, request):
        if not check_upload_rate_limit(request.user):
            return Response(
                {"message": "Upload rate limit reached. Please wait before uploading again."},
                status=429,
            )

        video_url = request.data.get("video_url")
        video_public_id = request.data.get("video_public_id", "")
        thumbnail_url = request.data.get("thumbnail_url", "")
        thumbnail_public_id = request.data.get("thumbnail_public_id", "")
        video_file = request.FILES.get("video") or request.FILES.get("file") or request.data.get("video")

        if not video_url and video_file and not isinstance(video_file, str):
            from django.core.files.storage import default_storage
            filename = default_storage.save(f"snips/{request.user.id}_{int(timezone.now().timestamp())}_{video_file.name}", video_file)
            video_url = default_storage.url(filename)

        if not video_url and isinstance(request.data.get("video"), str):
            video_url = request.data.get("video")

        if not video_url:
            return Response({"message": "No video provided"}, status=400)

        title = request.data.get("title", "").strip()
        if not title:
            return Response({"message": "Title is required"}, status=400)

        visibility = request.data.get("visibility", "public")
        if visibility not in ("public", "unlisted", "private"):
            visibility = "public"

        # Duration is optional (seconds). Tolerate a raw ISO-8601 or "1:23"
        # style string as well as a plain number.
        duration = 0
        raw_duration = request.data.get("duration", "")
        if raw_duration not in (None, ""):
            duration = youtube_duration_seconds(raw_duration)
            if not duration:
                parts = str(raw_duration).split(":")
                try:
                    parts = [int(p) for p in parts]
                except (TypeError, ValueError):
                    parts = []
                if len(parts) == 2:
                    duration = parts[0] * 60 + parts[1]
                elif len(parts) == 3:
                    duration = parts[0] * 3600 + parts[1] * 60 + parts[2]

        # Auto-generate thumbnail from Cloudinary video URL if none provided
        if not thumbnail_url and video_url and video_public_id:
            from django.conf import settings
            cloud_name = getattr(settings, 'CLOUDINARY_STORAGE', {}).get('CLOUD_NAME', '')
            if cloud_name:
                thumbnail_url = f"https://res.cloudinary.com/{cloud_name}/video/upload/so_0,e_preview/{video_public_id}.jpg"

        try:
            snip = Snip.objects.create(
                author=request.user,
                title=title,
                description=request.data.get("description", ""),
                video=video_url,
                thumbnail=thumbnail_url,
                video_public_id=video_public_id,
                thumbnail_public_id=thumbnail_public_id,
                visibility=visibility,
                is_approved=False,
                duration=duration,
            )
        except Exception as e:
            from .cloudinary_utils import delete_cloudinary_resource
            delete_cloudinary_resource(video_public_id, "video")
            delete_cloudinary_resource(thumbnail_public_id, "image")
            logger.error("UploadSnip DB save failed for %s: %s", request.user.username, e)
            return Response({"message": "Upload failed. Please try again."}, status=500)

        record_upload(request.user)
        apply_tags(snip, snip.title, snip.description)
        serializer = SnipSerializer(snip, context={"request": request})

        try:
            from channels.layers import get_channel_layer
            from asgiref.sync import async_to_sync
            channel_layer = get_channel_layer()
            async_to_sync(channel_layer.group_send)(
                "snips_feed",
                {
                    "type": "new_snip",
                    "snip": serializer.data,
                },
            )
        except Exception:
            pass

        return Response(serializer.data, status=201)


# ---------------------------
# Snip Recommendation API
# ---------------------------
def _parse_id_list(raw):
    """Parse a comma-separated list of numeric ids from a query param."""
    ids = set()
    if not raw:
        return ids
    for part in str(raw).split(","):
        part = part.strip()
        if part.isdigit():
            ids.add(int(part))
    return ids


def _serialize_snips(scored_entries, request):
    """Serialize ``[{"snip": obj, "score", "reason"}]`` into API dicts.

    The internal ``score`` is never exposed; only the human-readable
    ``reason`` is attached to each item. Like/save/dislike state is batch
    loaded so the serializer issues no per-row queries.
    """
    if not scored_entries:
        return []
    snips = [entry["snip"] for entry in scored_entries]
    reasons = {
        entry["snip"].id: entry.get("reason")
        for entry in scored_entries
    }
    ids = [s.id for s in snips]
    user = request.user if request.user.is_authenticated else None
    context = {"request": request, "snip_reasons": reasons}
    if user:
        context["snip_liked_ids"] = set(
            SnipLike.objects.filter(author=user, snip_id__in=ids).values_list("snip_id", flat=True)
        )
        context["snip_saved_ids"] = set(
            SnipSave.objects.filter(author=user, snip_id__in=ids).values_list("snip_id", flat=True)
        )
        context["snip_disliked_ids"] = set(
            SnipDislike.objects.filter(author=user, snip_id__in=ids).values_list("snip_id", flat=True)
        )
        # Batch creator-follow state + follower counts.
        author_ids = {s.author_id for s in snips}
        followed = set(
            Creek.objects.filter(author=user, account__user_id__in=author_ids)
            .values_list("account__user_id", flat=True)
        )
        context["snip_followed_author_ids"] = followed
        context["snip_creator_followers"] = dict(
            MediaProfile.objects.filter(user_id__in=author_ids)
            .annotate(creek_count=Count("account"))
            .values_list("user_id", "creek_count")
        )
    serializer = SnipSerializer(
        snips, many=True, context=context
    )
    return list(serializer.data)


def _state_for_user(user, snip_ids):
    """Batch load like/save/dislike state for ``user`` over ``snip_ids``."""
    ids = list(snip_ids)
    return {
        "liked": set(
            SnipLike.objects.filter(author=user, snip_id__in=ids).values_list("snip_id", flat=True)
        ),
        "saved": set(
            SnipSave.objects.filter(author=user, snip_id__in=ids).values_list("snip_id", flat=True)
        ),
        "disliked": set(
            SnipDislike.objects.filter(author=user, snip_id__in=ids).values_list("snip_id", flat=True)
        ),
    }


def _is_following(user, author_id):
    if not user.is_authenticated or not author_id:
        return False
    return Creek.objects.filter(
        author=user, account__user_id=author_id, account__user__is_active=True
    ).exists()


def _snip_dislike_count(snip):
    """Fresh denormalized dislike count without reloading the row."""
    return SnipDislike.objects.filter(snip_id=snip.id).count()


class SnipFeed(APIView):
    """Recommended snips feed (with modes), backed by the ranking engine.

    ``mode``:
      * ``recommended`` (default) -- personalized ranking with diversity,
        exploration, session awareness, and negative-feedback suppression.
      * ``trending``             -- momentum (views/likes velocity), not personal.
      * ``fresh``                -- newest-first with light diversity.
      * ``following``            -- snips from creators the user creeks.

    Keeps the legacy ``results``/``page``/``page_size``/``count`` contract and
    adds ``mode``, ``youtube_error``, and a per-item ``reason`` explaining
    "why am I seeing this".
    """
    permission_classes = [AllowAny]

    FEED_POOL_LIMIT = 120

    def get(self, request):
        try:
            page = max(int(request.query_params.get("page", 1)), 1)
        except (TypeError, ValueError):
            page = 1
        try:
            page_size = min(max(int(request.query_params.get("page_size", 12)), 1), 50)
        except (TypeError, ValueError):
            page_size = 12

        mode = request.query_params.get("mode", "recommended").strip().lower()
        if mode not in ("recommended", "trending", "fresh", "following"):
            mode = "recommended"

        exclude_ids = _parse_id_list(request.query_params.get("exclude_ids", ""))

        user = request.user if request.user.is_authenticated else None
        if mode == "following" and not user:
            return Response({
                "results": [], "page": page, "page_size": page_size,
                "count": 0, "mode": mode, "youtube_error": youtube_api_status(),
            }, status=200)

        ctx = snips_rank.build_user_context(user)

        if mode == "recommended":
            native_entries = snips_rank.build_recommended(
                user, ctx, limit=self.FEED_POOL_LIMIT, exclude_ids=exclude_ids
            )
            youtube_items = build_youtube_snips_feed(
                request.user,
                interest_categories=ctx.get("category_scores"),
                history_keywords=recent_watch_keywords(request.user),
                tag_queries=list(ctx.get("tag_scores", {}).keys()),
            )
        elif mode == "trending":
            native_entries = snips_rank.build_trending(
                user, limit=self.FEED_POOL_LIMIT, exclude_ids=exclude_ids
            )
            youtube_items = build_youtube_snips_feed(request.user, limit=40)
        elif mode == "fresh":
            native_entries = snips_rank.build_fresh(
                user, limit=self.FEED_POOL_LIMIT, exclude_ids=exclude_ids
            )
            youtube_items = build_youtube_snips_feed(request.user, limit=40)
        else:
            native_entries = snips_rank.build_following(
                user, limit=self.FEED_POOL_LIMIT, exclude_ids=exclude_ids
            )
            youtube_items = build_youtube_follows_feed(user, limit=40, shorts=True)

        native_items = _serialize_snips(native_entries, request)
        combined = interleave_feed(native_items, youtube_items, ratio=HYBRID_YOUTUBE_RATIO)

        total = len(combined)
        start = (page - 1) * page_size
        results = combined[start:start + page_size]

        return Response({
            "results": results,
            "page": page,
            "page_size": page_size,
            "count": total,
            "mode": mode,
            "youtube_error": youtube_api_status(),
        }, status=200)


class WatchSnip(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        snip_id = request.query_params.get("id")
        if not snip_id:
            return Response({"detail": "Snip ID required"}, status=400)

        # A live YouTube Shorts ID (e.g. /snips/{yt_id}) is streamed straight
        # from the Data API through the iframe embed, exactly like videos.
        if validate_youtube_id(snip_id):
            yt_item = get_youtube_video_details(snip_id)
            if not yt_item:
                return Response({"detail": "Snip not found"}, status=404)
            is_liked, like_count, yt_like_count, creek_like_count = youtube_snip_like_state(request, snip_id)
            return Response({
                "id": yt_item.get("youtube_video_id") or snip_id,
                "title": yt_item["title"],
                "description": yt_item["description"],
                "video": None,
                "thumbnail": yt_item["thumbnail"],
                "timestamp": yt_item["timestamp"],
                "author": yt_item["author"],
                "author_id": None,
                "author_avatar": yt_item.get("author_avatar") or None,
                "author_active": True,
                "view_count": yt_item["view_count"],
                "like_count": like_count,
                "youtube_like_count": yt_like_count,
                "creek_like_count": creek_like_count,
                "is_liked": is_liked,
                "source_type": "YOUTUBE",
                "content_type": yt_item.get("content_type") or "SNIP",
                "duration": yt_item.get("duration") or 0,
                "youtube_video_id": yt_item.get("youtube_video_id") or snip_id,
                "youtube_channel_id": yt_item.get("youtube_channel_id") or "",
                "youtube_channel_name": yt_item.get("youtube_channel_name") or "",
                "embed_url": youtube_embed_url(snip_id),
            }, status=200)

        try:
            snip = Snip.objects.filter(author__is_active=True).select_related("author").get(id=snip_id)
        except Snip.DoesNotExist:
            return Response({"detail": "Snip not found"}, status=404)

        if snip.visibility == "private" and snip.author != request.user:
            return Response({"detail": "Snip not found"}, status=404)

        # Record view with spam prevention (authenticated users only)
        if request.user.is_authenticated:
            record_view(request.user, snip=snip)
            record_tag_interest_from_watch(request.user, snip=snip)
            if snip.category_id:
                record_category_interest(request.user, snip.category.slug)
        else:
            Snip.objects.filter(id=snip.id).update(view_count=F("view_count") + 1)
            snip.refresh_from_db(fields=["view_count"])

        if request.user.is_authenticated:
            context = {"request": request}
            state = _state_for_user(request.user, [snip.id])
            context.update({
                "snip_liked_ids": state["liked"],
                "snip_saved_ids": state["saved"],
                "snip_disliked_ids": state["disliked"],
                "snip_followed_author_ids": {snip.author_id} if _is_following(request.user, snip.author_id) else set(),
            })
        else:
            context = {"request": request}

        return Response({
            **SnipSerializer(snip, context=context).data,
        }, status=200)


class LikeSnip(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        snip_id = request.data.get("id")
        if not snip_id:
            return Response({"detail": "Snip ID required"}, status=400)

        # Likes on live YouTube Shorts are stored against the lightweight
        # YOUTUBE Video row so they persist; the count mirrors the YouTube
        # watch page (YouTube likes + CreekTube likes).
        if validate_youtube_id(snip_id):
            video = ensure_youtube_video(snip_id, request.user)
            if not video:
                return Response({"detail": "Snip not found"}, status=404)
            like, created = Like.objects.get_or_create(video=video, author=request.user)
            creek_like_count = Like.objects.filter(video=video).count()
            yt_like_count = youtube_like_counts_for([snip_id]).get(snip_id, 0)
            if not created:
                like.delete()
                creek_like_count = Like.objects.filter(video=video).count()
                return Response({
                    "is_liked": False,
                    "like_count": max(yt_like_count + creek_like_count, 0),
                    "youtube_like_count": yt_like_count,
                    "creek_like_count": creek_like_count,
                }, status=200)
            return Response({
                "is_liked": True,
                "like_count": yt_like_count + creek_like_count,
                "youtube_like_count": yt_like_count,
                "creek_like_count": creek_like_count,
            }, status=201)

        snip = get_object_or_404(Snip.objects.filter(author__is_active=True), id=snip_id)
        # Liking and disliking are mutually exclusive.
        had_dislike = SnipDislike.objects.filter(author=request.user, snip=snip).delete()[0] > 0
        like, created = SnipLike.objects.get_or_create(author=request.user, snip=snip)

        if not created:
            like.delete()
            Snip.objects.filter(id=snip.id).update(like_count=F("like_count") - 1)
            snip.refresh_from_db(fields=["like_count"])
            return Response({
                "is_liked": False,
                "is_disliked": False,
                "like_count": max(snip.like_count, 0),
                "dislike_count": _snip_dislike_count(snip),
            }, status=200)

        Snip.objects.filter(id=snip.id).update(like_count=F("like_count") + 1)
        snip.refresh_from_db(fields=["like_count"])
        if had_dislike:
            Snip.objects.filter(id=snip.id).update(dislike_count=F("dislike_count") - 1)

        # Learn the positive signal: a like is stronger than a plain watch.
        record_tag_interest_from_watch(request.user, snip=snip)
        if snip.category_id:
            record_category_interest(request.user, snip.category.slug, boost=ranking.LIKE_BOOST)

        return Response({
            "is_liked": True,
            "is_disliked": False,
            "like_count": snip.like_count,
            "dislike_count": _snip_dislike_count(snip),
        }, status=201)


class DislikeSnip(APIView):
    """Dislike a snip: mutually exclusive with like, and teaches the ranker."""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        snip_id = request.data.get("id")
        if not snip_id:
            return Response({"detail": "Snip ID required"}, status=400)

        # Live YouTube Shorts are streamed via the Data API; dislikes are not
        # stored against the lightweight Video row (only likes are).
        if validate_youtube_id(snip_id):
            return Response({"detail": "Dislikes are only available on CreekTube snips"}, status=400)

        snip = get_object_or_404(Snip.objects.filter(author__is_active=True), id=snip_id)

        dislike, created = SnipDislike.objects.get_or_create(author=request.user, snip=snip)

        if not created:
            dislike.delete()
            Snip.objects.filter(id=snip.id).update(dislike_count=F("dislike_count") - 1)
            snip.refresh_from_db(fields=["dislike_count"])
            return Response({
                "is_disliked": False,
                "is_liked": False,
                "dislike_count": max(snip.dislike_count, 0),
                "like_count": snip.like_count,
            }, status=200)

        # Mutual exclusivity: disliking removes a prior like.
        like_count_delta = 0
        had_like = SnipLike.objects.filter(author=request.user, snip=snip).delete()[0] > 0
        if had_like:
            like_count_delta = -1

        Snip.objects.filter(id=snip.id).update(
            dislike_count=F("dislike_count") + 1,
            like_count=F("like_count") + like_count_delta,
        )

        if snip.category_id:
            record_category_interest(request.user, snip.category.slug, boost=ranking.DISLIKE_BOOST)

        snip.refresh_from_db(fields=["dislike_count", "like_count"])
        return Response({
            "is_disliked": True,
            "is_liked": False,
            "dislike_count": max(snip.dislike_count, 0),
            "like_count": max(snip.like_count, 0),
        }, status=201)


class SaveSnip(APIView):
    """Save/unsave a snip to the user's "Saved snips" collection."""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        snip_id = request.data.get("id")
        if not snip_id:
            return Response({"detail": "Snip ID required"}, status=400)

        if validate_youtube_id(snip_id):
            return Response({"detail": "Saving is only available on CreekTube snips"}, status=400)

        snip = get_object_or_404(Snip.objects.filter(author__is_active=True), id=snip_id)
        save, created = SnipSave.objects.get_or_create(author=request.user, snip=snip)

        if not created:
            save.delete()
            return Response({"is_saved": False}, status=200)

        record_tag_interest_from_watch(request.user, snip=snip)
        if snip.category_id:
            record_category_interest(request.user, snip.category.slug, boost=ranking.SAVE_BOOST)
        return Response({"is_saved": True}, status=201)


class GetSavedSnips(APIView):
    """The current user's saved snips, newest save first."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        saved = (
            SnipSave.objects.filter(author=request.user, snip__is_approved=True)
            .select_related("snip", "snip__author")
            .order_by("-created_at")
        )
        snips = [s.snip for s in saved]
        return Response({
            "results": _serialize_snips(
                [{"snip": s, "score": 0, "reason": "Saved by you"} for s in snips],
                request,
            ),
        }, status=200)


class SnipFeedbackView(APIView):
    """Negative/report/share feedback that shapes the recommendation engine.

    ``kind`` is one of: ``not_interested`` (hide this snip), ``not_interested_creator``
    (fewer from this creator), ``not_interested_topic`` (fewer of this tag),
    ``share`` (a share event), ``report`` (abuse/rule violation; goes to review).
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        snip_id = request.data.get("id")
        kind = (request.data.get("kind") or "").strip().lower()
        if not snip_id:
            return Response({"detail": "Snip ID required"}, status=400)
        if kind not in SnipFeedback.KINDS:
            return Response({"detail": f"Unknown feedback kind: {kind}"}, status=400)

        if validate_youtube_id(snip_id):
            return Response({"detail": "Feedback is only available on CreekTube snips"}, status=400)

        snip = get_object_or_404(Snip.objects.filter(author__is_active=True), id=snip_id)

        if kind == SnipFeedback.REPORT:
            reason = (request.data.get("reason") or "").strip()[:500]
            if not reason:
                return Response({"detail": "A report reason is required"}, status=400)

        creator = snip.author if kind == SnipFeedback.NOT_INTERESTED_CREATOR else None
        tag = None
        if kind == SnipFeedback.NOT_INTERESTED_TOPIC:
            tag_name = (request.data.get("tag") or "").strip().lstrip("#")
            if tag_name:
                tag = Tag.objects.filter(name__iexact=tag_name).first()

        SnipFeedback.objects.create(
            author=request.user,
            snip=snip,
            kind=kind,
            creator=creator,
            tag=tag,
            reason=request.data.get("reason", "")[:500] if kind == SnipFeedback.REPORT else "",
        )

        if kind == SnipFeedback.NOT_INTERESTED_CREATOR:
            if snip.category_id:
                record_category_interest(request.user, snip.category.slug, boost=ranking.DISLIKE_BOOST)
            record_tag_interest(request.user, tag_names_for(snip), boost=ranking.DISLIKE_BOOST)

        return Response({"status": "ok", "kind": kind}, status=201)


class RelatedSnips(APIView):
    """Rabbit hole: snips related to a seed snip (tag -> category -> recent)."""
    permission_classes = [AllowAny]

    def get(self, request):
        snip_id = request.query_params.get("id")
        if not snip_id:
            return Response({"detail": "Snip ID required"}, status=400)
        try:
            limit = max(1, min(int(request.query_params.get("limit", 10)), 20))
        except (TypeError, ValueError):
            limit = 10

        if validate_youtube_id(snip_id):
            return Response({"results": []}, status=200)

        seed = get_object_or_404(
            Snip.objects.filter(author__is_active=True).prefetch_related("tags"),
            id=snip_id,
        )
        related = snips_rank.related_snips(seed, limit=limit)
        items = [
            {
                "snip": r,
                "score": 0,
                "reason": snips_rank.reason_for_related(seed, r),
            }
            for r in related
        ]
        return Response({"results": _serialize_snips(items, request)}, status=200)


class SnipSearch(APIView):
    """Search snips by title, description, hashtags, or creator username."""
    permission_classes = [AllowAny]

    def get(self, request):
        q = (request.query_params.get("q") or "").strip()
        if not q:
            return Response({"results": [], "query": q}, status=200)
        try:
            limit = max(1, min(int(request.query_params.get("limit", 12)), 50))
        except (TypeError, ValueError):
            limit = 12

        qs = (
            Snip.objects.filter(
                is_approved=True, visibility="public", author__is_active=True
            )
            .select_related("author", "category")
            .prefetch_related("tags")
            .filter(
                Q(title__icontains=q)
                | Q(description__icontains=q)
                | Q(author__username__icontains=q)
                | Q(tags__name__icontains=q)
            )
            .distinct()
            .order_by("-timestamp")
        )

        results = list(qs[:limit])
        return Response({
            "results": _serialize_snips(
                [{"snip": s, "score": 0, "reason": None} for s in results],
                request,
            ),
            "query": q,
        }, status=200)


class GetOwnSnips(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        snips = Snip.objects.filter(author=request.user).order_by("-timestamp")
        serializer = SnipSerializer(snips, many=True, context={"request": request})
        return Response(serializer.data, status=200)


class SnipDelete(APIView):
    permission_classes = [IsAuthenticated]

    def delete(self, request, snip_id):
        snip = get_object_or_404(Snip, id=snip_id, author=request.user)
        snip.delete()
        return Response(status=204)


# ---------------------------
# Snip Comments
# ---------------------------
@method_decorator(csrf_exempt, name='dispatch')
class SnipCommentList(APIView):
    def get(self, request):
        snip_id = request.query_params.get('snip_id')
        if not snip_id:
            return Response({'detail': 'snip_id required'}, status=status.HTTP_400_BAD_REQUEST)

        # Live YouTube Shorts surface their read-only YouTube comments.
        if validate_youtube_id(snip_id):
            return Response(youtube_comments(snip_id), status=status.HTTP_200_OK)

        snip = get_object_or_404(Snip.objects.filter(author__is_active=True), id=snip_id)
        top_level = Comment.objects.filter(snip=snip, parent=None, author__is_active=True).select_related('author').order_by('-is_pinned', '-timestamp')
        serializer = CommentSerializer(top_level, many=True, context={'request': request})
        return Response(serializer.data, status=status.HTTP_200_OK)


@method_decorator(csrf_exempt, name='dispatch')
class UploadSnipComment(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        author = request.user
        comment_text = request.data.get('comment')
        snip_id = request.data.get('snip_id')
        parent_id = request.data.get('parent_id')

        if not comment_text:
            return Response({'detail': 'No comment provided'}, status=status.HTTP_400_BAD_REQUEST)

        # YouTube comments are read-only: the user comments on YouTube, not here.
        if validate_youtube_id(snip_id):
            return Response(
                {'detail': 'YouTube comments are read-only'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        snip = get_object_or_404(Snip.objects.filter(author__is_active=True), id=snip_id)

        cutoff = timezone.now() - COMMENT_SPAM_WINDOW
        recent = Comment.objects.filter(
            author=author, snip=snip, timestamp__gte=cutoff
        ).count()
        if recent >= COMMENT_SPAM_LIMIT:
            return Response(
                {'detail': 'You are posting too many comments. Please wait before posting again.'},
                status=status.HTTP_429_TOO_MANY_REQUESTS
            )

        parent = None
        if parent_id:
            parent = get_object_or_404(Comment, id=parent_id, snip=snip)

        comment = Comment.objects.create(author=author, snip=snip, text=comment_text, parent=parent)
        return Response({
            'detail': 'Comment added',
            'comment': CommentSerializer(comment, context={'request': request}).data
        }, status=status.HTTP_201_CREATED)


@method_decorator(csrf_exempt, name='dispatch')
class PinSnipComment(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        comment_id = request.data.get("comment_id")
        if not comment_id:
            return Response({"detail": "Comment ID required"}, status=status.HTTP_400_BAD_REQUEST)

        comment = get_object_or_404(Comment, id=comment_id)
        if not comment.snip or comment.snip.author != request.user:
            return Response({"detail": "Only the snip author can pin comments"}, status=status.HTTP_403_FORBIDDEN)

        comment.is_pinned = not comment.is_pinned
        comment.save(update_fields=['is_pinned'])
        return Response({"is_pinned": comment.is_pinned}, status=status.HTTP_200_OK)


class SnipStudioComments(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        snips = Snip.objects.filter(author=request.user)
        comments = Comment.objects.filter(snip__in=snips).select_related('author', 'snip').order_by('-timestamp')
        data = [{
            'id': c.id,
            'text': c.text,
            'author': c.author.username,
            'snip_id': c.snip.id,
            'snip_title': c.snip.title,
            'timestamp': c.timestamp,
            'is_pinned': c.is_pinned,
        } for c in comments]
        return Response(data, status=200)

    def delete(self, request):
        comment_id = request.query_params.get('id')
        if not comment_id:
            return Response({'detail': 'Comment ID required'}, status=400)
        try:
            comment = Comment.objects.select_related('snip').get(
                id=comment_id, snip__author=request.user
            )
            comment.delete()
            return Response(status=204)
        except Comment.DoesNotExist:
            return Response({'detail': 'Comment not found'}, status=404)


# ---------------------------
# Snip Retention Tracking
# ---------------------------
class TrackSnipRetention(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        snip_id = request.data.get("snip_id")
        duration = request.data.get("duration", 0)

        if not snip_id:
            return Response({"detail": "Snip ID required"}, status=400)

        try:
            duration = max(0, min(int(duration), 86400))
        except (TypeError, ValueError):
            duration = 0

        # Live YouTube Shorts aren't tracked in the retention tables.
        if validate_youtube_id(snip_id):
            return Response({"status": "ok"}, status=200)

        snip = get_object_or_404(Snip.objects.filter(author__is_active=True), id=snip_id)
        event = (
            WatchEvent.objects.filter(user=request.user, snip=snip)
            .order_by('-timestamp')
            .first()
        )
        if event and event.duration_watched < duration:
            event.duration_watched = duration
            event.save(update_fields=['duration_watched'])

        # Learn interest from how much of the clip was actually watched:
        # a full watch is a strong positive, a near-immediate skip is a signal
        # to downweight that topic/creator slightly.
        duration_watched = max(0, min(int(duration), 86400))
        snip_duration = snip.duration or 0
        completion = (duration_watched / snip_duration) if snip_duration else 0
        if completion >= 0.8 and snip.category_id:
            record_category_interest(request.user, snip.category.slug, boost=ranking.WATCH_BOOST)
        elif 0 < completion <= 0.1 and snip_duration >= snips_rank.ABANDON_MIN_SECONDS:
            if snip.category_id:
                record_category_interest(request.user, snip.category.slug, boost=-0.25)
            record_tag_interest(request.user, tag_names_for(snip), boost=-0.25)

        return Response({"status": "ok"}, status=200)


# ---------------------------
# Analytics API
# ---------------------------


class ChannelAnalytics(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        
        user = request.user
        period = request.query_params.get("period", "7d")

        period_map = {"24h": 1, "7d": 7, "30d": 30, "90d": 90}
        days = period_map.get(period, 7)
        since = timezone.now() - timedelta(days=days)

        my_videos = Video.objects.filter(author=user)
        my_snips = Snip.objects.filter(author=user)
        v_ids = list(my_videos.values_list('id', flat=True))
        s_ids = list(my_snips.values_list('id', flat=True))

        video_events = WatchEvent.objects.filter(video_id__in=v_ids, timestamp__gte=since) if v_ids else WatchEvent.objects.none()
        snip_events = WatchEvent.objects.filter(snip_id__in=s_ids, timestamp__gte=since) if s_ids else WatchEvent.objects.none()

        # Daily views
        video_daily = list(
            video_events.annotate(day=TruncDate('timestamp'))
            .values('day').annotate(v=Count('id'), u=Count('user', distinct=True))
            .order_by('day')
        )
        snip_daily = list(
            snip_events.annotate(day=TruncDate('timestamp'))
            .values('day').annotate(v=Count('id'), u=Count('user', distinct=True))
            .order_by('day')
        )

        # Per-content retention
        video_ret = list(
            video_events.values('video_id', 'video__title')
            .annotate(avg_dur=Avg('duration_watched'), views=Count('id'), viewers=Count('user', distinct=True))
            .order_by('-views')[:10]
        )
        snip_ret = list(
            snip_events.values('snip_id', 'snip__title')
            .annotate(avg_dur=Avg('duration_watched'), views=Count('id'), viewers=Count('user', distinct=True))
            .order_by('-views')[:10]
        )

        # Top content
        top_v = list(my_videos.order_by('-view_count')[:5].values('id', 'title', 'view_count', 'timestamp'))
        top_s = list(my_snips.order_by('-view_count')[:5].values('id', 'title', 'view_count', 'timestamp'))

        return Response({
            "totals": {
                "video_views": my_videos.aggregate(s=Sum('view_count'))['s'] or 0,
                "snip_views": my_snips.aggregate(s=Sum('view_count'))['s'] or 0,
                "total_views": (my_videos.aggregate(s=Sum('view_count'))['s'] or 0) + (my_snips.aggregate(s=Sum('view_count'))['s'] or 0),
                "video_count": my_videos.count(),
                "snip_count": my_snips.count(),
            },
            "views_over_time": {
                "videos": [{"date": str(d['day']), "total_views": d['v'], "unique_viewers": d['u']} for d in video_daily],
                "snips": [{"date": str(d['day']), "total_views": d['v'], "unique_viewers": d['u']} for d in snip_daily],
            },
            "retention": {
                "videos": [{"id": r['video_id'], "title": r['video__title'], "avg_duration": round(r['avg_dur'] or 0, 1), "total_views": r['views'], "unique_viewers": r['viewers']} for r in video_ret],
                "snips": [{"id": r['snip_id'], "title": r['snip__title'], "avg_duration": round(r['avg_dur'] or 0, 1), "total_views": r['views'], "unique_viewers": r['viewers']} for r in snip_ret],
            },
            "top_content": {"videos": top_v, "snips": top_s},
        }, status=200)


class WatchHistory(APIView):
    """Watch history for the current user (from WatchEvents)."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            limit = max(1, min(int(request.query_params.get("limit", 50)), 100))
        except (TypeError, ValueError):
            limit = 50

        videos = (
            Video.objects.filter(
                watch_events__user=request.user,
                is_approved=True,
                author__is_active=True,
            )
            .annotate(last_watched=Max('watch_events__timestamp'))
            .order_by('-last_watched')[:limit]
        )
        snips = (
            Snip.objects.filter(
                watch_events__user=request.user,
                is_approved=True,
                author__is_active=True,
            )
            .annotate(last_watched=Max('watch_events__timestamp'))
            .order_by('-last_watched')[:limit]
        )

        video_data = VideoSerializer(videos, many=True, context={'request': request}).data
        snip_data = SnipSerializer(snips, many=True, context={'request': request}).data
        for item in snip_data:
            item['is_snip'] = True

        return Response({"videos": video_data, "snips": snip_data}, status=200)
