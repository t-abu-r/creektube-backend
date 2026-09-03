from rest_framework import serializers
from accounts.models import Profile
import os
from .models import *
from .tags import tag_names_for
from .youtube import YOUTUBE_SYSTEM_USERNAME


class MediaProfileSerializer(serializers.ModelSerializer):
    username = serializers.CharField(source='user.username', read_only=True)
    avatar = serializers.SerializerMethodField()
    banner = serializers.SerializerMethodField()
    active = serializers.BooleanField(source='user.is_active', read_only=True)
    titles = serializers.SerializerMethodField()

    class Meta:
        model = MediaProfile
        fields = ["id", "username", "categories", "moderator", "official", "active", "avatar", "banner", "titles"]

    def get_titles(self, obj):
        return obj.title_payloads()

    def get_thumbnail(self, obj):
        request = self.context.get("request")
        try:
            profile = Profile.objects.get(user=obj.user)
            if profile.avatar:
                if request:
                    return request.build_absolute_uri(profile.avatar.url)
                return profile.avatar.url
            return None
        except Profile.DoesNotExist:
            return None

    def get_avatar(self, obj):
        request = self.context.get("request")
        try:
            profile = Profile.objects.get(user=obj.user)
            if profile.avatar:
                if request:
                    return request.build_absolute_uri(profile.avatar.url)
                return profile.avatar.url
            return None
        except Profile.DoesNotExist:
            return None


    def get_banner(self, obj):
        if not obj.banner:
            return None
        request = self.context.get("request")
        if request:
            return request.build_absolute_uri(obj.banner.url)
        return obj.banner.url


class CategoryVideoSerializer(serializers.ModelSerializer):
    video_count = serializers.IntegerField(read_only=True)
    count_videos = serializers.SerializerMethodField()

    class Meta:
        model = CategoryVideo
        fields = ["id", "name", "slug", "video_count", "count_videos"]

    def get_count_videos(self, obj):
        return obj.count_videos


class LikeSerializer(serializers.ModelSerializer):
    author = serializers.CharField(source="author.username", read_only=True)

    class Meta:
        model = Like
        fields = ["id", "author", "video", "created_at"]


class DisPikeSerializer(serializers.ModelSerializer):
    author = serializers.CharField(source="author.username", read_only=True)

    class Meta:
        model = DisPike
        fields = ["id", "author", "video", "created_at"]


class CreekSerializer(serializers.ModelSerializer):
    author = serializers.CharField(source="author.username", read_only=True)

    class Meta:
        model = Creek
        fields = ["id", "author", "account", "created_at"]


class CommentSerializer(serializers.ModelSerializer):
    author = serializers.CharField(source="author.username", read_only=True)
    author_id = serializers.SerializerMethodField()
    author_avatar = serializers.SerializerMethodField()
    is_pinned = serializers.BooleanField(read_only=True)
    replies = serializers.SerializerMethodField()
    edited = serializers.BooleanField(read_only=True)
    likes_count = serializers.SerializerMethodField()
    is_liked = serializers.SerializerMethodField()
    source = serializers.SerializerMethodField()
    read_only = serializers.SerializerMethodField()

    class Meta:
        model = Comment
        fields = ["id", "author", "author_id", "author_avatar", "text", "timestamp",
                  "updated_at", "is_pinned", "edited", "parent", "replies",
                  "likes_count", "is_liked", "source", "read_only"]

    def get_source(self, obj):
        return "creektube"

    def get_read_only(self, obj):
        return False

    def get_author_id(self, obj):
        try:
            return MediaProfile.objects.get(user=obj.author).id
        except MediaProfile.DoesNotExist:
            return None

    def get_author_avatar(self, obj):
        request = self.context.get("request")
        try:
            profile = Profile.objects.get(user=obj.author)
            if profile.avatar:
                if request:
                    return request.build_absolute_uri(profile.avatar.url)
                return profile.avatar.url
            return None
        except Profile.DoesNotExist:
            return None

    def get_replies(self, obj):
        if obj.parent is not None:
            return []
        replies = obj.replies.filter(author__is_active=True).order_by('timestamp')
        return CommentSerializer(replies, many=True, context=self.context).data

    def get_likes_count(self, obj):
        return obj.likes.count()

    def get_is_liked(self, obj):
        request = self.context.get("request")
        if request and request.user.is_authenticated:
            return obj.likes.filter(author=request.user).exists()
        return False


class SnipSerializer(serializers.ModelSerializer):
    author = serializers.CharField(source="author.username", read_only=True)
    author_id = serializers.SerializerMethodField()
    author_avatar = serializers.SerializerMethodField()
    author_active = serializers.SerializerMethodField()
    video = serializers.SerializerMethodField()
    thumbnail = serializers.SerializerMethodField()
    is_liked = serializers.SerializerMethodField()
    is_saved = serializers.SerializerMethodField()
    is_disliked = serializers.SerializerMethodField()
    source_type = serializers.SerializerMethodField()
    content_type = serializers.SerializerMethodField()
    duration = serializers.IntegerField(read_only=True)
    tags = serializers.SerializerMethodField()
    category = serializers.SerializerMethodField()
    category_name = serializers.SerializerMethodField()
    comment_count = serializers.SerializerMethodField()
    creator_followers = serializers.SerializerMethodField()
    is_followed = serializers.SerializerMethodField()
    creator_verified = serializers.SerializerMethodField()
    reason = serializers.SerializerMethodField()

    class Meta:
        model = Snip
        fields = [
            "id", "title", "description", "video", "thumbnail", "visibility", "timestamp",
            "is_approved", "author", "author_id", "author_avatar", "author_active",
            "view_count", "like_count", "dislike_count", "is_liked", "is_saved", "is_disliked",
            "source_type", "content_type", "duration", "tags", "category", "category_name",
            "comment_count", "creator_followers", "is_followed", "creator_verified", "reason",
        ]

    def _state_lookup(self, obj, key):
        """Batched lookup state (liked/saved/disliked ids) passed via context."""
        state = self.context.get(key)
        if isinstance(state, dict):
            return state.get(obj.id)
        if isinstance(state, (set, list)):
            return obj.id in state
        return None

    def get_tags(self, obj):
        return tag_names_for(obj)

    def get_author_active(self, obj):
        return obj.author.is_active

    def get_author_id(self, obj):
        profile = getattr(obj.author, "mediaprofile", None)
        return profile.pk if profile else obj.author.id

    def get_video(self, obj):
        if not obj.video:
            return None
        return obj.video

    def get_thumbnail(self, obj):
        if not obj.thumbnail:
            return None
        return obj.thumbnail

    def get_author_avatar(self, obj):
        try:
            profile = getattr(obj.author, "profile", None)
            if profile and profile.avatar:
                url = profile.avatar.url
                if url.startswith("http"):
                    return url
                return url.lstrip('/')
        except Exception:
            pass
        return None

    def get_is_liked(self, obj):
        known = self._state_lookup(obj, "snip_liked_ids")
        if known is not None:
            return bool(known)
        request = self.context.get("request")
        if request and request.user.is_authenticated:
            return obj.likes.filter(author=request.user).exists()
        return False

    def get_is_saved(self, obj):
        known = self._state_lookup(obj, "snip_saved_ids")
        if known is not None:
            return bool(known)
        request = self.context.get("request")
        if request and request.user.is_authenticated:
            return obj.saves.filter(author=request.user).exists()
        return False

    def get_is_disliked(self, obj):
        known = self._state_lookup(obj, "snip_disliked_ids")
        if known is not None:
            return bool(known)
        request = self.context.get("request")
        if request and request.user.is_authenticated:
            return obj.dislikes.filter(author=request.user).exists()
        return False

    def get_category(self, obj):
        return obj.category.slug if obj.category else None

    def get_category_name(self, obj):
        return obj.category.name if obj.category else None

    def get_comment_count(self, obj):
        if hasattr(obj, "comment_count") and obj.comment_count is not None:
            return obj.comment_count
        return obj.comments.count()

    def get_creator_followers(self, obj):
        if hasattr(obj, "creator_followers") and obj.creator_followers is not None:
            return obj.creator_followers
        batched = self.context.get("snip_creator_followers")
        if isinstance(batched, dict):
            return batched.get(obj.author_id, 0)
        try:
            return obj.author.mediaprofile.account.count() if getattr(obj.author, "mediaprofile", None) else 0
        except Exception:
            return 0

    def get_is_followed(self, obj):
        request = self.context.get("request")
        if request and request.user.is_authenticated:
            batched = self.context.get("snip_followed_author_ids")
            if isinstance(batched, set):
                return obj.author_id in batched
            profile = getattr(obj.author, "mediaprofile", None)
            if profile:
                from .models import Creek
                return Creek.objects.filter(author=request.user, account=profile).exists()
        return False

    def get_creator_verified(self, obj):
        profile = getattr(obj.author, "mediaprofile", None)
        if profile is None:
            return False
        return profile.is_moderator() or profile.is_official()

    def get_reason(self, obj):
        reasons = self.context.get("snip_reasons")
        if isinstance(reasons, dict):
            return reasons.get(obj.id)
        return None

    def get_source_type(self, obj):
        return "CREEKTUBE"

    def get_content_type(self, obj):
        return "SNIP"


class VideoSerializer(serializers.ModelSerializer):
    id = serializers.SerializerMethodField()
    author = serializers.SerializerMethodField()
    author_avatar = serializers.SerializerMethodField()
    author_active = serializers.SerializerMethodField()
    thumbnail = serializers.SerializerMethodField()
    video = serializers.SerializerMethodField()
    comments = serializers.SerializerMethodField()
    category = serializers.SlugRelatedField(slug_field="slug", read_only=True)
    category_name = serializers.SerializerMethodField()
    author_id = serializers.SerializerMethodField()
    view_count = serializers.IntegerField(read_only=True)
    source_type = serializers.CharField(read_only=True)
    youtube_video_id = serializers.CharField(read_only=True)
    youtube_channel_id = serializers.CharField(read_only=True)
    youtube_channel_name = serializers.CharField(read_only=True)
    embed_url = serializers.SerializerMethodField()
    content_type = serializers.CharField(read_only=True)
    duration = serializers.IntegerField(read_only=True)
    tags = serializers.SerializerMethodField()

    class Meta:
        model = Video
        fields = [
            "id", "category", "category_name", "title", "description", "thumbnail", "video", "visibility",
            "timestamp", "is_approved", "author", "author_id", "author_avatar", "author_active",
            "comments", "view_count", "source_type", "youtube_video_id", "youtube_channel_id",
            "youtube_channel_name", "embed_url", "content_type", "duration", "tags",
        ]

    def get_tags(self, obj):
        return tag_names_for(obj)

    def get_author_active(self, obj):
        return obj.author.is_active

    def get_id(self, obj):
        # Auto-materialized YouTube rows (owned by the reserved system account)
        # are addressed by their YouTube ID so every link behaves exactly like
        # the live feed item. Creator-added YouTube videos keep their real id.
        if (
            obj.source_type == "YOUTUBE"
            and obj.youtube_video_id
            and obj.author.username == YOUTUBE_SYSTEM_USERNAME
        ):
            return obj.youtube_video_id
        return obj.pk

    def get_author(self, obj):
        # Materialized YouTube rows are owned by the system account for
        # bookkeeping; show the real YouTube channel name instead.
        if (
            obj.source_type == "YOUTUBE"
            and obj.author.username == YOUTUBE_SYSTEM_USERNAME
        ):
            return obj.youtube_channel_name or "YouTube"
        return obj.author.username

    def get_category_name(self, obj):
        if obj.category:
            return obj.category.name
        return None

    def get_comments(self, obj):
        comments = obj.comments.filter(parent=None, author__is_active=True).order_by('-is_pinned', '-timestamp')
        return CommentSerializer(comments, many=True, context=self.context).data

    def get_author_id(self, obj):
        if obj.source_type == "YOUTUBE":
            return None
        try:
            return MediaProfile.objects.get(user=obj.author).id
        except MediaProfile.DoesNotExist:
            return None

    def get_video(self, obj):
        if not obj.video:
            return None
        return obj.video

    def get_embed_url(self, obj):
        if obj.source_type == "YOUTUBE" and obj.youtube_video_id:
            from .youtube import youtube_embed_url
            return youtube_embed_url(obj.youtube_video_id)
        return None

    def get_thumbnail(self, obj):
        if not obj.thumbnail:
            return None
        return obj.thumbnail

    def get_author_avatar(self, obj):
        if obj.source_type == "YOUTUBE" and obj.youtube_channel_id:
            from .youtube import youtube_channel_avatars_for
            return youtube_channel_avatars_for([obj.youtube_channel_id]).get(obj.youtube_channel_id)
        try:
            profile = getattr(obj.author, "profile", None)
            if profile and profile.avatar:
                url = profile.avatar.url
                if url.startswith("http"):
                    return url
                return url.lstrip('/')
        except Exception:
            pass
        return None
class NotificationSerializer(serializers.ModelSerializer):
    actor_name = serializers.CharField(source="actor.username", read_only=True)
    actor_avatar = serializers.SerializerMethodField()
    time_ago = serializers.SerializerMethodField()

    class Meta:
        model = Notification
        fields = ["id", "recipient", "actor", "actor_name", "actor_avatar", "verb",
                  "target_type", "target_id", "extra_data", "is_read", "timestamp", "time_ago"]

    def get_actor_avatar(self, obj):
        request = self.context.get("request")
        try:
            profile = Profile.objects.get(user=obj.actor)
            if profile.avatar:
                if request:
                    return request.build_absolute_uri(profile.avatar.url)
                return profile.avatar.url
            return None
        except Profile.DoesNotExist:
            return None

    def get_time_ago(self, obj):
        from django.utils import timezone
        now = timezone.now()
        diff = now - obj.timestamp
        if diff.days > 0:
            return f"{diff.days}d ago"
        if diff.seconds >= 3600:
            return f"{diff.seconds // 3600}h ago"
        if diff.seconds >= 60:
            return f"{diff.seconds // 60}m ago"
        return "Just now"
