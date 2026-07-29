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
    banner = models.TextField(blank=True, default="")

    def __str__(self):
        return self.user.username


class Video(models.Model):
    VISIBILITY_CHOICES = [
        ("public", "Public"),
        ("unlisted", "Unlisted"),
        ("private", "Private"),
    ]

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
    thumbnail = models.TextField(blank=True, default="")
    video = models.TextField()
    video_public_id = models.CharField(max_length=255, blank=True, default="")
    thumbnail_public_id = models.CharField(max_length=255, blank=True, default="")
    visibility = models.CharField(max_length=10, choices=VISIBILITY_CHOICES, default="public")
    timestamp = models.DateTimeField(auto_now_add=True)
    is_approved = models.BooleanField(default=False)
    view_count = models.PositiveIntegerField(default=0)

    def __str__(self):
        return self.title


class Snip(models.Model):
    VISIBILITY_CHOICES = [
        ("public", "Public"),
        ("unlisted", "Unlisted"),
        ("private", "Private"),
    ]

    author = models.ForeignKey(User, on_delete=models.CASCADE)
    title = models.CharField(max_length=100)
    description = models.TextField(blank=True, default="")
    video = models.TextField()
    thumbnail = models.TextField(blank=True, default="")
    video_public_id = models.CharField(max_length=255, blank=True, default="")
    thumbnail_public_id = models.CharField(max_length=255, blank=True, default="")
    visibility = models.CharField(max_length=10, choices=VISIBILITY_CHOICES, default="public")
    timestamp = models.DateTimeField(auto_now_add=True)
    is_approved = models.BooleanField(default=False)
    view_count = models.PositiveIntegerField(default=0)
    like_count = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['-timestamp']

    def __str__(self):
        return self.title


class SnipLike(models.Model):
    author = models.ForeignKey(User, on_delete=models.CASCADE)
    snip = models.ForeignKey(Snip, on_delete=models.CASCADE, related_name='likes')
    created_at = models.DateTimeField(auto_now_add=True, null=True)

    class Meta:
        unique_together = ['author', 'snip']

    def __str__(self):
        return f"{self.author.username} liked snip {self.snip.id}"


class Comment(models.Model):
    author = models.ForeignKey(User, on_delete=models.CASCADE)
    video = models.ForeignKey(Video, related_name="comments", on_delete=models.CASCADE, null=True, blank=True)
    snip = models.ForeignKey('Snip', related_name="comments", on_delete=models.CASCADE, null=True, blank=True)
    text = models.TextField(max_length=500)
    timestamp = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True, null=True)
    is_pinned = models.BooleanField(default=False)
    edited = models.BooleanField(default=False)
    parent = models.ForeignKey('self', null=True, blank=True, on_delete=models.CASCADE, related_name='replies')


class CommentLike(models.Model):
    author = models.ForeignKey(User, on_delete=models.CASCADE)
    comment = models.ForeignKey(Comment, on_delete=models.CASCADE, related_name='likes')
    created_at = models.DateTimeField(auto_now_add=True, null=True)

    class Meta:
        unique_together = ['author', 'comment']

    def __str__(self):
        return f"{self.author.username} liked comment {self.comment.id}"


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
    """Records individual watch sessions for retention tracking and spam prevention.
    Each user can have at most 6 views per video/snip, with time gaps enforced.
    Excess views are deleted automatically."""
    user = models.ForeignKey(User, on_delete=models.CASCADE, null=True, blank=True)
    video = models.ForeignKey(Video, on_delete=models.CASCADE, related_name='watch_events', null=True, blank=True)
    snip = models.ForeignKey('Snip', on_delete=models.CASCADE, related_name='watch_events', null=True, blank=True)
    timestamp = models.DateTimeField(auto_now_add=True)
    duration_watched = models.PositiveIntegerField(default=0, help_text="Seconds watched")
    session_id = models.CharField(max_length=64, blank=True, default="",
                                  help_text="Rough session grouping: events from same user within 30min share a session_id")

    class Meta:
        indexes = [
            models.Index(fields=['video', 'timestamp']),
            models.Index(fields=['snip', 'timestamp']),
            models.Index(fields=['user', 'video']),
            models.Index(fields=['user', 'snip']),
            models.Index(fields=['session_id']),
        ]

    def __str__(self):
        user_str = self.user.username if self.user else "anon"
        target = self.video.title if self.video else (self.snip.title if self.snip else "?")
        return f"{user_str} watched {target} ({self.duration_watched}s)"


class UploadRateLimit(models.Model):
    """Tracks upload frequency per user for spam prevention."""
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=['user', 'uploaded_at']),
        ]


class Notification(models.Model):
    NOTIFICATION_TYPES = [
        ("comment", "New comment on your video"),
        ("reply", "Reply to your comment"),
        ("like", "Someone liked your video"),
        ("subscribe", "New subscriber"),
        ("mention", "You were mentioned"),
    ]

    recipient = models.ForeignKey(User, on_delete=models.CASCADE, related_name="notifications")
    actor = models.ForeignKey(User, on_delete=models.CASCADE, related_name="actor_notifications")
    verb = models.CharField(max_length=20, choices=NOTIFICATION_TYPES)
    target_type = models.CharField(max_length=20, blank=True, default="")
    target_id = models.PositiveIntegerField(null=True, blank=True)
    extra_data = models.JSONField(default=dict, blank=True)
    is_read = models.BooleanField(default=False)
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-timestamp"]
        indexes = [
            models.Index(fields=["recipient", "-timestamp"]),
            models.Index(fields=["recipient", "is_read"]),
        ]

    def __str__(self):
        return f"{self.actor.username} {self.verb} -> {self.recipient.username}"
