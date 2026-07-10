from django.test import TestCase
from django.contrib.auth.models import User
from media.models import MediaProfile


class ProfileCreationTest(TestCase):
    def test_media_profile_created_via_view(self):
        user = User.objects.create_user(username="testuser", password="testpass123")
        MediaProfile.objects.get_or_create(user=user)
        self.assertTrue(MediaProfile.objects.filter(user=user).exists())
