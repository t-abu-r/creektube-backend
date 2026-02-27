from django.db import models
from django.contrib.auth.models import User

class PlanChoices(models.TextChoices):
    FREE = "free", "Free"
    STANDARD = "standard", "Standard"
    PREMIUM = "premium", "Premium"

class Profile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    avatar = models.ImageField(upload_to="avatars/", blank=True, null=True)
    plan = models.CharField(
        max_length=10,
        choices=PlanChoices.choices,
        default=PlanChoices.FREE
    )
    is_verified = models.BooleanField(default=False)

    def __str__(self):
        return f"{self.user.username} - {self.get_plan_display()}"
