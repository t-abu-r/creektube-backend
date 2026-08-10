from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from . import ranking
from .models import (CategoryVideo, Comment, Creek, DisPike, Like, MediaProfile,
                     Snip, Video, WatchEvent, UploadRateLimit)
from . import youtube as youtube_module
from .youtube import (normalize_youtube_url, validate_youtube_id,
                      youtube_embed_url, youtube_thumbnail_url, get_video_metadata,
                      YOUTUBE_SYSTEM_USERNAME)
from .Serializers import VideoSerializer


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
        items = []
        for cid in (params.get("id") or "").split(","):
            if not cid:
                continue
            items.append({
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
            raise Exception("quota exceeded")

        with mock.patch.object(youtube_module, "_api_key", return_value="test-key"), \
             mock.patch.object(youtube_module, "_youtube_request", side_effect=boom):
            make_video(self.other, self.gaming, hours_old=1, title="native_survives")
            resp = self.client.get("/media/guestgetvideo/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("native_survives", [v["title"] for v in resp.data["results"]])


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

