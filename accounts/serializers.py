from rest_framework import serializers
from .models import Profile

class ProfileSerializer(serializers.ModelSerializer):
    avatar = serializers.ImageField(required=False, allow_null=True)
    avatar_url = serializers.SerializerMethodField()

    class Meta:
        model = Profile
        fields = ("id", "user", "plan", "avatar", "avatar_url", "bio")

    def get_avatar_url(self, obj):
        request = self.context.get("request")
        avatar = getattr(obj, 'avatar', None)
        if not avatar:
            return None
        url = avatar.url
        if url.startswith("http"):
            return url
        if request:
            return request.build_absolute_uri(url)
        return url