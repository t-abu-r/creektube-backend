from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from . import ranking
from .models import (CategoryVideo, Creek, DisPike, Like, MediaProfile,
                     Snip, Video, WatchEvent, UploadRateLimit, UserTitle)


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
