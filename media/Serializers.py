from rest_framework import serializers
from accounts.models import Profile
import os
from .models import *


class MediaProfileSerializer(serializers.ModelSerializer):
    username = serializers.CharField(source='user.username', read_only=True)
    avatar = serializers.SerializerMethodField()
    banner = serializers.SerializerMethodField()
    active = serializers.BooleanField(source='user.is_active', read_only=True)

    class Meta:
        model = MediaProfile
        fields = ["id", "username", "categories", "moderator", "official", "active", "avatar", "banner"]

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

    class Meta:
        model = Comment
        fields = ["id", "author", "author_id", "author_avatar", "text", "timestamp",
                  "updated_at", "is_pinned", "edited", "parent", "replies",
                  "likes_count", "is_liked"]

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

    class Meta:
        model = Snip
        fields = [
            "id", "title", "description", "video", "thumbnail", "visibility", "timestamp",
            "is_approved", "author", "author_id", "author_avatar", "author_active",
            "view_count", "like_count", "is_liked",
        ]

    def get_author_active(self, obj):
        return obj.author.is_active

    def get_author_id(self, obj):
        try:
            return MediaProfile.objects.get(user=obj.author).id
        except MediaProfile.DoesNotExist:
            return None

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
        request = self.context.get("request")
        if request and request.user.is_authenticated:
            return obj.likes.filter(author=request.user).exists()
        return False


class VideoSerializer(serializers.ModelSerializer):
    author = serializers.CharField(source="author.username", read_only=True)
    author_avatar = serializers.SerializerMethodField()
    author_active = serializers.SerializerMethodField()
    thumbnail = serializers.SerializerMethodField()
    video = serializers.SerializerMethodField()
    comments = serializers.SerializerMethodField()
    category = serializers.SlugRelatedField(slug_field="slug", read_only=True)
    category_name = serializers.SerializerMethodField()
    author_id = serializers.SerializerMethodField()
    view_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = Video
        fields = [
            "id", "category", "category_name", "title", "description", "thumbnail", "video", "visibility",
            "timestamp", "is_approved", "author", "author_id", "author_avatar", "author_active",
            "comments", "view_count",
        ]

    def get_author_active(self, obj):
        return obj.author.is_active

    def get_category_name(self, obj):
        if obj.category:
            return obj.category.name
        return None

    def get_comments(self, obj):
        comments = obj.comments.filter(parent=None, author__is_active=True).order_by('-is_pinned', '-timestamp')
        return CommentSerializer(comments, many=True, context=self.context).data

    def get_author_id(self, obj):
        try:
            return MediaProfile.objects.get(user=obj.author).id
        except MediaProfile.DoesNotExist:
            return None

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
