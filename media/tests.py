from datetime import timedelta
from unittest import IsolatedAsyncioTestCase, mock

import os

from django.contrib.auth.models import User
from django.db import IntegrityError
from django.test import SimpleTestCase, TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from . import ranking
from . import snips_rank
from .consumers import SnipFeedConsumer
from .models import (CategoryVideo, Comment, Creek, DisPike, Like, MediaProfile,
                     Snip, SnipDislike, SnipFeedback, SnipLike, SnipSave,
                     Tag, Video, WatchEvent, UploadRateLimit, UserTitle,
                     YouTubeChannelFollow)
from . import youtube as youtube_module
from .youtube import (normalize_youtube_url, validate_youtube_id,
                      youtube_embed_url, youtube_thumbnail_url, get_video_metadata,
                      YOUTUBE_SYSTEM_USERNAME)
from .Serializers import VideoSerializer
from .views import ensure_category, recent_watch_keywords


def make_video(author, category, hours_old=0, is_approved=True, title="video"):
    """Helper: create a Video with a backdated timestamp."""
    video = Video.objects.create(
        author=author,
        category=category,
        title=title,
        description="desc",
        is_approved=is_approved,
    )
    if hours_old:
        Video.objects.filter(pk=video.pk).update(
            timestamp=timezone.now() - timedelta(hours=hours_old)
        )
        video.refresh_from_db()
    return video


def make_watch_event(user, video, hours_ago=0, duration=60, session_id=""):
    """Helper: create a WatchEvent with a backdated timestamp."""
    event = WatchEvent.objects.create(
        user=user,
        video=video,
        duration_watched=duration,
        session_id=session_id or f"session_{user.id}",
    )
    if hours_ago:
        WatchEvent.objects.filter(pk=event.pk).update(
            timestamp=timezone.now() - timedelta(hours=hours_ago)
        )
        event.refresh_from_db()
    return event


def make_snip(author, category=None, hours_old=0, is_approved=True, title="snip",
              duration=15, view_count=0, tags=()):
    """Helper: create a Snip with backdated timestamp and optional tags."""
    snip = Snip.objects.create(
        author=author,
        category=category,
        title=title,
        description=f"desc {title}",
        video=f"snip_{title}.mp4",
        thumbnail="",
        visibility="public",
        is_approved=is_approved,
        duration=duration,
        view_count=view_count,
    )
    if tags:
        for name in tags:
            tag, _ = Tag.objects.get_or_create(name=name)
            snip.tags.add(tag)
    if hours_old:
        Snip.objects.filter(pk=snip.pk).update(
            timestamp=timezone.now() - timedelta(hours=hours_old)
        )
        snip.refresh_from_db()
    return snip


def make_snip_event(user, snip, hours_ago=0, duration=0, session_id=""):
    """Helper: create a WatchEvent for a snip with a backdated timestamp."""
    event = WatchEvent.objects.create(
        user=user,
        snip=snip,
        duration_watched=duration,
        session_id=session_id or f"snip_session_{user.id}",
    )
    if hours_ago:
        WatchEvent.objects.filter(pk=event.pk).update(
            timestamp=timezone.now() - timedelta(hours=hours_ago)
        )
        event.refresh_from_db()
    return event


# ---------------------------------------------------------------------------
# Pure-function unit tests for media/ranking.py
# ---------------------------------------------------------------------------
class RankingUnitTests(TestCase):
    def test_interest_affinity_empty_interests_returns_exploration_floor(self):
        self.assertEqual(ranking.interest_affinity("gaming", {}), ranking.EXPLORATION_FLOOR)

    def test_interest_affinity_unknown_category_returns_exploration_floor(self):
        self.assertEqual(
            ranking.interest_affinity("cooking", {"gaming": 10}),
            ranking.EXPLORATION_FLOOR,
        )

    def test_interest_affinity_top_category_is_normalized_to_one(self):
        self.assertEqual(ranking.interest_affinity("gaming", {"gaming": 10, "music": 5}), 1.0)

    def test_interest_affinity_secondary_category_is_scaled(self):
        self.assertAlmostEqual(
            ranking.interest_affinity("music", {"gaming": 10, "music": 5}), 0.5
        )

    def test_engagement_score_is_zero_for_no_votes(self):
        self.assertEqual(ranking.engagement_score(0, 0), 0.0)

    def test_engagement_score_positive_for_net_likes(self):
        self.assertGreater(ranking.engagement_score(10, 2), 0)

    def test_engagement_score_negative_for_net_dislikes(self):
        self.assertLess(ranking.engagement_score(1, 10), 0)

    def test_engagement_score_is_dampened_not_linear(self):
        low = ranking.engagement_score(10, 0)
        high = ranking.engagement_score(100, 0)
        self.assertGreater(high, low)
        self.assertLess(high, low * 10)

    def test_recency_score_decays_with_age(self):
        now_score = ranking.recency_score(timezone.now())
        old_score = ranking.recency_score(timezone.now() - timedelta(hours=ranking.RECENCY_HALF_LIFE_HOURS))
        self.assertAlmostEqual(now_score, 1.0, places=2)
        self.assertAlmostEqual(old_score, 0.5, places=2)
        self.assertGreater(now_score, old_score)

    def test_adjust_category_score_increases_and_clamps_at_max(self):
        categories = {"gaming": ranking.MAX_CATEGORY_SCORE - 1}
        result = ranking.adjust_category_score(categories, "gaming", 5)
        self.assertEqual(result["gaming"], ranking.MAX_CATEGORY_SCORE)

    def test_adjust_category_score_decreases_and_clamps_at_min(self):
        categories = {"gaming": 1}
        result = ranking.adjust_category_score(categories, "gaming", -5)
        self.assertEqual(result["gaming"], ranking.MIN_CATEGORY_SCORE)

    def test_adjust_category_score_noop_without_slug(self):
        categories = {"gaming": 5}
        result = ranking.adjust_category_score(categories, None, 5)
        self.assertEqual(result, {"gaming": 5})

    def test_cowatch_affinity_empty_returns_zero(self):
        self.assertEqual(ranking.cowatch_affinity(1, [], {}), 0.0)

    def test_cowatch_affinity_no_match_returns_zero(self):
        self.assertEqual(ranking.cowatch_affinity(99, [1, 2], {1: 0.5, 2: 0.3}), 0.0)

    def test_cowatch_affinity_with_match_scales_to_one(self):
        result = ranking.cowatch_affinity(1, [1, 2], {1: 0.8, 2: 0.4})
        self.assertAlmostEqual(result, 1.0)  # 0.8 / max(0.8, 0.4) = 1.0

    def test_cowatch_affinity_below_max_scales_correctly(self):
        result = ranking.cowatch_affinity(2, [1, 2], {1: 1.0, 2: 0.5})
        self.assertAlmostEqual(result, 0.5)

    def test_cowatch_increases_composite_score(self):
        """A video that co-watches with user history should score higher than one that doesn't."""
        viewer = User.objects.create_user(username="viewer_cw", password="pw")
        creator = User.objects.create_user(username="creator_cw", password="pw")
        gaming = CategoryVideo.objects.create(name="Gaming CW", slug="gaming-cw")

        v1 = make_video(creator, gaming, hours_old=1, title="cowatched")
        v1.num_likes = v1.num_dislikes = 0

        v2 = make_video(creator, gaming, hours_old=1, title="unrelated")
        v2.num_likes = v2.num_dislikes = 0

        cowatch_map = {v1.id: 1.0, v2.id: 0.0}
        interests = {"gaming-cw": 10}

        score1 = ranking.score_video(v1, interests, set(), cowatch_map=cowatch_map, user_recent_video_ids=[1])
        score2 = ranking.score_video(v2, interests, set(), cowatch_map=cowatch_map, user_recent_video_ids=[1])
        self.assertGreater(score1, score2)


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------
class RankVideosIntegrationTests(TestCase):
    def setUp(self):
        self.viewer = User.objects.create_user(username="viewer", password="pw")
        self.creator_a = User.objects.create_user(username="creator_a", password="pw")
        self.creator_b = User.objects.create_user(username="creator_b", password="pw")

        self.gaming = CategoryVideo.objects.create(name="Gaming", slug="gaming")
        self.cooking = CategoryVideo.objects.create(name="Cooking", slug="cooking")

    def test_disliked_video_ranks_below_undisliked_video_in_same_category(self):
        liked = make_video(self.creator_a, self.gaming, hours_old=1, title="liked")
        disliked = make_video(self.creator_a, self.gaming, hours_old=1, title="disliked")

        Like.objects.create(author=self.viewer, video=liked)
        for i in range(5):
            u = User.objects.create_user(username=f"hater{i}")
            DisPike.objects.create(author=u, video=disliked)

        liked.num_likes, liked.num_dislikes = 1, 0
        disliked.num_likes, disliked.num_dislikes = 0, 5

        interests = {"gaming": 10}
        ranked = ranking.rank_videos([disliked, liked], interests, creeked_author_ids=set())
        self.assertEqual([v.title for v in ranked], ["liked", "disliked"])

    def test_old_favorite_category_video_can_be_outranked_by_fresh_offcategory_viral_video(self):
        old_favorite = make_video(self.creator_a, self.gaming, hours_old=500, title="old_favorite")
        old_favorite.num_likes, old_favorite.num_dislikes = 3, 0

        fresh_viral = make_video(self.creator_b, self.cooking, hours_old=1, title="fresh_viral")
        fresh_viral.num_likes, fresh_viral.num_dislikes = 50, 1

        interests = {"gaming": 20, "cooking": 1}
        ranked = ranking.rank_videos([old_favorite, fresh_viral], interests, creeked_author_ids=set())
        self.assertEqual(ranked[0].title, "fresh_viral")

    def test_creek_bonus_breaks_ties_towards_followed_creator(self):
        followed_video = make_video(self.creator_a, self.gaming, hours_old=1, title="followed")
        followed_video.num_likes, followed_video.num_dislikes = 0, 0

        stranger_video = make_video(self.creator_b, self.gaming, hours_old=1, title="stranger")
        stranger_video.num_likes, stranger_video.num_dislikes = 0, 0

        interests = {"gaming": 10}
        ranked = ranking.rank_videos(
            [stranger_video, followed_video], interests, creeked_author_ids={self.creator_a.id}
        )
        self.assertEqual(ranked[0].title, "followed")

    def test_new_user_with_no_interests_still_gets_full_ranking_not_empty(self):
        v1 = make_video(self.creator_a, self.gaming, hours_old=1, title="v1")
        v2 = make_video(self.creator_b, self.cooking, hours_old=2, title="v2")
        v1.num_likes = v1.num_dislikes = v2.num_likes = v2.num_dislikes = 0

        ranked = ranking.rank_videos([v1, v2], user_interests={}, creeked_author_ids=set())
        self.assertEqual(len(ranked), 2)
        self.assertEqual(ranked[0].title, "v1")

    def test_cowatch_related_videos_rank_higher_than_unrelated(self):
        """Videos watched by same users should rank higher for the feed."""
        user_c = User.objects.create_user(username="cowatch_user", password="pw")
        other1 = User.objects.create_user(username="cw_other1", password="pw")
        other2 = User.objects.create_user(username="cw_other2", password="pw")

        seed = make_video(self.creator_a, self.gaming, hours_old=5, title="seed")
        related = make_video(self.creator_b, self.gaming, hours_old=3, title="related")
        unrelated = make_video(self.creator_a, self.gaming, hours_old=1, title="unrelated")

        # Others watched seed + related (co-watch pair)
        make_watch_event(other1, seed, hours_ago=4)
        make_watch_event(other1, related, hours_ago=3)
        make_watch_event(other2, seed, hours_ago=4)
        make_watch_event(other2, related, hours_ago=3)

        # User watched seed
        make_watch_event(user_c, seed, hours_ago=2)

        seed.num_likes = seed.num_dislikes = related.num_likes = related.num_dislikes = 0
        unrelated.num_likes = unrelated.num_dislikes = 0

        user_recent = ranking.get_user_recent_video_ids(user_c)
        cowatch_map = ranking.build_cowatch_map(user_recent, [related.id, unrelated.id])

        interests = {"gaming": 10}
        score_related = ranking.score_video(related, interests, set(), cowatch_map=cowatch_map, user_recent_video_ids=user_recent)
        score_unrelated = ranking.score_video(unrelated, interests, set(), cowatch_map=cowatch_map, user_recent_video_ids=user_recent)
        self.assertGreater(score_related, score_unrelated)


# ---------------------------------------------------------------------------
# Co-watch computation tests
# ---------------------------------------------------------------------------
class CoWatchComputationTests(TestCase):
    def setUp(self):
        self.user1 = User.objects.create_user(username="cw_u1", password="pw")
        self.user2 = User.objects.create_user(username="cw_u2", password="pw")
        self.creator = User.objects.create_user(username="cw_creator", password="pw")
        self.gaming = CategoryVideo.objects.create(name="Gaming CW2", slug="gaming-cw2")

    def test_build_cowatch_map_empty_when_no_history(self):
        result = ranking.build_cowatch_map([], [1, 2, 3])
        self.assertEqual(result, {})

    def test_build_cowatch_map_finds_co_watched_videos(self):
        v1 = make_video(self.creator, self.gaming, title="v1")
        v2 = make_video(self.creator, self.gaming, title="v2")
        v3 = make_video(self.creator, self.gaming, title="v3")

        # user1 watched v1 and v2
        make_watch_event(self.user1, v1, hours_ago=5)
        make_watch_event(self.user1, v2, hours_ago=4)

        # user2 watched v1 and v2
        make_watch_event(self.user2, v1, hours_ago=3)
        make_watch_event(self.user2, v2, hours_ago=2)

        # v3 was never watched with v1
        make_watch_event(self.user1, v3, hours_ago=1)

        result = ranking.build_cowatch_map([v1.id], [v2.id, v3.id])
        self.assertIn(v2.id, result)
        self.assertIn(v3.id, result)
        # v2 should have higher co-watch score (watched by 2 users with v1)
        self.assertGreater(result[v2.id], result.get(v3.id, 0))

    def test_cold_start_user_no_history_returns_empty_map(self):
        result = ranking.build_cowatch_map([], [1, 2])
        self.assertEqual(result, {})

    def test_cold_start_new_video_no_cowatch_data_returns_zero(self):
        new_video = make_video(self.creator, self.gaming, title="brand_new")
        result = ranking.cowatch_affinity(new_video.id, [1, 2, 3], {})
        self.assertEqual(result, 0.0)


# ---------------------------------------------------------------------------
# API-level tests
# ---------------------------------------------------------------------------
class FeedViewTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username="alice", password="pw")
        self.other = User.objects.create_user(username="bob", password="pw")

        self.gaming = CategoryVideo.objects.create(name="Gaming", slug="gaming")
        self.cooking = CategoryVideo.objects.create(name="Cooking", slug="cooking")

        self.profile, _ = MediaProfile.objects.get_or_create(user=self.user)
        self.profile.categories = {"gaming": 10}
        self.profile.save()

        self.video = make_video(self.other, self.gaming, hours_old=1, title="gaming_vid")

    def test_login_get_video_requires_auth(self):
        resp = self.client.get("/media/logingetvideo/")
        self.assertEqual(resp.status_code, 401)

    def test_login_get_video_returns_paginated_envelope(self):
        self.client.force_authenticate(user=self.user)
        resp = self.client.get("/media/logingetvideo/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("results", resp.data)
        self.assertIn("page", resp.data)
        self.assertIn("page_size", resp.data)
        self.assertIn("count", resp.data)
        self.assertEqual(resp.data["count"], 1)

    def test_login_get_video_pagination_page_size_respected(self):
        for i in range(3):
            make_video(self.other, self.gaming, hours_old=i, title=f"extra_{i}")

        self.client.force_authenticate(user=self.user)
        resp = self.client.get("/media/logingetvideo/?page=1&page_size=2")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data["results"]), 2)
        self.assertEqual(resp.data["count"], 4)

    def test_guest_get_video_is_open_and_paginated(self):
        resp = self.client.get("/media/guestgetvideo/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("results", resp.data)

    def test_unapproved_video_excluded_from_feeds(self):
        make_video(self.other, self.gaming, hours_old=0, is_approved=False, title="pending")
        self.client.force_authenticate(user=self.user)
        resp = self.client.get("/media/logingetvideo/")
        titles = [v["title"] for v in resp.data["results"]]
        self.assertNotIn("pending", titles)

    def test_dislike_lowers_category_score(self):
        self.client.force_authenticate(user=self.user)
        before = self.profile.categories.get("gaming", 0)

        resp = self.client.post("/media/dispikevideo/", {"id": self.video.id})
        self.assertEqual(resp.status_code, 201)

        self.profile.refresh_from_db()
        self.assertEqual(self.profile.categories.get("gaming"), before - ranking.DISLIKE_PENALTY)

    def test_undoing_dislike_restores_category_score(self):
        self.client.force_authenticate(user=self.user)
        self.client.post("/media/dispikevideo/", {"id": self.video.id})
        self.profile.refresh_from_db()
        after_dislike = self.profile.categories.get("gaming")

        resp = self.client.post("/media/dispikevideo/", {"id": self.video.id})
        self.assertEqual(resp.status_code, 200)

        self.profile.refresh_from_db()
        self.assertEqual(self.profile.categories.get("gaming"), after_dislike + ranking.DISLIKE_PENALTY)

    def test_watch_video_boosts_category_score(self):
        self.client.force_authenticate(user=self.user)
        before = self.profile.categories.get("gaming", 0)

        resp = self.client.post("/media/watchvideo/", {"video_id": self.video.id})
        self.assertEqual(resp.status_code, 200)

        self.profile.refresh_from_db()
        self.assertEqual(self.profile.categories.get("gaming"), before + ranking.WATCH_BOOST)

    def test_watch_video_boost_is_clamped_at_max(self):
        self.profile.categories = {"gaming": ranking.MAX_CATEGORY_SCORE}
        self.profile.save()

        self.client.force_authenticate(user=self.user)
        self.client.post("/media/watchvideo/", {"video_id": self.video.id})

        self.profile.refresh_from_db()
        self.assertEqual(self.profile.categories.get("gaming"), ranking.MAX_CATEGORY_SCORE)

    def test_view_count_increments_on_watch(self):
        self.client.force_authenticate(user=self.user)
        self.assertEqual(self.video.view_count, 0)

        self.client.post("/media/watchvideo/", {"video_id": self.video.id})
        self.video.refresh_from_db()
        self.assertEqual(self.video.view_count, 1)

    def test_watch_snip_view_count_increments(self):
        snip = Snip.objects.create(
            author=self.user,
            title="snip test",
            description="desc",
            video="https://example.com/video.mp4",
        )

        resp = self.client.get("/media/snip/watch/", {"id": snip.id})

        self.assertEqual(resp.status_code, 200)
        snip.refresh_from_db()
        self.assertEqual(snip.view_count, 1)

    def test_view_dedup_prevents_double_counting(self):
        self.client.force_authenticate(user=self.user)

        self.client.post("/media/watchvideo/", {"video_id": self.video.id})
        self.client.post("/media/watchvideo/", {"video_id": self.video.id})
        self.video.refresh_from_db()
        # Only 1 new view because of dedup (WatchEvent still recorded, but view_count only +1)
        self.assertEqual(self.video.view_count, 1)

    def test_upload_rate_limit_blocks_spam(self):
        from .views import check_upload_rate_limit
        self.client.force_authenticate(user=self.user)

        # Upload 3 videos (the limit)
        for i in range(3):
            UploadRateLimit.objects.create(user=self.user)

        self.assertFalse(check_upload_rate_limit(self.user))

    def test_upload_rate_limit_allows_after_window(self):
        self.client.force_authenticate(user=self.user)

        # Old uploads outside the window
        for i in range(5):
            ul = UploadRateLimit.objects.create(user=self.user)
            UploadRateLimit.objects.filter(pk=ul.pk).update(
                uploaded_at=timezone.now() - timedelta(hours=2)
            )

        from .views import check_upload_rate_limit
        self.assertTrue(check_upload_rate_limit(self.user))

    def test_track_retention_saves_duration(self):
        self.client.force_authenticate(user=self.user)
        self.client.post("/media/watchvideo/", {"video_id": self.video.id})

        resp = self.client.post("/media/trackretention/", {
            "video_id": self.video.id,
            "duration": 120,
        })
        self.assertEqual(resp.status_code, 200)

        event = WatchEvent.objects.filter(user=self.user, video=self.video).first()
        self.assertIsNotNone(event)
        self.assertEqual(event.duration_watched, 120)

    def test_video_serializer_includes_view_count(self):
        self.client.force_authenticate(user=self.user)
        resp = self.client.get("/media/logingetvideo/")
        video_data = resp.data["results"][0]
        self.assertIn("view_count", video_data)
        self.assertEqual(video_data["view_count"], 0)


# ---------------------------------------------------------------------------
# YouTube source support
# ---------------------------------------------------------------------------
class YouTubeNormalizationTests(TestCase):
    def test_accepts_watch_url(self):
        self.assertEqual(
            normalize_youtube_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ"),
            "dQw4w9WgXcQ",
        )

    def test_accepts_watch_url_with_extra_params(self):
        self.assertEqual(
            normalize_youtube_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PL123"),
            "dQw4w9WgXcQ",
        )

    def test_accepts_short_url(self):
        self.assertEqual(normalize_youtube_url("https://youtu.be/dQw4w9WgXcQ"), "dQw4w9WgXcQ")

    def test_accepts_embed_url(self):
        self.assertEqual(
            normalize_youtube_url("https://www.youtube.com/embed/dQw4w9WgXcQ"),
            "dQw4w9WgXcQ",
        )

    def test_accepts_shorts_url(self):
        self.assertEqual(
            normalize_youtube_url("https://www.youtube.com/shorts/dQw4w9WgXcQ"),
            "dQw4w9WgXcQ",
        )

    def test_accepts_raw_id(self):
        self.assertEqual(normalize_youtube_url("dQw4w9WgXcQ"), "dQw4w9WgXcQ")

    def test_rejects_invalid_input(self):
        for bad in ["", None, "not-a-youtube-url", "https://example.com/x",
                    "https://www.youtube.com/watch?v=", "dQw4w9WgXc"]:
            self.assertIsNone(normalize_youtube_url(bad))

    def test_validate_youtube_id(self):
        self.assertTrue(validate_youtube_id("dQw4w9WgXcQ"))
        self.assertFalse(validate_youtube_id("dQw4w9WgXcQ!@#"))
        self.assertFalse(validate_youtube_id(""))

    def test_embed_and_thumbnail_urls(self):
        self.assertEqual(youtube_embed_url("dQw4w9WgXcQ"), "https://www.youtube.com/embed/dQw4w9WgXcQ")
        self.assertEqual(youtube_thumbnail_url("dQw4w9WgXcQ"), "https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg")
        self.assertEqual(youtube_embed_url("nope"), "")
        self.assertEqual(youtube_thumbnail_url("nope"), "")

    def test_metadata_returns_none_without_api_key(self):
        self.assertIsNone(get_video_metadata("dQw4w9WgXcQ"))

    def test_metadata_returns_none_for_invalid_id(self):
        from django.conf import settings
        with self.settings(YOUTUBE_API_KEY="test-key"):
            self.assertIsNone(get_video_metadata("invalid"))


class AddYouTubeVideoTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username="yt_creator", password="pw")
        self.client.force_authenticate(user=self.user)

    def test_add_youtube_video_requires_auth(self):
        anon = APIClient()
        resp = anon.post("/media/youtube/add/", {"youtube_url": "https://youtu.be/dQw4w9WgXcQ"})
        self.assertEqual(resp.status_code, 401)

    def test_add_youtube_video_stores_id_not_file(self):
        resp = self.client.post("/media/youtube/add/", {
            "youtube_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "title": "Never Gonna Give You Up",
            "description": "A classic.",
            "category": "music",
        })
        self.assertEqual(resp.status_code, 201)
        data = resp.data
        self.assertEqual(data["source_type"], "YOUTUBE")
        self.assertEqual(data["youtube_video_id"], "dQw4w9WgXcQ")
        self.assertEqual(data["embed_url"], "https://www.youtube.com/embed/dQw4w9WgXcQ")
        self.assertIn("i.ytimg.com", data["thumbnail"])
        self.assertIsNone(data["video"])  # no file is stored

        row = Video.objects.get(id=data["id"])
        self.assertEqual(row.source_type, "YOUTUBE")
        self.assertEqual(row.youtube_video_id, "dQw4w9WgXcQ")
        self.assertEqual(row.video, "")
        self.assertTrue(row.is_approved)

    def test_add_youtube_video_rejects_invalid_url(self):
        resp = self.client.post("/media/youtube/add/", {"youtube_url": "https://example.com/not-youtube"})
        self.assertEqual(resp.status_code, 400)

    def test_add_youtube_video_never_trusts_client_source_type(self):
        resp = self.client.post("/media/youtube/add/", {
            "youtube_video_id": "dQw4w9WgXcQ",
            "source_type": "CREEKTUBE",
        })
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.data["source_type"], "YOUTUBE")


class UnifiedFeedTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username="alice_hybrid", password="pw")
        self.other = User.objects.create_user(username="bob_hybrid", password="pw")
        self.gaming = CategoryVideo.objects.create(name="Gaming", slug="gaming")
        self.client.force_authenticate(user=self.user)

    def test_native_video_serializes_as_creektube_source(self):
        native = make_video(self.other, self.gaming, title="native_vid")
        resp = self.client.get("/media/guestgetvideo/?video_id=%d" % native.id)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["source_type"], "CREEKTUBE")
        self.assertIsNone(resp.data["embed_url"])

    def test_youtube_videos_share_the_feed(self):
        native = make_video(self.other, self.gaming, hours_old=1, title="native")
        Video.objects.create(
            author=self.other,
            category=self.gaming,
            title="youtube_vid",
            description="desc",
            is_approved=True,
            source_type="YOUTUBE",
            youtube_video_id="dQw4w9WgXcQ",
            thumbnail="https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg",
        )
        resp = self.client.get("/media/logingetvideo/")
        self.assertEqual(resp.status_code, 200)
        results = resp.data["results"]
        titles = [v["title"] for v in results]
        self.assertIn("native", titles)
        self.assertIn("youtube_vid", titles)
        yt_item = next(v for v in results if v["title"] == "youtube_vid")
        self.assertEqual(yt_item["source_type"], "YOUTUBE")
        self.assertEqual(yt_item["youtube_video_id"], "dQw4w9WgXcQ")

    def test_guest_feed_includes_youtube_videos(self):
        Video.objects.create(
            author=self.other,
            category=self.gaming,
            title="youtube_guest",
            description="desc",
            is_approved=True,
            source_type="YOUTUBE",
            youtube_video_id="dQw4w9WgXcQ",
        )
        resp = self.client.get("/media/guestgetvideo/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("youtube_guest", [v["title"] for v in resp.data["results"]])

    def test_search_includes_youtube_videos(self):
        Video.objects.create(
            author=self.other,
            category=self.gaming,
            title="CreekTube Search YouTube Test",
            description="desc",
            is_approved=True,
            source_type="YOUTUBE",
            youtube_video_id="dQw4w9WgXcQ",
        )
        resp = self.client.get("/media/searchvideo/?q=Search YouTube Test")
        self.assertEqual(resp.status_code, 200)
        yt_items = [v for v in resp.data["videos"] if v["source_type"] == "YOUTUBE"]
        self.assertEqual(len(yt_items), 1)
        self.assertEqual(yt_items[0]["youtube_video_id"], "dQw4w9WgXcQ")


# ---------------------------------------------------------------------------
# Live YouTube mixing (the Data API, not stored rows)
# ---------------------------------------------------------------------------
VALID_YT_IDS = ["dQw4w9WgXcQ", "aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc"]


def fake_youtube_request(endpoint, params):
    """Canned YouTube Data API response for tests."""
    if endpoint == "search":
        query = (params.get("q") or "default").strip()
        items = []
        for idx, vid in enumerate(VALID_YT_IDS):
            items.append({
                "id": {"videoId": vid},
                "snippet": {
                    "title": f"{query} result {idx}",
                    "description": "A description",
                    "channelId": "UCfakechannel",
                    "channelTitle": "Fake Channel",
                    "publishedAt": "2024-01-01T00:00:00Z",
                },
            })
        return {"items": items}
    if endpoint == "videos":
        part = params.get("part", "")
        if params.get("chart") == "mostPopular":
            # The cheap default feed: no ``id`` param, just a chart.
            items = []
            for idx, vid in enumerate(VALID_YT_IDS):
                item = {"id": vid, "snippet": {
                    "title": f"Popular result {idx}",
                    "description": "A description",
                    "channelId": "UCfakechannel",
                    "channelTitle": "Fake Channel",
                    "publishedAt": "2024-01-01T00:00:00Z",
                }}
                if "contentDetails" in part:
                    try:
                        i = VALID_YT_IDS.index(vid)
                    except ValueError:
                        i = 0
                    item["contentDetails"] = {"duration": "PT45S" if i == 0 else "PT6M"}
                if "statistics" in part:
                    item["statistics"] = {"viewCount": "12345"}
                items.append(item)
            return {"items": items}
        items = []
        for vid in (params.get("id") or "").split(","):
            if not vid:
                continue
            item = {"id": vid}
            if "statistics" in part:
                item["statistics"] = {"viewCount": "12345"}
            if "snippet" in part:
                item["snippet"] = {
                    "title": "Video Details Title",
                    "description": "Details description",
                    "channelId": "UCfakechannel",
                    "channelTitle": "Fake Channel",
                    "publishedAt": "2024-01-01T00:00:00Z",
                }
            if "contentDetails" in part:
                # First mock id is a Short (PT45S), the rest are long-form so
                # the main video feed only ever mixes in long YouTube videos.
                try:
                    idx = VALID_YT_IDS.index(vid)
                except ValueError:
                    idx = 0
                item["contentDetails"] = {"duration": "PT45S" if idx == 0 else "PT6M"}
            items.append(item)
        return {"items": items}
    if endpoint == "channels":
        part = params.get("part", "")
        items = []
        for cid in (params.get("id") or "").split(","):
            if not cid:
                continue
            item = {
                "id": cid,
                "snippet": {
                    "title": "Fake Channel",
                    "customUrl": "@fakechannel",
                    "description": "A channel description",
                    "publishedAt": "2020-01-01T00:00:00Z",
                    "thumbnails": {
                        "default": {"url": "https://example.com/avatar.jpg"},
                        "medium": {"url": "https://example.com/avatar_m.jpg"},
                        "high": {"url": "https://example.com/avatar_h.jpg"},
                    },
                },
                "statistics": {"subscriberCount": "1000", "videoCount": "50", "viewCount": "9000"},
                "brandingSettings": {"image": {"bannerImageUrl": "https://example.com/banner.jpg"}},
            }
            if "contentDetails" in part:
                item["contentDetails"] = {"relatedPlaylists": {"uploads": "UUfakeuploadplaylist"}}
            items.append(item)
        return {"items": items}
    if endpoint == "playlistItems":
        items = []
        for idx, vid in enumerate(VALID_YT_IDS):
            items.append({
                "id": f"playlistItem{idx}",
                "snippet": {
                    "channelId": "UCfakechannel",
                    "channelTitle": "Fake Channel",
                    "title": f"Upload {idx}",
                    "description": "A description",
                    "publishedAt": "2024-01-01T00:00:00Z",
                    "resourceId": {"videoId": vid},
                },
            })
        return {"items": items}
    if endpoint == "commentThreads":
        items = []
        for idx in range(3):
            items.append({
                "id": f"yt_comment_{idx}",
                "snippet": {
                    "channelId": "UCfakechannel",
                    "videoId": params.get("videoId", ""),
                    "topLevelComment": {
                        "id": f"yt_comment_{idx}",
                        "snippet": {
                            "textDisplay": f"YouTube comment {idx}",
                            "authorDisplayName": "YT Commenter",
                            "authorProfileImageUrl": "https://example.com/commenter.jpg",
                            "authorChannelId": {"value": "UCfakechannel"},
                            "publishedAt": "2024-01-01T00:00:00Z",
                            "likeCount": "2",
                            "isPinned": idx == 0,
                        },
                    },
                },
            })
        return {"items": items}
    return None


class LiveYouTubeMixin:
    def mock_api(self):
        youtube_module._cache.clear()
        youtube_module._last_api_error = None
        youtube_module._quota_blocked_until = 0.0
        patcher_key = mock.patch.object(youtube_module, "_api_key", return_value="test-key")
        patcher_req = mock.patch.object(youtube_module, "_youtube_request", side_effect=fake_youtube_request)
        patcher_key.start()
        patcher_req.start()
        self.addCleanup(patcher_key.stop)
        self.addCleanup(patcher_req.stop)


class LiveYouTubeFeedTests(LiveYouTubeMixin, TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username="hybrid_interest", password="pw")
        self.other = User.objects.create_user(username="hybrid_author", password="pw")
        self.gaming = CategoryVideo.objects.create(name="Gaming", slug="gaming")
        self.music = CategoryVideo.objects.create(name="Music", slug="music")

    def test_guest_feed_mixes_live_youtube_items(self):
        self.mock_api()
        make_video(self.other, self.gaming, hours_old=1, title="native_one")
        resp = self.client.get("/media/guestgetvideo/")
        self.assertEqual(resp.status_code, 200)
        yt_items = [v for v in resp.data["results"] if v["source_type"] == "YOUTUBE"]
        self.assertTrue(len(yt_items) > 0)
        first = yt_items[0]
        self.assertEqual(first["source_type"], "YOUTUBE")
        self.assertIn(first["youtube_video_id"], VALID_YT_IDS)
        self.assertEqual(first["embed_url"], f"https://www.youtube.com/embed/{first['youtube_video_id']}")
        self.assertIn("i.ytimg.com", first["thumbnail"])
        self.assertIsNone(first["author_id"])
        self.assertEqual(first["view_count"], 12345)

    def test_logged_in_feed_mixes_and_interleaves(self):
        self.mock_api()
        self.client.force_authenticate(user=self.user)
        native = make_video(self.other, self.gaming, hours_old=1, title="native_primary")
        resp = self.client.get("/media/logingetvideo/")
        self.assertEqual(resp.status_code, 200)
        results = resp.data["results"]
        titles = [v["title"] for v in results]
        self.assertIn("native_primary", titles)
        yt_items = [v for v in results if v["source_type"] == "YOUTUBE"]
        self.assertTrue(len(yt_items) > 0)
        # YouTube items never repeat a native video's id or title.
        self.assertNotIn(native.id, [v["id"] for v in yt_items])

    def test_main_feed_excludes_youtube_shorts(self):
        self.mock_api()
        make_video(self.other, self.gaming, hours_old=1, title="native_main")
        resp = self.client.get("/media/guestgetvideo/")
        self.assertEqual(resp.status_code, 200)
        for v in resp.data["results"]:
            if v["source_type"] == "YOUTUBE":
                self.assertEqual(v["content_type"], "VIDEO")

    def test_homepage_snips_tab_mixes_youtube_shorts(self):
        self.mock_api()
        Snip.objects.create(
            author=self.other, title="native_snip_home", description="",
            video="native.mp4", thumbnail="", visibility="public",
            is_approved=True,
        )
        resp = self.client.get("/media/guestgetvideo/?category=shortform-videos")
        self.assertEqual(resp.status_code, 200)
        titles = [item["title"] for item in resp.data["results"]]
        self.assertIn("native_snip_home", titles)
        yt_shorts = [
            item for item in resp.data["results"]
            if item.get("source_type") == "YOUTUBE" and item.get("content_type") == "SNIP"
        ]
        self.assertTrue(len(yt_shorts) > 0)
        self.assertEqual(yt_shorts[0]["id"], VALID_YT_IDS[0])
        self.assertEqual(
            yt_shorts[0]["embed_url"],
            f"https://www.youtube.com/embed/{VALID_YT_IDS[0]}",
        )

    def test_logged_in_feed_builds_queries_from_interests(self):
        self.mock_api()
        profile, _ = MediaProfile.objects.get_or_create(user=self.user)
        profile.categories = {"gaming": 9, "music": 3}
        profile.save()
        queries = []

        def spy_request(endpoint, params):
            if endpoint == "search":
                queries.append(params.get("q"))
            return fake_youtube_request(endpoint, params)

        with mock.patch.object(youtube_module, "_youtube_request", side_effect=spy_request):
            self.client.force_authenticate(user=self.user)
            self.client.get("/media/logingetvideo/")
        self.assertTrue(any("gaming" in q for q in queries))

    def test_feed_without_api_key_is_native_only(self):
        with mock.patch.object(youtube_module, "_api_key", return_value=""):
            make_video(self.other, self.gaming, hours_old=1, title="only_native")
            resp = self.client.get("/media/guestgetvideo/")
        self.assertEqual(resp.status_code, 200)
        for v in resp.data["results"]:
            self.assertNotEqual(v["source_type"], "YOUTUBE")
        self.assertIn("only_native", [v["title"] for v in resp.data["results"]])

    def test_quota_error_degrades_to_native_feed(self):
        def boom(*args, **kwargs):
            return None

        with mock.patch.object(youtube_module, "_api_key", return_value="test-key"), \
             mock.patch.object(youtube_module, "_youtube_request", side_effect=boom):
            make_video(self.other, self.gaming, hours_old=1, title="native_survives")
            resp = self.client.get("/media/guestgetvideo/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("native_survives", [v["title"] for v in resp.data["results"]])

    def test_quota_error_is_surfaced_in_feed_payload(self):
        def boom(*args, **kwargs):
            youtube_module._record_api_error("search", 403, "quotaExceeded", "Quota exceeded")
            return None

        youtube_module._cache.clear()
        youtube_module._last_api_error = None
        youtube_module._quota_blocked_until = 0.0
        with mock.patch.object(youtube_module, "_api_key", return_value="test-key"), \
             mock.patch.object(youtube_module, "_youtube_request", side_effect=boom):
            make_video(self.other, self.gaming, hours_old=1, title="native_status")
            resp = self.client.get("/media/guestgetvideo/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("native_status", [v["title"] for v in resp.data["results"]])
        err = resp.data.get("youtube_error")
        self.assertIsNotNone(err)
        self.assertEqual(err["reason"], "quotaExceeded")

    def test_channel_videos_use_playlist_items_not_search(self):
        self.mock_api()
        calls = []

        def recording(endpoint, params):
            calls.append(endpoint)
            return fake_youtube_request(endpoint, params)

        with mock.patch.object(youtube_module, "_youtube_request", side_effect=recording):
            items = youtube_module.youtube_channel_videos("UCabcdefghijklmnopqrstuv", limit=4)
        self.assertGreater(len(items), 0)
        self.assertNotIn("search", calls)
        self.assertIn("playlistItems", calls)


class LiveYouTubeWatchTests(LiveYouTubeMixin, TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username="watch_yt", password="pw")

    def test_guestgetvideo_serves_live_youtube_id(self):
        self.mock_api()
        resp = self.client.get("/media/guestgetvideo/?video_id=dQw4w9WgXcQ")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["source_type"], "YOUTUBE")
        self.assertEqual(resp.data["youtube_video_id"], "dQw4w9WgXcQ")
        self.assertEqual(resp.data["embed_url"], "https://www.youtube.com/embed/dQw4w9WgXcQ")

    def test_guestwatch_serves_live_youtube_video(self):
        self.mock_api()
        resp = self.client.post("/media/guestwatchvideo/", {"video_id": "dQw4w9WgXcQ"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["video"]["source_type"], "YOUTUBE")
        self.assertEqual(resp.data["like_count"], 0)
        self.assertIs(resp.data["like"], False)
        self.assertGreaterEqual(len(resp.data["related_videos"]), 1)
        self.assertEqual(resp.data["related_videos"][0]["source_type"], "YOUTUBE")

    def test_loginwatch_serves_live_youtube_video(self):
        self.mock_api()
        self.client.force_authenticate(user=self.user)
        resp = self.client.post("/media/watchvideo/", {"video_id": "aaaaaaaaaaa"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["video"]["source_type"], "YOUTUBE")
        self.assertEqual(resp.data["video"]["youtube_video_id"], "aaaaaaaaaaa")
        self.assertEqual(resp.data["creek_count"], 0)

    def test_watch_unknown_id_returns_404(self):
        with mock.patch.object(youtube_module, "_api_key", return_value=""):
            resp = self.client.post("/media/guestwatchvideo/", {"video_id": "not-a-video-id"})
        self.assertEqual(resp.status_code, 404)

    def test_like_on_live_youtube_is_saved(self):
        self.client.force_authenticate(user=self.user)
        resp = self.client.post("/media/pikevideo/", {"id": "dQw4w9WgXcQ"})
        self.assertEqual(resp.status_code, 201)
        self.assertIs(resp.data["liked"], True)
        self.assertEqual(resp.data["creek_like_count"], 1)
        self.assertGreaterEqual(resp.data["like_count"], 0)
        self.assertIn("youtube_like_count", resp.data)
        # A stored row was materialized so the creek like persists.
        self.assertTrue(Video.objects.filter(youtube_video_id="dQw4w9WgXcQ").exists())
        resp = self.client.post("/media/pikevideo/", {"id": "dQw4w9WgXcQ"})
        self.assertEqual(resp.status_code, 200)
        self.assertIs(resp.data["liked"], False)
        self.assertEqual(resp.data["creek_like_count"], 0)
        # Dislikes work too once a stored row exists.
        resp = self.client.post("/media/dispikevideo/", {"id": "dQw4w9WgXcQ"})
        self.assertEqual(resp.status_code, 201)
        self.assertIs(resp.data["dispike"], True)
        resp = self.client.post("/media/dispikevideo/", {"id": "dQw4w9WgXcQ"})
        self.assertEqual(resp.status_code, 200)
        self.assertIs(resp.data["dispike"], False)

    def test_comments_on_live_youtube_are_served(self):
        self.mock_api()
        resp = self.client.get("/media/comment/?video_id=dQw4w9WgXcQ")
        self.assertEqual(resp.status_code, 200)
        self.assertGreaterEqual(len(resp.data), 1)
        first = resp.data[0]
        self.assertEqual(first["source"], "youtube")
        self.assertIs(first["read_only"], True)
        self.assertEqual(first["text"], "YouTube comment 0")

    def test_comment_submit_on_live_youtube_is_saved(self):
        self.client.force_authenticate(user=self.user)
        resp = self.client.post("/media/uploadcommentvideo/", {"video_id": "dQw4w9WgXcQ", "comment": "hi"})
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.data["comment"]["text"], "hi")
        # CreekTube comments on a live YouTube video are persisted, not rejected.
        video = Video.objects.get(youtube_video_id="dQw4w9WgXcQ")
        self.assertTrue(Comment.objects.filter(video=video, text="hi").exists())
        resp = self.client.get("/media/comment/?video_id=dQw4w9WgXcQ")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data[0]["text"], "hi")

    def test_trackretention_on_live_youtube_is_ok(self):
        self.client.force_authenticate(user=self.user)
        resp = self.client.post("/media/trackretention/", {"video_id": "dQw4w9WgXcQ", "duration": 30})
        self.assertEqual(resp.status_code, 200)


class LiveYouTubeSnipTests(LiveYouTubeMixin, TestCase):
    """YouTube Shorts served through the /snips endpoints."""

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username="snip_yt", password="pw")
        self.other = User.objects.create_user(username="snip_author", password="pw")

    def test_watch_snip_serves_live_youtube_short(self):
        self.mock_api()
        resp = self.client.get("/media/snip/watch/?id=dQw4w9WgXcQ")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["id"], "dQw4w9WgXcQ")
        self.assertEqual(resp.data["source_type"], "YOUTUBE")
        self.assertEqual(resp.data["youtube_video_id"], "dQw4w9WgXcQ")
        self.assertEqual(resp.data["embed_url"], "https://www.youtube.com/embed/dQw4w9WgXcQ")
        self.assertIsNone(resp.data["author_id"])
        self.assertIn("i.ytimg.com", resp.data["thumbnail"])
        # YouTube likes and CreekTube likes are exposed separately.
        self.assertIn("youtube_like_count", resp.data)
        self.assertIn("creek_like_count", resp.data)

    def test_snip_feed_includes_youtube_shorts(self):
        self.mock_api()
        Snip.objects.create(
            author=self.other, title="native_snip", description="",
            video="native.mp4", thumbnail="", visibility="public",
            is_approved=True,
        )
        resp = self.client.get("/media/snip/feed/")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.data["count"] > 0)
        yt_items = [item for item in resp.data["results"] if item.get("source_type") == "YOUTUBE"]
        self.assertTrue(len(yt_items) > 0)
        first = yt_items[0]
        self.assertEqual(first["source_type"], "YOUTUBE")
        self.assertIn(first["youtube_video_id"], VALID_YT_IDS)
        self.assertEqual(first["embed_url"], f"https://www.youtube.com/embed/{first['youtube_video_id']}")
        # Live items carry both the real YouTube like count and creek state.
        self.assertGreaterEqual(first["youtube_like_count"], 0)
        self.assertGreaterEqual(first["creek_like_count"], 0)
        self.assertIs(first["is_liked"], False)

    def test_snip_feed_without_api_key_is_native_only(self):
        with mock.patch.object(youtube_module, "_api_key", return_value=""):
            Snip.objects.create(
                author=self.other, title="native_only", description="",
                video="native.mp4", thumbnail="", visibility="public",
                is_approved=True,
            )
            resp = self.client.get("/media/snip/feed/")
        self.assertEqual(resp.status_code, 200)
        for item in resp.data["results"]:
            self.assertNotEqual(item.get("source_type"), "YOUTUBE")
        self.assertIn("native_only", [item["title"] for item in resp.data["results"]])

    def test_snip_comments_live_youtube_are_served(self):
        self.mock_api()
        resp = self.client.get("/media/snip/comments/?snip_id=dQw4w9WgXcQ")
        self.assertEqual(resp.status_code, 200)
        self.assertIsInstance(resp.data, list)

    def test_snip_comment_submit_on_live_youtube_rejected(self):
        self.mock_api()
        self.client.force_authenticate(user=self.user)
        resp = self.client.post("/media/snip/comment/", {"snip_id": "dQw4w9WgXcQ", "comment": "hi"})
        self.assertEqual(resp.status_code, 400)

    def test_like_on_live_youtube_short_is_saved(self):
        self.mock_api()
        self.client.force_authenticate(user=self.user)
        resp = self.client.post("/media/snip/like/", {"id": "dQw4w9WgXcQ"})
        self.assertEqual(resp.status_code, 201)
        self.assertIs(resp.data["is_liked"], True)
        # A stored row is materialized so the creek like persists.
        self.assertTrue(Video.objects.filter(youtube_video_id="dQw4w9WgXcQ").exists())
        resp = self.client.post("/media/snip/like/", {"id": "dQw4w9WgXcQ"})
        self.assertEqual(resp.status_code, 200)
        self.assertIs(resp.data["is_liked"], False)

    def test_like_does_not_reassign_author_or_date(self):
        """Liking a YouTube short must not claim it or reset its publish date."""
        self.mock_api()
        self.client.force_authenticate(user=self.user)
        self.client.post("/media/snip/like/", {"id": "dQw4w9WgXcQ"})

        row = Video.objects.get(youtube_video_id="dQw4w9WgXcQ")
        # The row is owned by the reserved system account, never the liker.
        self.assertEqual(row.author.username, YOUTUBE_SYSTEM_USERNAME)
        # The real publish date is preserved, not reset to "now".
        self.assertEqual(row.timestamp.date().isoformat(), "2024-01-01")

        # The serialized row shows the real channel as author and is addressed
        # by its YouTube ID so links keep working exactly like live items.
        serialized = VideoSerializer(row, context={"request": None}).data
        self.assertEqual(serialized["author"], "Fake Channel")
        self.assertEqual(serialized["id"], "dQw4w9WgXcQ")
        self.assertEqual(serialized["timestamp"][:10], "2024-01-01")

    def test_liked_youtube_video_is_not_duplicated_on_homepage(self):
        self.mock_api()
        self.client.force_authenticate(user=self.user)
        self.client.post("/media/pikevideo/", {"id": "aaaaaaaaaaa"})

        home = self.client.get("/media/logingetvideo/")
        self.assertEqual(home.status_code, 200)
        results = home.data["results"]
        # The materialized row must not appear as a native duplicate.
        self.assertEqual(
            [v for v in results if v.get("author") == self.user.username],
            [],
        )
        # The video is only present through its live feed item, with the real
        # channel name.
        live = [v for v in results if v.get("youtube_video_id") == "aaaaaaaaaaa"]
        self.assertEqual(len(live), 1)
        self.assertEqual(live[0]["author"], "Fake Channel")

    def test_trackretention_on_live_youtube_short_is_ok(self):
        self.client.force_authenticate(user=self.user)
        resp = self.client.post("/media/snip/trackretention/", {"snip_id": "dQw4w9WgXcQ", "duration": 30})
        self.assertEqual(resp.status_code, 200)

    def test_channel_snips_type_uses_short_filter(self):
        self.mock_api()
        resp = self.client.get(
            "/media/youtube/channel/",
            {"channel_id": "UCabcdefghijklmnopqrstuv", "type": "snips"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["type"], "snips")


# ---------------------------------------------------------------------------
# ensure_category race-safety
# ---------------------------------------------------------------------------
class EnsureCategoryTests(TestCase):
    def test_existing_slug_is_returned(self):
        cat = CategoryVideo.objects.create(slug="music", name="Music")
        self.assertEqual(ensure_category("music"), cat)

    def test_existing_name_is_reused_under_different_slug(self):
        existing = CategoryVideo.objects.create(slug="news-and-politics", name="News & Politics")
        got = ensure_category("news-and-politics", name="News & Politics")
        self.assertEqual(got.id, existing.id)
        self.assertEqual(CategoryVideo.objects.filter(name="News & Politics").count(), 1)

    def test_duplicate_name_is_returned_not_duplicated(self):
        existing = CategoryVideo.objects.create(slug="news-and-politics", name="News & Politics")
        got = ensure_category("news-and-politics", name="News & Politics")
        self.assertEqual(got.id, existing.id)
        self.assertEqual(CategoryVideo.objects.count(), 1)

    def test_racing_create_is_recovered(self):
        existing = CategoryVideo.objects.create(slug="sports", name="Sports")
        real_filter = CategoryVideo.objects.filter
        state = {"calls": 0}

        def fake_first(*args, **kwargs):
            state["calls"] += 1
            # Pre-checks miss, the post-IntegrityError refetch hits.
            return existing if state["calls"] >= 3 else None

        def fake_filter(*args, **kwargs):
            qs = real_filter(*args, **kwargs)
            qs.first = fake_first
            return qs

        with mock.patch.object(CategoryVideo.objects, "create", side_effect=IntegrityError("dup")), \
             mock.patch.object(CategoryVideo.objects, "filter", side_effect=fake_filter):
            got = ensure_category("sports", name="Sports")
        self.assertEqual(got.id, existing.id)


# ---------------------------------------------------------------------------
# recent_watch_keywords discovery-phrase extraction
# ---------------------------------------------------------------------------
class RecentWatchKeywordTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="kw_user", password="pw")
        self.other = User.objects.create_user(username="kw_author", password="pw")
        self.music = CategoryVideo.objects.create(name="Music", slug="music")

    def test_watch_query_phrase_keeps_leading_title_segment(self):
        from .views import _watch_query_phrase
        self.assertEqual(
            _watch_query_phrase("Sweater Weather - The NeighbourHood Lyrics Video"),
            "sweater weather",
        )

    def test_recent_watch_keywords_yield_phrase_not_bare_first_word(self):
        video = make_video(
            self.other, self.music,
            title="Sweater Weather - The NeighbourHood Lyrics Video",
        )
        make_watch_event(self.user, video)
        self.assertEqual(recent_watch_keywords(self.user), ["sweater weather"])
        self.assertNotIn("sweater", recent_watch_keywords(self.user))

    def test_recent_watch_keywords_prefer_youtube_channel_name(self):
        Video.objects.create(
            author=self.other,
            category=self.music,
            title="Sweater Weather - The NeighbourHood Lyrics Video",
            description="desc",
            is_approved=True,
            source_type="YOUTUBE",
            youtube_video_id="dQw4w9WgXcQ",
            youtube_channel_name="The Neighbourhood",
        )
        video = Video.objects.get(youtube_video_id="dQw4w9WgXcQ")
        make_watch_event(self.user, video)
        self.assertEqual(
            recent_watch_keywords(self.user),
            ["the neighbourhood", "sweater weather"],
        )


# ---------------------------------------------------------------------------
# Hashtag tags: extraction, learning, ranking, interest page
# ---------------------------------------------------------------------------
class TagFeatureTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username="tag_user", password="pw")
        self.other = User.objects.create_user(username="tag_author", password="pw")
        self.music = CategoryVideo.objects.create(name="Music", slug="music")

    def test_extract_hashtags_normalizes_and_dedupes(self):
        from .tags import extract_hashtags
        self.assertEqual(
            extract_hashtags("Set Fire #Music #Music #theneighbourhood", "desc #Gaming"),
            ["music", "theneighbourhood", "gaming"],
        )

    def test_apply_tags_from_title_and_description(self):
        from .tags import apply_tags, tag_names_for
        video = make_video(self.other, self.music, title="My Song #Music #theneighbourhood")
        video.description = "#concert"
        video.save()
        apply_tags(video, video.title, video.description)
        self.assertEqual(set(tag_names_for(video)), {"music", "theneighbourhood", "concert"})

    def test_watch_learns_tag_interests(self):
        from .tags import apply_tags
        from .views import record_tag_interest_from_watch
        video = make_video(self.other, self.music, title="Song #theneighbourhood")
        apply_tags(video, video.title, video.description)
        record_tag_interest_from_watch(self.user, video=video)
        profile = MediaProfile.objects.get(user=self.user)
        self.assertGreaterEqual(profile.tags.get("theneighbourhood", 0), 1)

    def test_tag_affinity_outranks_no_match(self):
        from .ranking import tag_affinity
        self.assertGreater(
            tag_affinity(["theneighbourhood"], {"theneighbourhood": 10}),
            tag_affinity(["someotherband"], {"theneighbourhood": 10}),
        )

    def test_interest_tag_page_returns_videos_and_snips(self):
        from .tags import apply_tags
        video = make_video(self.other, self.music, title="Song #music")
        apply_tags(video, video.title, video.description)
        snip = Snip.objects.create(
            author=self.other, category=self.music, title="Clip #music",
            description="d", is_approved=True, visibility="public",
        )
        apply_tags(snip, snip.title, snip.description)
        resp = self.client.get("/media/interests/music/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["tag"], "music")
        self.assertEqual(len(resp.data["videos"]), 1)
        self.assertEqual(len(resp.data["snips"]), 1)

    def test_following_feed_does_not_need_tags(self):
        # Sanity: LoginGetVideo without any tags still returns 200.
        self.client.force_authenticate(user=self.user)
        resp = self.client.get("/media/logingetvideo/")
        self.assertEqual(resp.status_code, 200)


# ---------------------------------------------------------------------------
# Superuser title panel + restricted moderator permissions
# ---------------------------------------------------------------------------
class AdminTitleTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.admin = User.objects.create_user(username="root", password="pw", is_superuser=True)
        self.mod_user = User.objects.create_user(username="moddy", password="pw")
        self.victim = User.objects.create_user(username="victim", password="pw")
        self.mod_profile = MediaProfile.objects.create(user=self.mod_user)
        self.victim_profile = MediaProfile.objects.create(user=self.victim)
        self.music = CategoryVideo.objects.create(name="Music", slug="music")

    def test_superuser_creates_and_grants_title(self):
        self.client.force_authenticate(user=self.admin)
        resp = self.client.post(
            "/media/admin-titles/",
            {"name": "Approver", "color": "#00ff00", "symbol": "S",
             "permissions": ["mod.approve", "bogus.perm"]},
            format="json",
        )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.data["permissions"], ["mod.approve"])

        grant = self.client.post(
            "/media/admin-users/",
            {"id": self.mod_profile.id, "title_id": resp.data["id"], "active": True},
            format="json",
        )
        self.assertEqual(grant.status_code, 200)
        self.mod_profile.refresh_from_db()
        self.assertTrue(self.mod_profile.is_moderator())
        self.assertTrue(self.mod_profile.has_permission("mod.approve"))
        self.assertFalse(self.mod_profile.has_permission("mod.deactivate"))

    def test_restricted_mod_cannot_deactivate_accounts(self):
        title = UserTitle.objects.create(
            name="Approver", color="#0f0", symbol="A", permissions=["mod.approve"]
        )
        self.mod_profile.titles.add(title)
        self.mod_profile.refresh_from_db()
        self.client.force_authenticate(user=self.mod_user)
        resp = self.client.post(
            "/media/set-account-active/",
            {"id": self.victim_profile.id, "active": False, "reason": "x"},
        )
        self.assertEqual(resp.status_code, 403)

    def test_restricted_mod_cannot_view_admin_panel(self):
        title = UserTitle.objects.create(
            name="Approver", color="#0f0", symbol="A", permissions=["mod.approve"]
        )
        self.mod_profile.titles.add(title)
        self.client.force_authenticate(user=self.mod_user)
        self.assertEqual(self.client.get("/media/admin-panel/").status_code, 403)

    def test_anonymous_cannot_create_titles(self):
        resp = self.client.post("/media/admin-titles/", {"name": "Rogue", "permissions": []})
        self.assertIn(resp.status_code, (401, 403))


# ---------------------------------------------------------------------------
# YouTube channel creeks (read-only subscriptions)
# ---------------------------------------------------------------------------
class YouTubeFollowTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username="follow_user", password="pw")
        self.client.force_authenticate(user=self.user)

    def test_creek_channel_toggle_and_list(self):
        payload = {
            "channel_id": "UCabcdefghijklmnopqrstuv",
            "channel_name": "Fake Channel",
            "channel_thumbnail": "http://thumb/1.jpg",
            "channel_handle": "@fakechannel",
        }
        resp = self.client.post("/media/creek-youtube-channel/", payload)
        self.assertEqual(resp.status_code, 201)
        self.assertTrue(resp.data["creek"])
        self.assertEqual(
            YouTubeChannelFollow.objects.filter(user=self.user, channel_id=payload["channel_id"]).count(), 1
        )

        follows = self.client.get("/media/youtube-follows/")
        self.assertEqual(follows.status_code, 200)
        self.assertEqual(len(follows.data), 1)
        self.assertEqual(follows.data[0]["channel_name"], "Fake Channel")
        self.assertEqual(follows.data[0]["channel_handle"], "fakechannel")

        resp = self.client.post("/media/creek-youtube-channel/", {"channel_id": payload["channel_id"]})
        self.assertEqual(resp.data["creek"], False)
        self.assertEqual(YouTubeChannelFollow.objects.filter(user=self.user).count(), 0)

    def test_creek_rejects_invalid_channel_id(self):
        resp = self.client.post("/media/creek-youtube-channel/", {"channel_id": "not-valid"})
        self.assertEqual(resp.status_code, 400)

    def test_following_feed_mixes_followed_channel_videos(self):
        from . import views as views_module
        author = User.objects.create_user(username="creator_a", password="pw")
        cat = CategoryVideo.objects.create(name="Music", slug="music")
        make_video(author, cat, is_approved=True, title="native")
        Creek.objects.create(author=self.user, account=MediaProfile.objects.get_or_create(user=author)[0])
        YouTubeChannelFollow.objects.create(
            user=self.user, channel_id="UCabcdefghijklmnopqrstuv", channel_name="Fake Channel",
        )
        fake_yt = {"id": "yt1", "source_type": "YOUTUBE", "title": "YT upload",
                   "thumbnail": "http://thumb/yt.jpg", "author": "Fake Channel",
                   "category": "music", "category_name": "Music",
                   "timestamp": "2024-01-01T00:00:00Z"}
        with mock.patch.object(views_module, "build_youtube_feed", return_value=[]), \
             mock.patch.object(views_module, "youtube_channel_videos", return_value=[fake_yt]):
            resp = self.client.get("/media/logingetvideo/?feed=following")
        self.assertEqual(resp.status_code, 200)
        ids = [v["id"] for v in resp.data["results"]]
        self.assertIn("yt1", ids)
        self.assertTrue(any(v["source_type"] == "YOUTUBE" for v in resp.data["results"]))


# ---------------------------------------------------------------------------
# Cross-source related videos (native <-> YouTube)
# ---------------------------------------------------------------------------
class CrossSourceRelatedTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="rel_user", password="pw")
        self.other = User.objects.create_user(username="rel_author", password="pw")
        self.music = CategoryVideo.objects.create(name="Music", slug="music")

    def test_native_video_mixes_youtube_related(self):
        from . import views as views_module
        from .tags import apply_tags
        video = make_video(self.other, self.music, title="Native #theneighbourhood")
        apply_tags(video, video.title, video.description)
        fake_yt = {"id": "yt1", "source_type": "YOUTUBE", "title": "YT",
                   "thumbnail": "", "author": "Ch", "category": "music",
                   "category_name": "Music", "timestamp": "2024-01-01T00:00:00Z"}
        with mock.patch.object(views_module, "youtube_search_results", return_value=[{}]), \
             mock.patch.object(views_module, "youtube_feed_item", return_value=fake_yt), \
             mock.patch.object(views_module, "_attach_youtube_enrichment"):
            related = views_module.related_youtube_for(video, limit=4)
        self.assertEqual([r["id"] for r in related], ["yt1"])

    def test_mixed_related_serializes_native_rows(self):
        from . import views as views_module
        from rest_framework.test import APIRequestFactory
        video = make_video(self.other, self.music, title="Native")
        req = APIRequestFactory().get("/")
        related = views_module.mixed_related_videos([video], [], req)
        self.assertEqual(len(related), 1)
        self.assertEqual(related[0]["id"], video.pk)
        self.assertEqual(related[0]["source_type"], "CREEKTUBE")

    def test_youtube_row_gets_native_related(self):
        from . import views as views_module
        native = make_video(self.other, self.music, title="Native")
        yt = Video.objects.create(
            author=self.other, category=self.music, title="YT",
            description="", is_approved=True, source_type="YOUTUBE",
            youtube_video_id="dQw4w9WgXcQ",
        )
        related = views_module.related_native_for(yt, limit=6)
        self.assertEqual([v.id for v in related], [native.id])


# ---------------------------------------------------------------------------
# Snip recommendation engine tests
# ---------------------------------------------------------------------------
class SnipRecommendationUnitTests(TestCase):
    """Unit-level scoring for the snips_rank engine."""

    def setUp(self):
        self.viewer = User.objects.create_user(username="snip_viewer", password="pw")
        self.creator = User.objects.create_user(username="snip_creator", password="pw")
        self.gaming = CategoryVideo.objects.create(name="Gaming", slug="gaming")
        self.cooking = CategoryVideo.objects.create(name="Cooking", slug="cooking")

    def test_interest_affinity_matches_top_category(self):
        self.assertEqual(snips_rank.interest_affinity("gaming", {"gaming": 10}), 1.0)
        self.assertEqual(snips_rank.interest_affinity("cooking", {"gaming": 10}), snips_rank.EXPLORATION_FLOOR)

    def test_topic_affinity_matches_known_tag(self):
        self.assertAlmostEqual(
            snips_rank.topic_affinity(["redstone"], {"redstone": 8, "farms": 4}), 1.0
        )
        self.assertEqual(snips_rank.topic_affinity(["music"], {"redstone": 8}), 0.0)

    def test_creator_affinity_prefers_followed_author(self):
        self.assertEqual(
            snips_rank.creator_affinity(self.creator.id, {self.creator.id}, {}), 1.0
        )

    def test_engagement_signal_log_scaled(self):
        low = snips_rank.engagement_signal(10, 1000)
        high = snips_rank.engagement_signal(100, 1000)
        self.assertGreater(high, low)

    def test_quality_signal_completion_rate(self):
        self.assertEqual(snips_rank.quality_signal(7.5, 15), 0.5)
        self.assertEqual(snips_rank.quality_signal(None, 15), 0.0)

    def test_recency_decays_with_age(self):
        fresh = timezone.now()
        old = timezone.now() - timedelta(hours=72)
        self.assertGreater(snips_rank.recency_score(fresh), snips_rank.recency_score(old))

    def test_dislike_flag_dominates_score(self):
        ctx = snips_rank.build_user_context(self.viewer)
        s = make_snip(self.creator, self.gaming, title="t")
        metrics = {"likes": 0, "dislikes": 1, "comment_count": 0,
                   "avg_duration_watched": None, "views": 10, "max_likes": 1, "max_views": 10}
        _, total_clean, _ = snips_rank.score_snip(s, ctx, metrics)
        ctx["disliked_ids"] = {s.id}
        _, total_disliked, _ = snips_rank.score_snip(s, ctx, metrics)
        self.assertLess(total_disliked, total_clean - 2.5)


class SnipRecommendationIntegrationTests(TestCase):
    """Engine-level recommendation behavior."""

    def setUp(self):
        self.viewer = User.objects.create_user(username="rec_viewer", password="pw")
        self.creator_a = User.objects.create_user(username="rec_creator_a", password="pw")
        self.creator_b = User.objects.create_user(username="rec_creator_b", password="pw")
        self.gaming = CategoryVideo.objects.create(name="Gaming", slug="gaming")
        self.cooking = CategoryVideo.objects.create(name="Cooking", slug="cooking")
        self.music = CategoryVideo.objects.create(name="Music", slug="music")

    def test_cold_start_returns_diverse_pool(self):
        for i in range(6):
            make_snip(self.creator_a, self.gaming, hours_old=i, title=f"a{i}", view_count=50)
        for i in range(3):
            make_snip(self.creator_b, self.cooking, hours_old=i, title=f"b{i}", view_count=50)
        ctx = snips_rank.build_user_context(self.viewer)
        result = snips_rank.build_recommended(self.viewer, ctx, limit=40)
        titles = [r["snip"].title for r in result]
        # Cold-start pool caps each author at 2 so both creators appear.
        self.assertEqual(len(titles), 4)
        self.assertLessEqual(len([t for t in titles if t.startswith("a")]), 2)
        self.assertLessEqual(len([t for t in titles if t.startswith("b")]), 2)
        self.assertTrue(all(r["reason"] for r in result))

    def test_topic_match_ranks_above_mismatch(self):
        redstone = Tag.objects.create(name="redstone")
        watched = make_snip(self.creator_a, self.gaming, hours_old=2, title="watched", tags=["redstone"])
        make_snip_event(self.viewer, watched, hours_ago=1, duration=15)
        matched = make_snip(self.creator_b, self.gaming, hours_old=1, title="matched", tags=["redstone"])
        mismatched = make_snip(self.creator_b, self.cooking, hours_old=1, title="mismatched")

        ctx = snips_rank.build_user_context(self.viewer)
        result = snips_rank.build_recommended(self.viewer, ctx, limit=10)
        titles = [r["snip"].title for r in result]
        self.assertLess(titles.index("matched"), titles.index("mismatched"))

    def test_disliked_snip_is_excluded(self):
        bad = make_snip(self.creator_a, self.gaming, hours_old=1, title="bad")
        good = make_snip(self.creator_b, self.gaming, hours_old=1, title="good")
        SnipDislike.objects.create(author=self.viewer, snip=bad)
        ctx = snips_rank.build_user_context(self.viewer)
        result = snips_rank.build_recommended(self.viewer, ctx, limit=10)
        titles = [r["snip"].title for r in result]
        self.assertNotIn("bad", titles)
        self.assertIn("good", titles)

    def test_not_interested_snip_is_hidden(self):
        bad = make_snip(self.creator_a, self.gaming, hours_old=1, title="hidden")
        good = make_snip(self.creator_b, self.gaming, hours_old=1, title="good")
        SnipFeedback.objects.create(
            author=self.viewer, snip=bad, kind=SnipFeedback.NOT_INTERESTED
        )
        ctx = snips_rank.build_user_context(self.viewer)
        result = snips_rank.build_recommended(self.viewer, ctx, limit=10)
        titles = [r["snip"].title for r in result]
        self.assertNotIn("hidden", titles)
        self.assertIn("good", titles)

    def test_abandoned_snip_is_penalized_but_not_removed(self):
        # The abandoned clip was watched ~9 days ago: outside the 7-day
        # "watched" exclusion window, so it stays a candidate and is only
        # penalized by the abandon signal.
        abandoned = make_snip(self.creator_a, self.gaming, hours_old=240, title="abandoned", duration=20)
        make_snip_event(self.viewer, abandoned, hours_ago=216, duration=1)
        fresh = make_snip(self.creator_b, self.gaming, hours_old=1, title="fresh", duration=20)
        ctx = snips_rank.build_user_context(self.viewer)
        result = snips_rank.build_recommended(self.viewer, ctx, limit=10)
        titles = [r["snip"].title for r in result]
        self.assertIn("abandoned", titles)
        self.assertLess(titles.index("fresh"), titles.index("abandoned"))

    def test_followed_creator_reason_surface(self):
        make_snip(self.creator_a, self.gaming, hours_old=1, title="followed_snip")
        Creek.objects.create(author=self.viewer, account=MediaProfile.objects.get_or_create(user=self.creator_a)[0])
        ctx = snips_rank.build_user_context(self.viewer)
        result = snips_rank.build_recommended(self.viewer, ctx, limit=10)
        reason = result[0]["reason"]
        self.assertEqual(reason, f"Because you follow @{self.creator_a.username}")

    def test_related_snips_tag_first_then_category(self):
        seed = make_snip(self.creator_a, self.gaming, hours_old=1, title="seed", tags=["redstone"])
        tag_related = make_snip(self.creator_b, self.cooking, hours_old=1, title="tag_related", tags=["redstone"])
        cat_related = make_snip(self.creator_b, self.gaming, hours_old=1, title="cat_related")
        make_snip(self.creator_b, self.music, hours_old=1, title="unrelated")

        related = snips_rank.related_snips(seed, limit=2)
        titles = [r.title for r in related]
        # Tag overlap ranks first, category match fills the rest; the recent
        # fallback is only used to top up when there aren't enough matches.
        self.assertEqual(titles, ["tag_related", "cat_related"])
        reason = snips_rank.reason_for_related(seed, related[0])
        self.assertEqual(reason, "More about #redstone")

    def test_trending_uses_momentum_not_personalization(self):
        trending = make_snip(self.creator_a, self.gaming, hours_old=1, title="trending", view_count=500)
        stale = make_snip(self.creator_b, self.gaming, hours_old=500, title="stale", view_count=1)
        for i in range(5):
            u = User.objects.create_user(username=f"trend_viewer_{i}")
            make_snip_event(u, trending, hours_ago=1, duration=15)
        result = snips_rank.build_trending(None, limit=10)
        titles = [r["snip"].title for r in result]
        self.assertLess(titles.index("trending"), titles.index("stale"))


class SnipRecommendationApiTests(TestCase):
    """API surface for the snip recommendation endpoints."""

    def setUp(self):
        self.client = APIClient()
        self.viewer = User.objects.create_user(username="api_viewer", password="pw")
        self.creator = User.objects.create_user(username="api_creator", password="pw")
        self.gaming = CategoryVideo.objects.create(name="Gaming", slug="gaming")
        self.cooking = CategoryVideo.objects.create(name="Cooking", slug="cooking")
        self.snip = make_snip(self.creator, self.gaming, title="api_snip", tags=["redstone"])

    def _auth(self):
        self.client.force_authenticate(user=self.viewer)

    def test_feed_returns_modes_and_contract(self):
        resp = self.client.get("/media/snip/feed/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("results", resp.data)
        self.assertIn("count", resp.data)
        self.assertIn("mode", resp.data)
        self.assertEqual(resp.data["mode"], "recommended")
        self.assertIn("youtube_error", resp.data)

    def test_feed_respects_exclude_ids(self):
        resp = self.client.get("/media/snip/feed/", {"exclude_ids": str(self.snip.id)})
        titles = [item["title"] for item in resp.data["results"]]
        self.assertNotIn("api_snip", titles)

    def test_feed_pagination(self):
        resp = self.client.get("/media/snip/feed/", {"page_size": 1, "page": 1})
        self.assertEqual(len(resp.data["results"]), 1)

    def test_dislike_is_mutually_exclusive_with_like(self):
        self._auth()
        resp = self.client.post("/media/snip/dislike/", {"id": self.snip.id})
        self.assertEqual(resp.status_code, 201)
        self.assertTrue(resp.data["is_disliked"])
        self.assertTrue(SnipDislike.objects.filter(author=self.viewer, snip=self.snip).exists())
        self.assertFalse(SnipLike.objects.filter(author=self.viewer, snip=self.snip).exists())

        resp = self.client.post("/media/snip/like/", {"id": self.snip.id})
        self.assertEqual(resp.status_code, 201)
        self.assertTrue(resp.data["is_liked"])
        self.assertFalse(resp.data["is_disliked"])
        self.assertFalse(SnipDislike.objects.filter(author=self.viewer, snip=self.snip).exists())

    def test_dislike_requires_auth(self):
        resp = self.client.post("/media/snip/dislike/", {"id": self.snip.id})
        self.assertEqual(resp.status_code, 401)

    def test_save_unsave_and_saved_list(self):
        self._auth()
        resp = self.client.post("/media/snip/save/", {"id": self.snip.id})
        self.assertEqual(resp.status_code, 201)
        self.assertTrue(resp.data["is_saved"])
        self.assertTrue(SnipSave.objects.filter(author=self.viewer, snip=self.snip).exists())

        resp = self.client.get("/media/snip/saved/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual([item["id"] for item in resp.data["results"]], [self.snip.id])

        resp = self.client.post("/media/snip/save/", {"id": self.snip.id})
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.data["is_saved"])
        self.assertFalse(SnipSave.objects.filter(author=self.viewer, snip=self.snip).exists())

    def test_feedback_not_interested_hides_from_feed(self):
        self._auth()
        resp = self.client.post(
            "/media/snip/feedback/",
            {"id": self.snip.id, "kind": "not_interested"},
        )
        self.assertEqual(resp.status_code, 201)
        feed = self.client.get("/media/snip/feed/", {"exclude_ids": ""})
        titles = [item["title"] for item in feed.data["results"]]
        self.assertNotIn("api_snip", titles)

    def test_feedback_report_requires_reason(self):
        self._auth()
        resp = self.client.post(
            "/media/snip/feedback/", {"id": self.snip.id, "kind": "report"}
        )
        self.assertEqual(resp.status_code, 400)
        resp = self.client.post(
            "/media/snip/feedback/", {"id": self.snip.id, "kind": "report", "reason": "spam"}
        )
        self.assertEqual(resp.status_code, 201)

    def test_feedback_invalid_kind_rejected(self):
        self._auth()
        resp = self.client.post(
            "/media/snip/feedback/", {"id": self.snip.id, "kind": "banana"}
        )
        self.assertEqual(resp.status_code, 400)

    def test_related_endpoint(self):
        related = make_snip(self.creator, self.gaming, title="related_api", tags=["redstone"])
        resp = self.client.get("/media/snip/related/", {"id": self.snip.id})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual([item["id"] for item in resp.data["results"]], [related.id])
        self.assertTrue(resp.data["results"][0]["reason"])

    def test_related_requires_id(self):
        resp = self.client.get("/media/snip/related/")
        self.assertEqual(resp.status_code, 400)

    def test_snip_search_by_title_tag_and_creator(self):
        other = make_snip(self.creator, self.cooking, title="cooking_short", tags=["farms"])
        resp = self.client.get("/media/snip/search/", {"q": "cooking"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual([item["id"] for item in resp.data["results"]], [other.id])

        resp = self.client.get("/media/snip/search/", {"q": "redstone"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual([item["id"] for item in resp.data["results"]], [self.snip.id])

        resp = self.client.get("/media/snip/search/", {"q": "api_creator"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn(self.snip.id, [item["id"] for item in resp.data["results"]])

    def test_watch_snip_learns_category_interest(self):
        self._auth()
        resp = self.client.get("/media/snip/watch/", {"id": self.snip.id})
        self.assertEqual(resp.status_code, 200)
        profile = MediaProfile.objects.get(user=self.viewer)
        self.assertGreater(profile.categories.get("gaming", 0), 0)

    def test_watch_records_session_id(self):
        self._auth()
        self.client.get("/media/snip/watch/", {"id": self.snip.id})
        event = WatchEvent.objects.filter(user=self.viewer, snip=self.snip).first()
        self.assertIsNotNone(event)
        self.assertTrue(event.session_id.startswith(f"{self.viewer.id}:"))

    def test_serializer_exposes_new_fields(self):
        self._auth()
        resp = self.client.get("/media/snip/watch/", {"id": self.snip.id})
        data = resp.data
        self.assertIn("is_saved", data)
        self.assertIn("is_disliked", data)
        self.assertIn("comment_count", data)
        self.assertIn("reason", data)
        self.assertIn("creator_followers", data)
        self.assertIn("is_followed", data)
        self.assertIn("tags", data)
        self.assertIn("category", data)
        self.assertIn("duration", data)


class BrokenChannelLayer:
    """Fake channel layer that raises like an unreachable Redis backplane."""

    async def group_add(self, group, channel):
        raise ConnectionError("redis down")

    async def group_discard(self, group, channel):
        raise ConnectionError("redis down")

    async def group_send(self, group, payload):
        raise ConnectionError("redis down")


class SnipFeedConsumerResilienceTests(IsolatedAsyncioTestCase):
    """A WebSocket connection must survive a missing/unreachable channel layer."""

    def _consumer(self):
        consumer = SnipFeedConsumer()
        consumer.scope = {"user": None}
        consumer.channel_name = "test_channel"
        return consumer

    async def test_group_add_sets_channel_ok_false_on_error(self):
        consumer = self._consumer()
        consumer.channel_layer = BrokenChannelLayer()
        await consumer._group_add()
        self.assertIs(consumer._channel_ok, False)

    async def test_group_send_skips_when_channel_down(self):
        consumer = self._consumer()
        consumer._channel_ok = False
        sent = []

        class Layer:
            async def group_send(self, group, payload):
                sent.append(payload)

        consumer.channel_layer = Layer()
        await consumer._group_send({"type": "viewer_count", "count": 1})
        self.assertEqual(sent, [])

    async def test_group_send_swallows_errors(self):
        consumer = self._consumer()
        consumer._channel_ok = True
        consumer.channel_layer = BrokenChannelLayer()
        await consumer._group_send({"type": "viewer_count", "count": 1})

    async def test_group_discard_swallows_errors(self):
        consumer = self._consumer()
        consumer._channel_ok = True
        consumer.channel_layer = BrokenChannelLayer()
        await consumer._group_discard()

    async def test_connect_accepts_when_channel_layer_down(self):
        consumer = self._consumer()
        consumer.channel_layer = BrokenChannelLayer()
        sent = []

        async def fake_send(payload):
            sent.append(payload)

        consumer.base_send = fake_send

        async def no_count():
            return 0

        consumer.get_viewer_count = no_count
        await consumer.connect()
        self.assertIs(consumer._channel_ok, False)
        self.assertTrue(any(p.get("type") == "websocket.accept" for p in sent))


class RedisConfigTests(SimpleTestCase):
    """Loopback REDIS_URLs must never enable the Redis channel/cache layer."""

    def _url(self):
        from burst.settings import base
        return base._redis_url()

    def test_loopback_urls_treated_as_unconfigured(self):
        for url in ("redis://localhost:6379",
                    "redis://127.0.0.1:6379/0",
                    "redis://0.0.0.0:6379",
                    "rediss://localhost:6379"):
            with mock.patch.dict(os.environ, {"REDIS_URL": url}):
                self.assertEqual(self._url(), "", url)

    def test_remote_url_is_usable(self):
        with mock.patch.dict(os.environ, {"REDIS_URL": "rediss://u:p@redis.example.com:6379"}):
            self.assertEqual(
                self._url(),
                "rediss://u:p@redis.example.com:6379",
            )

    def test_missing_url_is_unconfigured(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("REDIS_URL", None)
            self.assertEqual(self._url(), "")

