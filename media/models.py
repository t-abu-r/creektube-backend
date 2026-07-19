from django.db import models
from django.contrib.auth.models import User
from cloudinary.models import CloudinaryField


class CategoryVideo(models.Model):
    name = models.CharField(max_length=100, unique=True)
    slug = models.SlugField(unique=True)

    def __str__(self):
        return self.name


class MediaProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    categories = models.JSONField(default=dict)
    moderator = models.BooleanField(default=False)
    official = models.BooleanField(default=False)

    def __str__(self):
        return self.user.username


class Video(models.Model):
    author = models.ForeignKey(User, on_delete=models.CASCADE)
    category = models.ForeignKey(
        CategoryVideo,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="videos",
    )
    title = models.CharField(max_length=100)
    description = models.TextField()
    thumbnail = models.ImageField(upload_to='thumbnails/', null=True, blank=True)
    video = CloudinaryField('video', resource_type='video', null=True, blank=True)
    timestamp = models.DateTimeField(auto_now_add=True)
    is_approved = models.BooleanField(default=False)
    view_count = models.PositiveIntegerField(default=0)

    def __str__(self):
        return self.title


class Comment(models.Model):
    author = models.ForeignKey(User, on_delete=models.CASCADE)
    video = models.ForeignKey(Video, related_name="comments", on_delete=models.CASCADE, null=True)
    text = models.TextField(max_length=500)
    timestamp = models.DateTimeField(auto_now_add=True)


class Like(models.Model):
    author = models.ForeignKey(User, on_delete=models.CASCADE)
    video = models.ForeignKey(Video, on_delete=models.CASCADE, related_name='likes')
    created_at = models.DateTimeField(auto_now_add=True, null=True)

    def __str__(self):
        return f"{self.author.username} Piked {self.video.title}"


class DisPike(models.Model):
    author = models.ForeignKey(User, on_delete=models.CASCADE)
    video = models.ForeignKey(Video, on_delete=models.CASCADE, related_name='dispikes')
    created_at = models.DateTimeField(auto_now_add=True, null=True)

    def __str__(self):
        return f"{self.author.username} DisPiked {self.video.title}"


class Creek(models.Model):
    author = models.ForeignKey(User, on_delete=models.CASCADE)
    account = models.ForeignKey(MediaProfile, on_delete=models.CASCADE, related_name='account')
    created_at = models.DateTimeField(auto_now_add=True, null=True)

    def __str__(self):
        return f"{self.author.username} Creeked {self.account.username}"


class WatchEvent(models.Model):
    """Records individual watch sessions for co-watch computation and retention tracking."""
    user = models.ForeignKey(User, on_delete=models.CASCADE, null=True, blank=True)
    video = models.ForeignKey(Video, on_delete=models.CASCADE, related_name='watch_events')
    timestamp = models.DateTimeField(auto_now_add=True)
    duration_watched = models.PositiveIntegerField(default=0, help_text="Seconds watched")
    session_id = models.CharField(max_length=64, blank=True, default="",
                                  help_text="Rough session grouping: events from same user within 30min share a session_id")

    class Meta:
        indexes = [
            models.Index(fields=['video', 'timestamp']),
            models.Index(fields=['user', 'video']),
            models.Index(fields=['session_id']),
        ]

    def __str__(self):
        user_str = self.user.username if self.user else "anon"
        return f"{user_str} watched {self.video.title} ({self.duration_watched}s)"


class UploadRateLimit(models.Model):
    """Tracks upload frequency per user for spam prevention."""
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=['user', 'uploaded_at']),
        ]
