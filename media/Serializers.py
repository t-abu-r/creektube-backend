from rest_framework import serializers
from accounts.models import Profile
import os
from .models import *


class MediaProfileSerializer(serializers.ModelSerializer):
    username = serializers.CharField(source='user.username', read_only=True)
    class Meta:
        model = MediaProfile
        fields = ["id", "username", "categories", "moderator", "official"]

class CategoryVideoSerializer(serializers.ModelSerializer):
    video_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = CategoryVideo
        fields = ["id", "name", "slug", "video_count"]

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
    author_avatar = serializers.SerializerMethodField()  # method must be `get_author_avatar`

    class Meta:
        model = Comment
        fields = ["id", "author", "author_avatar", "text", "timestamp"]  # use your actual model field name

    # This method name MUST match the SerializerMethodField name
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


class VideoSerializer(serializers.ModelSerializer):
    author = serializers.CharField(source="author.username", read_only=True)
    author_avatar = serializers.SerializerMethodField()  # ← was "avatar"
    thumbnail = serializers.SerializerMethodField()
    video = serializers.SerializerMethodField()
    comments = CommentSerializer(many=True, read_only=True)
    category = serializers.SlugRelatedField(slug_field="slug", read_only=True)
    author_id = serializers.SerializerMethodField()  # ← add this

    class Meta:
        model = Video
        fields = ["id", "category", "title", "description", "thumbnail", "video", "timestamp", "is_approved", "author", "author_id", "author_avatar", "comments"]

    def get_author_id(self, obj):
        try:
            return MediaProfile.objects.get(user=obj.author).id
        except MediaProfile.DoesNotExist:
            return None

    def get_video(self, obj):
        if not obj.video:
            return None
        if hasattr(obj.video, 'url'):
            new_url = obj.video.url
            clean = new_url.removeprefix("/")
            return clean
        return None

    def get_thumbnail(self, obj):
        if not obj.thumbnail:
            return None
        if hasattr(obj.thumbnail, 'url'):
            url = obj.thumbnail.url
            if os.environ.get('DEBUG') == 'True':
                new_url = url
                clean = new_url.removeprefix("/")
                return clean
            if url.startswith("http"):
                return url
            return f"https://res.cloudinary.com/{os.environ.get('CLOUDINARY_CLOUD_NAME')}/{url.lstrip('/')}"
        return None

    def get_author_avatar(self, obj):
        try:
            profile = getattr(obj.author, "profile", None)
            if profile and profile.avatar:
                url = profile.avatar.url

                if url.startswith("http"):
                    return f"https://res.cloudinary.com/{os.environ.get('CLOUDINARY_CLOUD_NAME')}/{url.lstrip('/')}"

                return url.lstrip('/')

        except Exception:
            pass
        return None