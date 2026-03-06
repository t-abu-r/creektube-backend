from rest_framework import serializers
from .models import Profile
import os

class ProfileSerializer(serializers.ModelSerializer):
    avatar = serializers.ImageField(required=False, allow_null=True)
    avatar_url = serializers.SerializerMethodField()  # computed full URL

    class Meta:
        model = Profile
        fields = ("id", "user", "plan", "avatar", "avatar_url")

    def get_avatar_url(self, obj):
        request = self.context.get("request")
        if obj.avatar:
            url = obj.avatar.url
            # If already full URL (Cloudinary), return as-is
            if url.startswith("http") and "res.cloudinary.com" in url:
                return url
            # If request context exists, build absolute URI (local dev)
            if request:
                return request.build_absolute_uri(url)
            # Otherwise, construct Cloudinary URL manually
            return f"https://res.cloudinary.com/{os.environ.get('CLOUDINARY_CLOUD_NAME')}/{url.lstrip('/')}"
        return None