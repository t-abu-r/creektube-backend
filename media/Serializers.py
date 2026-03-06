from rest_framework import serializers
from accounts.models import Profile
import os
from .models import Video, Comment, CategoryVideo

class CategoryVideoSerializer(serializers.ModelSerializer):
    class Meta:
        model = CategoryVideo
        fields = ["id", "name", "slug"]

class CommentSerializer(serializers.ModelSerializer):
    author = serializers.CharField(source="author.username", read_only=True)
    author_avatar = serializers.SerializerMethodField()

    class Meta:
        model = Comment
        fields = ["id", "author", "author_avatar", "text", "timestamp"]

    def get_author_avatar(self, obj):
        try:
            profile = Profile.objects.get(user=obj.author)
            if profile.avatar:
                url = profile.avatar.url
                if url.startswith("http"):
                    return url
                return f"https://res.cloudinary.com/{os.environ.get('CLOUDINARY_CLOUD_NAME')}/{url.lstrip('/')}"
        except Profile.DoesNotExist:
            return None

class VideoSerializer(serializers.ModelSerializer):
    author = serializers.CharField(source="author.username", read_only=True)
    author_avatar = serializers.SerializerMethodField()
    thumbnail = serializers.SerializerMethodField()
    video = serializers.SerializerMethodField()
    comments = CommentSerializer(many=True, read_only=True)
    category = serializers.SlugRelatedField(slug_field="slug", read_only=True)

    class Meta:
        model = Video
        fields = [
            "id", "category", "title", "description",
            "thumbnail", "video", "timestamp", "is_approved",
            "author", "author_avatar", "comments"
        ]

    def get_author_avatar(self, obj):
        try:
            profile = getattr(obj.author, "profile", None)
            if profile and profile.avatar:
                url = profile.avatar.url
                if url.startswith("http"):
                    return url
                return f"https://res.cloudinary.com/{os.environ.get('CLOUDINARY_CLOUD_NAME')}/{url.lstrip('/')}"
        except Exception:
            return None

    def get_video(self, obj):
        if not obj.video:
            return None
        url = obj.video.url
        if url.startswith("http"):
            return url
        return f"https://res.cloudinary.com/{os.environ.get('CLOUDINARY_CLOUD_NAME')}/{url.lstrip('/')}"

    def get_thumbnail(self, obj):
        if not obj.thumbnail:
            return None
        url = obj.thumbnail.url
        if url.startswith("http"):
            return url
        return f"https://res.cloudinary.com/{os.environ.get('CLOUDINARY_CLOUD_NAME')}/{url.lstrip('/')}"