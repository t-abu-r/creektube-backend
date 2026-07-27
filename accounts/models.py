from django.db import models
from django.contrib.auth.models import User
# from cloudinary.models import CloudinaryField  # Commented out - using local storage
import os
from dotenv import load_dotenv
load_dotenv()

debug = os.getenv('DEBUG', 'False')

class PlanChoices(models.TextChoices):
    FREE = "free", "Free"
    STANDARD = "standard", "Standard"
    PREMIUM = "premium", "Premium"

class Profile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)

    # Use ImageField for all environments (local storage)
    avatar = models.ImageField(upload_to='avatars/', blank=True, null=True)

    bio = models.TextField(blank=True, null=True, max_length=500)
    plan = models.CharField(
        max_length=10,
        choices=PlanChoices.choices,
        default=PlanChoices.FREE
    )
    is_verified = models.BooleanField(default=False)
    notification_reply = models.BooleanField(default=True)
    recovery_email = models.EmailField(blank=True, null=True)
    recovery_email_verified = models.BooleanField(default=False)

    def __str__(self):
        return f"{self.user.username} - {self.get_plan_display()}"


class SecurityCode(models.Model):
    PURPOSE_CHOICES = [
        ("password_change", "Password Change Verification"),
        ("password_reset", "Password Reset"),
        ("recovery_email", "Recovery Email Verification"),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="security_codes")
    code = models.CharField(max_length=6)
    purpose = models.CharField(max_length=20, choices=PURPOSE_CHOICES)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    used = models.BooleanField(default=False)

    class Meta:
        indexes = [
            models.Index(fields=["user", "purpose", "used"]),
        ]

    def __str__(self):
        return f"{self.user.username} - {self.purpose} - {'used' if self.used else 'pending'}"
