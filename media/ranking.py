"""
Feed ranking for creektube.

Replaces the old "strict category bucket" ordering with a weighted composite
score, so the feed blends interest match, engagement, recency, follows,
and co-watch signals instead of letting one signal completely dominate.

score(video) = w_interest   * interest_affinity(video, user)
             + w_engagement * engagement_score(video)
             + w_recency    * recency_score(video)
             + w_creek      * creek_bonus(video, user)
             + w_cowatch    * cowatch_affinity(video, user)
"""
import math
from collections import defaultdict

from django.utils import timezone

# ---------------------------------------------------------------------------
# Tunable weights. Keep these in one place so the feed can be re-tuned
# without touching view logic.
# ---------------------------------------------------------------------------
WEIGHTS = {
    "interest": 3.0,
    "engagement": 2.0,
    "recency": 2.5,
    "creek": 1.5,
    "cowatch": 2.0,
}

RECENCY_HALF_LIFE_HOURS = 48
EXPLORATION_FLOOR = 0.15
WATCH_BOOST = 1.0
DISLIKE_PENALTY = 1.0
MIN_CATEGORY_SCORE = 0
MAX_CATEGORY_SCORE = 100

# How many recent watch events to consider when computing co-watch affinity.
COWATCH_RECENT_LIMIT = 50
# How many co-watchers to consider when computing affinity for a single video.
COWATCH_TOP_NEIGHBORS = 30
# Maximum number of co-watch videos to collect per candidate.
COWATCH_MAX_CANDIDATES = 20


def interest_affinity(category_slug, user_interests):
    """Normalize a user's interest score for a category into ~[0, 1]."""
    if not user_interests:
        return EXPLORATION_FLOOR

    max_score = max(user_interests.values())
    if max_score <= 0:
        return EXPLORATION_FLOOR

    raw = user_interests.get(category_slug, 0)
    if raw <= 0:
        return EXPLORATION_FLOOR

    return raw / max_score


def engagement_score(likes, dislikes):
    """Net likes vs dislikes, log-dampened and sign-preserving."""
    net = likes - dislikes
    if net == 0:
        return 0.0
    return math.copysign(math.log1p(abs(net)), net)


def recency_score(timestamp):
    """Exponential decay based on video age; 1.0 for a brand new video,
    exactly 0.5 at RECENCY_HALF_LIFE_HOURS."""
    age_hours = max((timezone.now() - timestamp).total_seconds() / 3600, 0)
    return math.exp(-math.log(2) * age_hours / RECENCY_HALF_LIFE_HOURS)


def cowatch_affinity(video_id, user_recent_video_ids, cowatch_map):
    """
    Score how well a candidate video co-watches with the user's recent history.

    `cowatch_map` is a dict: {candidate_video_id: co_watch_score} where the
    score represents how frequently this candidate was watched alongside videos
    the user recently watched.

    Returns a value in ~[0, 1] by normalizing against the best possible score.
    """
    if not user_recent_video_ids or not cowatch_map:
        return 0.0

    raw_score = cowatch_map.get(video_id, 0.0)
    if raw_score <= 0:
        return 0.0

    max_possible = max(cowatch_map.values()) if cowatch_map else 1.0
    if max_possible <= 0:
        return 0.0

    return min(raw_score / max_possible, 1.0)


def build_cowatch_map(user_recent_video_ids, all_video_ids):
    """
    On-the-fly co-watch computation.

    Given a user's recent video IDs, find other videos that were watched by
    the same users/sessions. Returns a dict mapping video_id -> co_watch_score.

    This is an on-the-fly query approach, suitable for the current small/medium
    catalog size (up to ~10k videos). For larger scale, this would be replaced
    by a precomputed co-watch table.

    Imports are deferred to avoid circular imports at module level.
    """
    from .models import WatchEvent
    from django.db.models import Count as QCount

    if not user_recent_video_ids:
        return {}

    # Find other users who watched any of the user's recent videos
    recent_user_set = set(user_recent_video_ids)

    # Get watch events for videos the user watched, to find co-watchers
    co_watcher_events = (
        WatchEvent.objects.filter(
            video_id__in=user_recent_video_ids,
            user__isnull=False,
        )
        .values_list('user_id', flat=True)
    )
    co_watcher_ids = set(co_watcher_events) - {None}
    if not co_watcher_ids:
        return {}

    # Cap co-watchers to prevent huge queries
    co_watcher_ids = list(co_watcher_ids)[:200]

    # Find what else those co-watchers watched
    other_watches = (
        WatchEvent.objects.filter(
            user_id__in=co_watcher_ids,
        )
        .exclude(video_id__in=recent_user_set)
        .values('video_id')
        .annotate(watch_count=QCount('id'))
        .order_by('-watch_count')[:COWATCH_MAX_CANDIDATES * 3]
    )

    cowatch_scores = defaultdict(float)
    for entry in other_watches:
        vid = entry['video_id']
        count = entry['watch_count']
        cowatch_scores[vid] += count

    # Recency-weight recent co-watches more heavily
    recent_events = (
        WatchEvent.objects.filter(
            user_id__in=co_watcher_ids,
            video_id__in=[int(v) for v in cowatch_scores.keys()],
            timestamp__gte=timezone.now() - timezone.timedelta(days=7),
        )
        .values('video_id')
        .annotate(recent_count=QCount('id'))
    )
    for entry in recent_events:
        vid = entry['video_id']
        cowatch_scores[vid] += entry['recent_count'] * 0.5  # recency bonus

    # Normalize to [0, 1]
    if not cowatch_scores:
        return {}

    max_score = max(cowatch_scores.values())
    if max_score > 0:
        return {vid: score / max_score for vid, score in cowatch_scores.items()}

    return {}


def score_video(video, user_interests, creeked_author_ids, likes_count=None,
                dislikes_count=None, cowatch_map=None, user_recent_video_ids=None):
    """
    Compute a single composite score for a video.
    """
    category_slug = video.category.slug if video.category_id else None
    interest = interest_affinity(category_slug, user_interests)

    likes = likes_count if likes_count is not None else getattr(video, "num_likes", None)
    dislikes = dislikes_count if dislikes_count is not None else getattr(video, "num_dislikes", None)
    likes = likes if likes is not None else 0
    dislikes = dislikes if dislikes is not None else 0
    engagement = engagement_score(likes, dislikes)

    recency = recency_score(video.timestamp)

    creek_bonus = 1.0 if video.author_id in creeked_author_ids else 0.0

    cowatch = cowatch_affinity(
        video.id,
        user_recent_video_ids or [],
        cowatch_map or {},
    )

    return (
        WEIGHTS["interest"] * interest
        + WEIGHTS["engagement"] * engagement
        + WEIGHTS["recency"] * recency
        + WEIGHTS["creek"] * creek_bonus
        + WEIGHTS["cowatch"] * cowatch
    )


def rank_videos(videos, user_interests, creeked_author_ids,
                 cowatch_map=None, user_recent_video_ids=None):
    """Sort an iterable of (already-annotated) videos by composite score, desc."""
    scored = [
        (score_video(v, user_interests, creeked_author_ids,
                     cowatch_map=cowatch_map,
                     user_recent_video_ids=user_recent_video_ids), v)
        for v in videos
    ]
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [v for _, v in scored]


def adjust_category_score(categories: dict, category_slug: str, delta: int) -> dict:
    """
    Bump a category score up or down, clamped to [MIN_CATEGORY_SCORE, MAX_CATEGORY_SCORE].
    """
    if not category_slug:
        return categories
    current = categories.get(category_slug, 0)
    categories[category_slug] = max(MIN_CATEGORY_SCORE, min(MAX_CATEGORY_SCORE, current + delta))
    return categories


def get_user_recent_video_ids(user, limit=COWATCH_RECENT_LIMIT):
    """Get a user's most recent video IDs from WatchEvent history."""
    from .models import WatchEvent
    return list(
        WatchEvent.objects.filter(user=user)
        .order_by('-timestamp')
        .values_list('video_id', flat=True)[:limit]
    )

