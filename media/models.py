from django.db import models
from django.contrib.auth.models import User

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

    def __str__(self):
        return self.user.username

# -----------------------------
# Video model
# -----------------------------
class Video(models.Model):
    author = models.ForeignKey(User, on_delete=models.CASCADE)
    # Only the new FK, no old category
    category = models.ForeignKey(
        CategoryVideo,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="videos"
    )
    # TOD0 Add a "top 5 interests" feature
        # categories dict looks like {"nature": 12, "gaming": 8, "music": 5}
        # To get top 5: sorted(self.categories.items(), key=lambda x: x[1], reverse=True)[:5]
        # Could build: a profile page showing top 5, or an onboarding screen to pick interests

    title = models.CharField(max_length=100)
    description = models.TextField()
    thumbnail = models.ImageField(upload_to='thumbnails/')
    video = models.FileField(upload_to='videos/')
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
# Likes
# -----------------------------
class Like(models.Model):
    author = models.ForeignKey(User, on_delete=models.CASCADE)
    video = models.ForeignKey(Video, on_delete=models.CASCADE, related_name='likes')
    created_at = models.DateTimeField(auto_now_add=True, null=True)

    def __str__(self):
        return f"{self.author.username} liked {self.video.title}"