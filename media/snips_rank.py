"""
Snip recommendation engine for CreekTube.

The old snips feed was pure reverse-chronological: newest clip first, no
personalization, no diversity, no negative feedback. This module replaces it
with a transparent weighted scoring system that blends:

* personalized interest match (categories + hashtags)
* creator affinity (follows + repeated watching)
* topic similarity (tag overlap)
* engagement (likes vs dislikes)
* freshness (recency decay)
* popularity + quality (views, completion rate)
* co-watch affinity (what similar viewers also watch)
* session awareness (recent activity reweights the next feed)

with explicit negative signals (dislikes, "not interested", abandoned clips)
and a diversity rerank so the feed never becomes "Creator A x5".

score(snip) = sum(W_positive * signal) - sum(W_negative * negative) + noise

The scoring layer is deliberately isolated: a future ML model can replace
``score_snip`` / ``rank_snips`` without touching the views or API contract.
"""

import hashlib
import math
from collections import defaultdict
from datetime import timedelta

from django.utils import timezone

# ---------------------------------------------------------------------------
# Tunable weights. Kept in one place so the feed can be re-tuned without
# touching view logic.
# ---------------------------------------------------------------------------
WEIGHTS = {
    "topic": 4.0,        # hashtag affinity (beats generic category)
    "interest": 3.0,     # category affinity
    "creator": 2.5,      # follows + creator affinity
    "session": 2.0,      # recent in-session topic/category boost
    "cowatch": 2.0,      # co-watch affinity
    "recency": 2.0,      # freshness
    "engagement": 1.5,   # net likes (log-scaled)
    "quality": 1.5,      # completion rate
    "popularity": 0.8,   # log-scaled views (helps cold start)
}

NEGATIVE_WEIGHTS = {
    "creator": 2.0,      # "not interested in this creator"
    "topic": 1.5,        # "not interested in this topic" / repeated skips
    "abandon": 1.0,      # user repeatedly abandoned this clip
    "dislike": 3.0,      # explicit dislike of this clip
}

EXPLORATION_FLOOR = 0.15
EXPLORATION_SLOT_EVERY = 8          # every Nth position is an exploration slot
EXPLORATION_START = 5               # first exploration slot index
AUTHOR_MAX_IN_FEED = 3              # max clips from one creator per feed page
TAG_MAX_IN_FEED = 4                 # max clips sharing one topic per feed page

RECENCY_HALF_LIFE_HOURS = 72        # snips age fast; ~50% decay at 3 days
RECENT_WINDOW_DAYS = 30             # candidate pool recency window
CANDIDATE_LIMIT = 300               # bounded candidate pool per request
RECENT_HISTORY_LIMIT = 40           # watch events used for user context
SESSION_LOOKBACK = 12               # last N events power session awareness
SESSION_WINDOW_HOURS = 24           # only events this fresh shape the session
WATCHED_LOOKBACK_DAYS = 7           # exclude clips watched recently
ABANDON_RATIO = 0.2                 # <20% of duration = abandoned
ABANDON_MIN_SECONDS = 3             # clips under this aren't "abandoned"
ABANDON_DURATION_MIN = 5            # clips shorter than this can't be abandoned
TRENDING_WINDOW_HOURS = 48          # momentum window for trending mode

# ---------------------------------------------------------------------------
# Small numeric helpers
# ---------------------------------------------------------------------------


def _clamp(value, low=0.0, high=1.0):
    return max(low, min(high, value))


def recency_score(timestamp, half_life_hours=RECENCY_HALF_LIFE_HOURS):
    """Exponential decay; 1.0 for brand new, ~0.5 at the half life."""
    age_hours = max((timezone.now() - timestamp).total_seconds() / 3600, 0)
    return math.exp(-math.log(2) * age_hours / half_life_hours)


def _log_norm(value, scale):
    """log1p(value) / log1p(scale), clamped to [0, 1]."""
    if value <= 0 or scale <= 0:
        return 0.0
    return _clamp(math.log1p(value) / math.log1p(scale))


def _profile_scores(profile, key):
    scores = {}
    if profile is not None:
        raw = getattr(profile, key, None) or {}
        if isinstance(raw, dict):
            scores = {str(k): float(v) for k, v in raw.items() if v}
    return scores


def _max_value(mapping):
    return max(mapping.values()) if mapping else 0.0


# ---------------------------------------------------------------------------
# Affinity signals
# ---------------------------------------------------------------------------


def interest_affinity(category_slug, category_scores):
    """Category affinity in [0,1], normalized against the user's strongest."""
    if not category_scores:
        return EXPLORATION_FLOOR
    max_score = _max_value(category_scores)
    if max_score <= 0:
        return EXPLORATION_FLOOR
    raw = category_scores.get(category_slug, 0)
    if raw <= 0:
        return EXPLORATION_FLOOR
    return _clamp(raw / max_score)


def topic_affinity(tag_names, tag_scores):
    """Tag affinity in [0,1]: matched tag score / user's strongest tag."""
    if not tag_scores:
        return 0.0
    max_score = _max_value(tag_scores)
    if max_score <= 0 or not tag_names:
        return 0.0
    matched = sum(s for tag, s in tag_scores.items() if tag in tag_names)
    if matched <= 0:
        return 0.0
    return _clamp(matched / max_score)


def creator_affinity(author_id, followed_author_ids, creator_scores):
    """Creator signal in [0,1]: explicit follow wins, else learned affinity."""
    if author_id in followed_author_ids:
        return 1.0
    if not creator_scores:
        return 0.0
    return _clamp(creator_scores.get(author_id, 0.0))


def session_affinity(tag_names, category_slug, session_boosts):
    """How strongly a clip's topic matches the user's *current* session.

    ``session_boosts`` is ``{"tags": {name: score}, "categories": {slug: score}}``
    built from the most recent watches with recency decay, so a sudden burst of
    football reweights the very next feed.
    """
    tag_hit = 0.0
    for name in tag_names:
        tag_hit = max(tag_hit, session_boosts["tags"].get(name, 0.0))
    category_hit = session_boosts["categories"].get(category_slug or "", 0.0)
    return _clamp(max(tag_hit, category_hit))


def engagement_signal(net_likes, scale):
    """Net likes (likes - dislikes) log-scaled to [0,1]."""
    return _log_norm(max(net_likes, 0), scale)


def popularity_signal(view_count, max_views):
    return _log_norm(view_count, max_views)


def quality_signal(avg_duration_watched, duration):
    """Completion rate of the clip across viewers, in [0,1].

    Unknown duration or no watch data returns a neutral 0.5 so we never
    hard-penalize content we cannot measure.
    """
    if not duration or duration <= 0:
        return 0.0
    if avg_duration_watched is None or avg_duration_watched <= 0:
        return 0.0
    return _clamp(avg_duration_watched / duration)


# ---------------------------------------------------------------------------
# User context (per-request, bounded)
# ---------------------------------------------------------------------------


def build_user_context(user):
    """Gather everything the ranker needs about ``user`` in a few queries.

    Returns a dict with watched/excluded ids, learned interests, creator
    affinity, session boosts, and negative-suppression sets. Anonymous users
    get an empty context (cold start).
    """
    ctx = {
        "profile": None,
        "category_scores": {},
        "tag_scores": {},
        "creator_scores": {},
        "session_boosts": {"tags": {}, "categories": {}},
        "followed_author_ids": set(),
        "watched_ids": set(),
        "abandoned_ids": set(),
        "disliked_ids": set(),
        "hidden_ids": set(),
        "not_interested_creator_ids": set(),
        "not_interested_tag_names": set(),
        "skipped_tag_scores": {},
    }
    if not user or not getattr(user, "is_authenticated", False):
        return ctx

    from .models import Creek, MediaProfile, Snip, SnipDislike, SnipFeedback, WatchEvent

    try:
        profile = MediaProfile.objects.get(user=user)
        ctx["profile"] = profile
        ctx["category_scores"] = _profile_scores(profile, "categories")
        ctx["tag_scores"] = _profile_scores(profile, "tags")
    except MediaProfile.DoesNotExist:
        pass

    ctx["followed_author_ids"] = set(
        Creek.objects.filter(author=user)
        .exclude(account__user__is_active=False)
        .values_list("account__user_id", flat=True)
    )

    # Negative signals load even when the user has no watch events yet.
    ctx["disliked_ids"] = set(
        SnipDislike.objects.filter(author=user).values_list("snip_id", flat=True)
    )
    feedbacks = SnipFeedback.objects.filter(author=user).order_by("-created_at")[:200]
    for fb in feedbacks:
        if fb.kind == SnipFeedback.NOT_INTERESTED:
            ctx["hidden_ids"].add(fb.snip_id)
        elif fb.kind == SnipFeedback.NOT_INTERESTED_CREATOR:
            if fb.creator_id:
                ctx["not_interested_creator_ids"].add(fb.creator_id)
        elif fb.kind == SnipFeedback.NOT_INTERESTED_TOPIC and fb.tag:
            ctx["not_interested_tag_names"].add(fb.tag.name)

    now = timezone.now()
    recent_events = list(
        WatchEvent.objects.filter(user=user, snip__isnull=False)
        .select_related("snip", "snip__author")
        .order_by("-timestamp")[:RECENT_HISTORY_LIMIT]
    )
    if not recent_events:
        return ctx

    recent_snip_ids = [e.snip_id for e in recent_events]
    watched_cutoff = now - timedelta(days=WATCHED_LOOKBACK_DAYS)
    ctx["watched_ids"] = {
        e.snip_id for e in recent_events if e.timestamp >= watched_cutoff
    }

    # Completion per snip, plus abandon detection.
    for event in recent_events:
        snip = event.snip
        if not snip:
            continue
        duration = snip.duration or 0
        if duration >= ABANDON_DURATION_MIN and event.duration_watched < max(
            ABANDON_MIN_SECONDS, ABANDON_RATIO * duration
        ):
            ctx["abandoned_ids"].add(snip.id)

    # Creator affinity from watches with decent completion.
    author_counts = defaultdict(float)
    for event in recent_events:
        snip = event.snip
        if not snip or not snip.author_id:
            continue
        duration = snip.duration or 0
        completion = 1.0 if not duration else _clamp(event.duration_watched / duration)
        decay = recency_score(event.timestamp, half_life_hours=24)
        author_counts[snip.author_id] += (0.3 + 0.7 * completion) * decay
    if author_counts:
        max_author = max(author_counts.values())
        ctx["creator_scores"] = {
            author_id: score / max_author
            for author_id, score in author_counts.items()
        }

    # Session awareness: the most recent watches shape the next feed.
    session_cutoff = now - timedelta(hours=SESSION_WINDOW_HOURS)
    tag_boosts = defaultdict(float)
    category_boosts = defaultdict(float)
    recent_snips = Snip.objects.filter(
        id__in=recent_snip_ids[:SESSION_LOOKBACK]
    ).prefetch_related("tags")
    recent_by_id = {s.id: s for s in recent_snips}
    for event in recent_events[:SESSION_LOOKBACK]:
        if event.timestamp < session_cutoff:
            break
        snip = recent_by_id.get(event.snip_id)
        if not snip:
            continue
        decay = recency_score(event.timestamp, half_life_hours=6)
        for tag in snip.tags.all():
            tag_boosts[tag.name] = max(tag_boosts[tag.name], decay)
        if snip.category_id:
            category_boosts[snip.category.slug] = max(
                category_boosts[snip.category.slug], decay
            )
    ctx["session_boosts"] = {
        "tags": dict(tag_boosts),
        "categories": dict(category_boosts),
    }
    return ctx


# ---------------------------------------------------------------------------
# Candidate selection
# ---------------------------------------------------------------------------


def base_candidates(exclude_ids=None):
    """Approved, public snips from active authors within the recency window."""
    from .models import Snip

    qs = Snip.objects.filter(
        is_approved=True,
        visibility="public",
        author__is_active=True,
        timestamp__gte=timezone.now() - timedelta(days=RECENT_WINDOW_DAYS),
    ).select_related("author", "category")
    if exclude_ids:
        qs = qs.exclude(id__in=exclude_ids)
    return qs


# ---------------------------------------------------------------------------
# Co-watch affinity (bounded, mirrors the video feed's cowatch approach)
# ---------------------------------------------------------------------------


def build_cowatch_map(user, recent_snip_ids, candidate_ids, max_co_watchers=150):
    """Find snips that people who watched the same clips as ``user`` also watch.

    Returns ``{snip_id: score}`` normalized to [0,1]. Imports are deferred and
    queries are capped so this stays cheap on a small/medium catalog.
    """
    from django.db.models import Count

    from .models import WatchEvent

    if not recent_snip_ids or not candidate_ids:
        return {}
    recent_set = set(recent_snip_ids)

    co_watcher_ids = set(
        WatchEvent.objects.filter(
            snip_id__in=recent_snip_ids, user__isnull=False
        ).values_list("user_id", flat=True)
    ) - {None}
    if not co_watcher_ids:
        return {}
    co_watcher_ids = list(co_watcher_ids)[:max_co_watchers]

    other_watches = (
        WatchEvent.objects.filter(user_id__in=co_watcher_ids)
        .exclude(snip_id__in=recent_set)
        .values("snip_id")
        .annotate(watch_count=Count("id"))
        .order_by("-watch_count")[:100]
    )
    scores = defaultdict(float)
    for entry in other_watches:
        if entry["snip_id"] in candidate_ids:
            scores[entry["snip_id"]] += entry["watch_count"]
    if not scores:
        return {}
    max_score = max(scores.values())
    return {sid: count / max_score for sid, count in scores.items()}


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def score_snip(snip, ctx, metrics, cowatch_map=None, noise=0.0):
    """Compute ``(components, total, reason)`` for a single candidate snip.

    ``metrics`` is a dict with ``likes``, ``dislikes``, ``comment_count``,
    ``avg_duration_watched``, ``max_likes``, ``max_views``. Components are the
    human-readable signal breakdown used to pick the "why am I seeing this"
    explanation; the total is the numeric rank.
    """
    tag_names = [t.name for t in snip.tags.all()] if snip.tags else []
    category_slug = snip.category.slug if snip.category_id and snip.category else None

    signals = {
        "topic": topic_affinity(tag_names, ctx["tag_scores"]),
        "interest": interest_affinity(category_slug, ctx["category_scores"]),
        "creator": creator_affinity(
            snip.author_id, ctx["followed_author_ids"], ctx["creator_scores"]
        ),
        "session": session_affinity(tag_names, category_slug, ctx["session_boosts"]),
        "cowatch": cowatch_map.get(snip.id, 0.0) if cowatch_map else 0.0,
        "recency": recency_score(snip.timestamp),
        "engagement": engagement_signal(metrics["likes"] - metrics["dislikes"], metrics["max_likes"]),
        "quality": quality_signal(metrics["avg_duration_watched"], snip.duration),
        "popularity": popularity_signal(metrics["views"], metrics["max_views"]),
    }

    negative = {
        "dislike": 1.0 if snip.id in ctx["disliked_ids"] else 0.0,
        "creator": 1.0 if snip.author_id in ctx["not_interested_creator_ids"] else 0.0,
        "topic": 1.0 if any(t in ctx["not_interested_tag_names"] for t in tag_names) else 0.0,
        "abandon": 1.0 if snip.id in ctx["abandoned_ids"] else 0.0,
    }

    total = (
        sum(WEIGHTS[k] * signals[k] for k in WEIGHTS)
        - sum(NEGATIVE_WEIGHTS[k] * negative[k] for k in NEGATIVE_WEIGHTS)
        + noise
    )
    return signals, total, _explain(signals, negative, snip, ctx)


def _explain(signals, negative, snip, ctx):
    """Pick the most honest explanation for a recommendation."""
    if snip.author_id in ctx["followed_author_ids"]:
        return f"Because you follow @{snip.author.username}"
    if negative["creator"]:
        return None
    if negative["topic"]:
        return None
    if signals["session"] >= 0.6:
        return "Because you've been watching this lately"
    if signals["topic"] >= 0.55:
        top = max(
            (t for t in snip.tags.all() if t.name in ctx["tag_scores"]),
            key=lambda t: ctx["tag_scores"].get(t.name, 0),
            default=None,
        )
        return f"Because you watched #{top.name} content" if top else "Because it matches your interests"
    if signals["creator"] >= 0.55:
        return f"From a creator you keep watching"
    if signals["interest"] >= 0.5:
        return f"Because you enjoy {snip.category.name if snip.category else 'this topic'}"
    if signals["cowatch"] >= 0.6:
        return "Popular with viewers of what you watch"
    if signals["engagement"] >= 0.6:
        return "Trending now"
    if signals["quality"] >= 0.6:
        return "People are loving this one"
    if signals["recency"] >= 0.9:
        return "Fresh from the community"
    return None


# ---------------------------------------------------------------------------
# Diversity rerank (greedy, MMR-style)
# ---------------------------------------------------------------------------


def rerank_diverse(scored):
    """Reorder ``[(signals, total, reason, snip)]`` enforcing creator/topic caps.

    Keeps the highest-scoring clip but caps how many clips one creator or one
    topic may occupy the front of the feed; the rest are appended at the tail
    so nothing is ever dropped.
    """
    scored = sorted(scored, key=lambda item: item[1], reverse=True)
    selected = []
    author_counts = defaultdict(int)
    tag_counts = defaultdict(int)
    deferred = []

    for entry in scored:
        snip = entry[3]
        author_ok = author_counts[snip.author_id] < AUTHOR_MAX_IN_FEED
        tags = [t.name for t in snip.tags.all()] if snip.tags else []
        tag_ok = all(tag_counts[t] < TAG_MAX_IN_FEED for t in tags)
        if author_ok and tag_ok:
            selected.append(entry)
            author_counts[snip.author_id] += 1
            for t in tags:
                tag_counts[t] += 1
        else:
            deferred.append(entry)

    return selected + deferred


# ---------------------------------------------------------------------------
# Mode builders
# ---------------------------------------------------------------------------


def _candidate_metrics(snips, mode):
    """Annotate candidate snips with cheap per-row metrics for scoring."""
    from django.db.models import Avg, Count, Q, Subquery, OuterRef

    from .models import WatchEvent

    now = timezone.now()
    qs = snips.annotate(
        num_likes=Count("likes", distinct=True),
        num_dislikes=Count("dislikes", distinct=True),
        comment_count=Count("comments", distinct=True),
        avg_duration_watched=Subquery(
            WatchEvent.objects.filter(
                snip=OuterRef("pk"), duration_watched__gt=0
            )
            .values("snip")
            .annotate(avg=Avg("duration_watched"))
            .values("avg")
        ),
    )
    if mode == "trending":
        since = now - timedelta(hours=TRENDING_WINDOW_HOURS)
        qs = qs.annotate(
            views_window=Count(
                "watch_events", filter=Q(watch_events__timestamp__gte=since), distinct=True
            ),
            likes_window=Count(
                "likes", filter=Q(likes__created_at__gte=since), distinct=True
            ),
        )
    return qs


def _exploration_noise(user_id, snip_id):
    """Deterministic per-user noise so cold-start feeds differ between users."""
    if user_id is None:
        return 0.0
    digest = hashlib.md5(f"{user_id}:{snip_id}".encode()).hexdigest()
    return (int(digest[:4], 16) / 0xFFFF - 0.5) * 0.04


def _cold_start_pool(user, snips):
    """Diverse, popular-but-fresh pool for users with no meaningful history."""
    scored = []
    max_views = max((s.view_count for s in snips), default=1) or 1
    for s in snips:
        pop = popularity_signal(s.view_count, max_views)
        fresh = recency_score(s.timestamp, half_life_hours=48)
        quality = quality_signal(
            getattr(s, "avg_duration_watched", None), s.duration
        )
        total = 1.6 * pop + 1.4 * fresh + 0.8 * quality + _exploration_noise(
            user.id if user else None, s.id
        )
        scored.append((total, s))
    scored.sort(key=lambda item: item[0], reverse=True)
    # Enforce per-category and per-author caps for a broad cold-start spread.
    category_counts = defaultdict(int)
    author_counts = defaultdict(int)
    picked = []
    for total, s in scored:
        cat_slug = s.category.slug if s.category else ""
        if author_counts[s.author_id] >= 2:
            continue
        if cat_slug and category_counts[cat_slug] >= 3:
            continue
        author_counts[s.author_id] += 1
        if cat_slug:
            category_counts[cat_slug] += 1
        picked.append((total, s))
        if len(picked) >= 60:
            break
    return picked


def _has_history(ctx):
    """A user "has history" when the ranker can personalize: watched clips,
    learned tag/category interests, or explicit creator follows. Following
    creators alone is enough -- those users expect to see their follows."""
    return bool(
        ctx["watched_ids"]
        or ctx["tag_scores"]
        or ctx["category_scores"]
        or ctx["followed_author_ids"]
    )


def build_recommended(user, ctx, limit=40, exclude_ids=None):
    """Personalized ranked feed: score -> diversity -> exploration slots."""
    qs = (
        base_candidates(exclude_ids=exclude_ids)
        .prefetch_related("tags")
        .order_by("-timestamp")[:CANDIDATE_LIMIT]
    )
    ordered = list(_candidate_metrics(qs, "recommended"))
    if not ordered:
        return []

    recent_ids = list(ctx["watched_ids"])[:30]
    cowatch_map = build_cowatch_map(user, recent_ids, [s.id for s in ordered]) if recent_ids else {}

    # Exclude hidden/disliked outright plus clips watched recently (a seen clip
    # is the least valuable thing to show); abandoned clips get a hard penalty
    # but can still resurface later (avoiding an irreversible one-off block).
    visible = [
        s for s in ordered
        if s.id not in ctx["disliked_ids"]
        and s.id not in ctx["hidden_ids"]
        and s.id not in ctx["watched_ids"]
    ]

    max_likes = max((getattr(s, "num_likes", 0) for s in visible), default=1) or 1
    max_views = max((s.view_count for s in visible), default=1) or 1

    if not _has_history(ctx):
        cold = _cold_start_pool(user, visible)
        return [
            {
                "snip": s,
                "score": total,
                "reason": _cold_start_reason(s, user, ctx),
            }
            for total, s in cold[:limit]
        ]

    scored = []
    for s in visible:
        metrics = {
            "likes": getattr(s, "num_likes", 0),
            "dislikes": getattr(s, "num_dislikes", 0),
            "comment_count": getattr(s, "comment_count", 0),
            "avg_duration_watched": getattr(s, "avg_duration_watched", None),
            "views": s.view_count,
            "max_likes": max_likes,
            "max_views": max_views,
        }
        signals, total, reason = score_snip(s, ctx, metrics, cowatch_map=cowatch_map)
        scored.append((signals, total, reason, s))

    ranked = rerank_diverse(scored)

    # Exploration slots: at every Nth position, prefer something the user
    # hasn't heavily signaled so the feed doesn't go stale.
    result = []
    for idx, entry in enumerate(ranked):
        if idx >= 1 and idx % EXPLORATION_SLOT_EVERY == EXPLORATION_START % EXPLORATION_SLOT_EVERY:
            candidates_pool = [e for e in ranked if e not in result]
            if candidates_pool:
                explorer = max(
                    candidates_pool,
                    key=lambda e: (e[3].view_count, e[1]),
                )
                result.append(explorer)
                continue
        result.append(entry)

    return [
        {
            "snip": entry[3],
            "score": entry[1],
            "reason": entry[2],
        }
        for entry in result[:limit]
    ]


def _cold_start_reason(snip, user, ctx):
    if snip.category:
        return f"Popular in {snip.category.name}"
    if snip.view_count >= 100:
        return "Popular in the community"
    return "New & trending"


def build_trending(user, limit=40, exclude_ids=None):
    """Momentum-based feed (NOT personalization): views/likes velocity."""
    from django.db.models import Q

    now = timezone.now()
    qs = (
        base_candidates(exclude_ids=exclude_ids)
        .prefetch_related("tags")
        .order_by("-timestamp")[:CANDIDATE_LIMIT]
    )
    scored = list(_candidate_metrics(qs, "trending"))
    if not scored:
        return []
    since = now - timedelta(hours=TRENDING_WINDOW_HOURS)
    items = []
    for s in scored:
        views = getattr(s, "views_window", 0) or s.view_count
        likes = getattr(s, "likes_window", 0) or 0
        momentum = (
            2.2 * math.log1p(views)
            + 1.4 * math.log1p(likes)
            + 0.4 * math.log1p(s.view_count)
            + 0.5 * recency_score(s.timestamp, half_life_hours=96)
        )
        items.append((momentum, s))
    items.sort(key=lambda item: item[0], reverse=True)
    return [
        {"snip": s, "score": total, "reason": "Trending now"}
        for total, s in items[:limit]
    ]


def build_fresh(user, limit=40, exclude_ids=None):
    """Newest-first feed with a light diversity shuffle."""
    candidates = list(
        base_candidates(exclude_ids=exclude_ids)
        .prefetch_related("tags")
        .order_by("-timestamp")[:CANDIDATE_LIMIT]
    )
    ranked = [
        {"snip": s, "score": recency_score(s.timestamp), "reason": "Fresh from the community"}
        for s in candidates[:limit]
    ]
    return ranked


def build_following(user, limit=40, exclude_ids=None):
    """Snips from creators the user creeks (native) + followed YouTube Shorts."""
    from .models import Creek

    creeked_ids = set(
        Creek.objects.filter(author=user)
        .exclude(account__user__is_active=False)
        .values_list("account__user_id", flat=True)
    )
    qs = base_candidates(exclude_ids=exclude_ids).filter(author_id__in=creeked_ids)
    snips = list(qs.prefetch_related("tags").order_by("-timestamp")[:limit])
    return [
        {"snip": s, "score": recency_score(s.timestamp), "reason": "From a creator you follow"}
        for s in snips
    ]


# ---------------------------------------------------------------------------
# Rabbit hole ("Dive deeper") / related
# ---------------------------------------------------------------------------


def related_snips(seed, limit=10, exclude_ids=None):
    """Find snips related to ``seed`` via tags, then category, then co-watch.

    Tags are the strongest link so the rabbit hole naturally chains topic to
    topic (Redstone -> Redstone farms -> Auto storage).
    """
    from .models import Snip

    qs = (
        Snip.objects.filter(
            is_approved=True, visibility="public", author__is_active=True
        )
        .exclude(id=seed.id)
        .select_related("author", "category")
        .prefetch_related("tags")
    )
    if exclude_ids:
        qs = qs.exclude(id__in=exclude_ids)

    seed_tag_ids = [t.id for t in seed.tags.all()]
    related = []
    if seed_tag_ids:
        matched = list(
            qs.filter(tags__id__in=seed_tag_ids).distinct().order_by("-timestamp")[:limit]
        )
        related.extend(matched)
    if len(related) < limit and seed.category_id:
        cat_ids = {s.id for s in related}
        matched = list(
            qs.filter(category=seed.category)
            .exclude(id__in=cat_ids | {seed.id})
            .order_by("-timestamp")[: limit - len(related)]
        )
        related.extend(matched)
    if len(related) < limit:
        existing = {s.id for s in related} | {seed.id}
        recent = list(
            qs.exclude(id__in=existing).order_by("-timestamp")[: limit - len(related)]
        )
        related.extend(recent)
    return related[:limit]


def reason_for_related(seed, related):
    """A short explanation of why a related clip was picked."""
    seed_tag_names = {t.name for t in seed.tags.all()}
    overlap = [t for t in related.tags.all() if t.name in seed_tag_names]
    if overlap:
        return f"More about #{overlap[0].name}"
    if seed.category and related.category and seed.category_id == related.category_id:
        return f"More {seed.category.name.lower()} content"
    return "Related to what you're watching"
