from rest_framework.serializers import ModelSerializer
from .models import *

class ProfileSerializer(ModelSerializer):
    avatar = serializers.ImageField(read_only=True)
    
    class Meta:
        model = Profile
        fields = ("id", "user", "plan", "avatar")