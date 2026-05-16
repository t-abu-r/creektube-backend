from rest_framework import serializers
from .models import Profile
from django.conf import settings

class ProfileSerializer(serializers.ModelSerializer):
    avatar = serializers.ImageField(required=False, allow_null=True)
    avatar_url = serializers.SerializerMethodField()

    class Meta:
        model = Profile
        fields = ("id", "user", "plan", "avatar", "avatar_url", "bio")

    def get_avatar_url(self, obj):

        avatar = getattr(obj, 'avatar', None)
        if not avatar:
            return None

        if settings.DEBUG:
            request = self.context.get("request")
            # avatar.name gives 'uploads/avatars/filename.jpg'
            url = f"/{avatar.name}"
            if request:
                return request.build_absolute_uri(url)
            return url

        # In production, return Cloudinary URL
        url = avatar.url
        if url.startswith('http'):
            return url
        return url