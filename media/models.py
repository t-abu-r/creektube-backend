from django.db import models
from django.contrib.auth.models import User
from cloudinary.models import CloudinaryField

# -----------------------------
# Category model (global)
# -----------------------------
class CategoryVideo(models.Model):
    name = models.CharField(max_length=100, unique=True)
    slug = models.SlugField(unique=True)

    def __str__(self):
        return self.name

# -----------------------------
# User profile with categories
# -----------------------------
class MediaProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    # categories stored as dict: {category_slug: score}
    categories = models.JSONField(default=dict)
    moderator = models.BooleanField(default=False)
    official = models.BooleanField(default=False)

    def __str__(self):
        return self.user.username

# -----------------------------
# Video model
# -----------------------------
class Video(models.Model):
    author = models.ForeignKey(User, on_delete=models.CASCADE)
    category = models.ForeignKey(
        CategoryVideo,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="videos",
    )
    # TOD0 Add a "top 5 interests" feature
        # categories dict looks like {"nature": 12, "gaming": 8, "music": 5}
        # To get top 5: sorted(self.categories.items(), key=lambda x: x[1], reverse=True)[:5]
        # Could build: a profile page showing top 5, or an onboarding screen to pick interests

    title = models.CharField(max_length=100)
    description = models.TextField()
    thumbnail = CloudinaryField(resource_type="image")
    video = CloudinaryField(resource_type="video")
    timestamp = models.DateTimeField(auto_now_add=True)
    is_approved = models.BooleanField(default=False)

    def __str__(self):
        return self.title

# -----------------------------
# Comments
# -----------------------------
class Comment(models.Model):
    author = models.ForeignKey(User, on_delete=models.CASCADE)
    video = models.ForeignKey(Video, related_name="comments", on_delete=models.CASCADE, null=True)
    text = models.TextField(max_length=500)
    timestamp = models.DateTimeField(auto_now_add=True)

# -----------------------------
# Pikes & Creek
# -----------------------------
class Like(models.Model):
    author = models.ForeignKey(User, on_delete=models.CASCADE)
    video = models.ForeignKey(Video, on_delete=models.CASCADE, related_name='likes')
    created_at = models.DateTimeField(auto_now_add=True, null=True)

    def __str__(self):
        return f"{self.author.username} Piked {self.video.title}"

class DisPike(models.Model):
    author = models.ForeignKey(User, on_delete=models.CASCADE)
    video = models.ForeignKey(Video, on_delete=models.CASCADE, related_name='likes')
    created_at = models.DateTimeField(auto_now_add=True, null=True)

    def __str__(self):
        return f"{self.author.username} DisPiked {self.video.title}"

class Creek(models.Model):
    author = models.ForeignKey(User, on_delete=models.CASCADE)
    account = models.ForeignKey(MediaProfile, on_delete=models.CASCADE, related_name='MediaProfile')
    created_at = models.DateTimeField(auto_now_add=True, null=True)

    def __str__(self):
        return f"{self.author.username} Creeked {self.account.username}"
