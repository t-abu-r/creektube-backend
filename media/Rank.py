"""
Feed ranking for creektube.

Replaces the old "strict category bucket" ordering with a weighted composite
score, so the feed blends interest match, engagement, recency, and follows
instead of letting one signal completely dominate the others.

score(video) = w_interest   * interest_affinity(video, user)
             + w_engagement * engagement_score(video)
             + w_recency    * recency_score(video)
             + w_creek      * creek_bonus(video, user)

             Maybe....

             eeiosdjnsodjnsodjnsodjnsodjnsodjnsodjnsod
             Idk wut to do

             
"""
import math

from django.utils import timezone

# ---------------------------------------------------------------------------
# Tunable weights. Keep these in one place so the feed can be re-tuned
# without touching view logic.
# ---------------------------------------------------------------------------
WEIGHTS = {
    "interest": 3.0,     # how well the video's category matches the user's interests
    "engagement": 2.0,   # net likes vs dislikes, log-dampened so virality doesn't dominate forever
    "recency": 2.5,      # freshness of the video
    "creek": 1.5,         # bonus for videos from creators the user follows ("creeked")
}

# A video's recency contribution halves every RECENCY_HALF_LIFE_HOURS hours.
RECENCY_HALF_LIFE_HOURS = 48

# Categories the user hasn't interacted with still get a small non-zero
# affinity, otherwise a brand-new account (empty `categories` dict) or a
# user who only ever picked one category would never see anything else.
EXPLORATION_FLOOR = 0.15

# How much a single watch / dislike nudges a category score. Kept small and
# symmetric so scores can rise AND fall, instead of only ever climbing.
WATCH_BOOST = 1.0
DISLIKE_PENALTY = 1.0
MIN_CATEGORY_SCORE = 0
MAX_CATEGORY_SCORE = 100


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
    exactly 0.5 at RECENCY_HALF_LIFE_HOURS (true half-life, hence the ln(2))."""
    age_hours = max((timezone.now() - timestamp).total_seconds() / 3600, 0)
    return math.exp(-math.log(2) * age_hours / RECENCY_HALF_LIFE_HOURS)


def score_video(video, user_interests, creeked_author_ids, likes_count=None, dislikes_count=None):
    """
    Compute a single composite score for a video.

    `video` is expected to have `.category`, `.author_id`, `.timestamp`, and
    (unless overridden) prefetched/annotated like/dislike counts.
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

    return (
        WEIGHTS["interest"] * interest
        + WEIGHTS["engagement"] * engagement
        + WEIGHTS["recency"] * recency
        + WEIGHTS["creek"] * creek_bonus
    )


def rank_videos(videos, user_interests, creeked_author_ids):
    """Sort an iterable of (already-annotated) videos by composite score, desc."""
    scored = [(score_video(v, user_interests, creeked_author_ids), v) for v in videos]
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [v for _, v in scored]


def adjust_category_score(categories: dict, category_slug: str, delta: int) -> dict:
    """
    Bump a category score up or down, clamped to [MIN_CATEGORY_SCORE, MAX_CATEGORY_SCORE].
    Used for both positive (watch) and negative (dislike) feedback so interest
    scores can actually fall, not just climb forever.
    """
    if not category_slug:
        return categories
    current = categories.get(category_slug, 0)
    categories[category_slug] = max(MIN_CATEGORY_SCORE, min(MAX_CATEGORY_SCORE, current + delta))
    return categories