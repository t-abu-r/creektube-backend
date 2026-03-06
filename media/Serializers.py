from rest_framework import serializers
from accounts.models import Profile
import os
from .models import Video, MediaProfile, Comment, CategoryVideo

class CategoryVideoSerializer(serializers.ModelSerializer):
    class Meta:
        model = CategoryVideo
        fields = ["id", "name", "slug"]

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
    author_avatar = serializers.SerializerMethodField()
    thumbnail = serializers.SerializerMethodField()
    # Change this from a standard field to a MethodField
    video = serializers.SerializerMethodField()
    comments = CommentSerializer(many=True, read_only=True)
    category = serializers.SlugRelatedField(slug_field="slug", read_only=True)

    class Meta:
        model = Video
        fields = ["id", "category", "title", "description", "thumbnail", "video", "timestamp", "is_approved", "author", "author_avatar", "comments"]

    # --- THE FIX ---
    def get_video(self, obj):
        if not obj.video:
            return None
        url = obj.video.url
        if url.startswith("http"):
            return url
        # Construct the full Cloudinary URL
        return f"https://res.cloudinary.com/{os.environ.get('CLOUDINARY_CLOUD_NAME')}/{url}"

    def get_thumbnail(self, obj):
        if not obj.thumbnail:
            return None
        url = obj.thumbnail.url
        if url.startswith("http"):
            return url
        return f"https://res.cloudinary.com/{os.environ.get('CLOUDINARY_CLOUD_NAME')}/{url}"

    def get_author_avatar(self, obj):
        try:
            profile = getattr(obj.author, "profile", None)
            if profile and profile.avatar:
                url = profile.avatar.url
                # If it's already an absolute URL, return as-is
                if url.startswith("http"):
                    return url
                # If request exists, build absolute URI
                request = self.context.get("request")
                if request:
                    return request.build_absolute_uri(url)
                # Last resort: prepend Cloudinary URL
                return f"https://res.cloudinary.com/{os.environ.get('CLOUDINARY_CLOUD_NAME')}/{url}"
        except Exception:
            pass
        return None