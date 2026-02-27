from rest_framework import serializers
from accounts.models import Profile
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
    comments = CommentSerializer(many=True, read_only=True)
    category = serializers.SlugRelatedField(
        slug_field="slug",  # or "name" if you prefer
        read_only=True
    )

    class Meta:
        model = Video
        fields = [
            "id",
            "category",
            "title",
            "description",
            "thumbnail",
            "video",
            "timestamp",
            "is_approved",
            "author",
            "author_avatar",
            "comments",
        ]

    def get_author_avatar(self, obj):
        try:
            profile = getattr(obj.author, "profile", None)
            if profile and profile.avatar:
                request = self.context.get("request")
                if request:
                    return request.build_absolute_uri(profile.avatar.url)
                return profile.avatar.url
        except Exception:
            pass
        return None