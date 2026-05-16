from rest_framework import serializers
from .models import Profile

class ProfileSerializer(serializers.ModelSerializer):
    avatar = serializers.ImageField(required=False, allow_null=True)
    avatar_url = serializers.SerializerMethodField()

    class Meta:
        model = Profile
        fields = ("id", "user", "plan", "avatar", "avatar_url", "bio")

    def get_avatar_url(self, obj):
        from django.conf import settings
        avatar = getattr(obj, 'avatar', None)
        if not avatar:
            return None

        if settings.DEBUG:
            # In dev, return local path
            request = self.context.get("request")
            url = f"/uploads/avatars/{str(avatar)}"
            if request:
                return request.build_absolute_uri(url)
            return url

        # In production, return Cloudinary URL
        return avatar.url