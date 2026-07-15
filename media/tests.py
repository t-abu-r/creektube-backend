from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from . import ranking
from .models import CategoryVideo, Creek, DisPike, Like, MediaProfile, Video


def make_video(author, category, hours_old=0, is_approved=True, title="video"):
    """Helper: create a Video with a backdated timestamp (timestamp is
    auto_now_add, so we create then patch it directly with .update() to
    avoid re-triggering auto_now_add)."""
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


# ---------------------------------------------------------------------------
# Pure-function unit tests for media/ranking.py — no DB required beyond what
# Django's TestCase sets up by default.
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
        # 100 net likes shouldn't score 10x as high as 10 net likes —
        # log-dampening should compress the gap.
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


# ---------------------------------------------------------------------------
# Integration tests exercising real Video/CategoryVideo rows through
# rank_videos(), so category joins and ordering behave as expected together.
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

        # Old bucket-based algorithm would always put every "gaming" video
        # before any "cooking" video for a user whose top interest is
        # gaming. The new composite score should let a fresh, highly
        # engaged off-category video surface above a stale favorite.
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
        # With equal engagement/interest, freshest should win on recency alone.
        self.assertEqual(ranked[0].title, "v1")


# ---------------------------------------------------------------------------
# API-level tests: make sure the views actually use the ranking module
# end-to-end, and that feedback loops (watch/dislike) move category scores
# the right direction.
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
        self.client.post("/media/dispikevideo/", {"id": self.video.id})  # dislike
        self.profile.refresh_from_db()
        after_dislike = self.profile.categories.get("gaming")

        resp = self.client.post("/media/dispikevideo/", {"id": self.video.id})  # undo
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