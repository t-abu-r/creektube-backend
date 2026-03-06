from rest_framework import serializers
from .models import Profile
import os

class ProfileSerializer(serializers.ModelSerializer):
    avatar = serializers.ImageField(required=False, allow_null=True)
    avatar_url = serializers.SerializerMethodField()

    class Meta:
        model = Profile
        fields = ("id", "user", "plan", "avatar", "avatar_url")

    def get_avatar_url(self, obj):
        if not obj.avatar:
            return None

        url = obj.avatar.url
        # Already full URL (http/https)
        if url.startswith("http"):
            return url

        # Construct Cloudinary URL if relative
        return f"https://res.cloudinary.com/{os.environ.get('CLOUDINARY_CLOUD_NAME')}/{url.lstrip('/')}"