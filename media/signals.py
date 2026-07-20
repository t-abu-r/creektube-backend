from django.db.models.signals import post_save
from django.dispatch import receiver
from .models import Comment, Like, DisPike, Creek, Notification
import re


def create_notification(recipient, actor, verb, target_type="", target_id=None, extra_data=None):
    if recipient == actor:
        return
    Notification.objects.create(
        recipient=recipient,
        actor=actor,
        verb=verb,
        target_type=target_type,
        target_id=target_id,
        extra_data=extra_data or {},
    )


@receiver(post_save, sender=Comment)
def comment_notification(sender, instance, created, **kwargs):
    if not created:
        return

    video = instance.video
    if not video:
        return

    # Notify video author about new comment
    create_notification(
        recipient=video.author,
        actor=instance.author,
        verb="comment",
        target_type="video",
        target_id=video.id,
        extra_data={"video_title": video.title, "comment_text": instance.text[:200]},
    )

    # If reply to another comment, notify parent comment author (if enabled)
    if instance.parent and instance.parent.author != instance.author:
        parent = instance.parent
        from accounts.models import Profile
        try:
            parent_profile = Profile.objects.get(user=parent.author)
            if parent_profile.notification_reply:
                create_notification(
                    recipient=parent.author,
                    actor=instance.author,
                    verb="reply",
                    target_type="comment",
                    target_id=parent.id,
                    extra_data={"video_title": video.title, "comment_text": instance.text[:200]},
                )
        except Profile.DoesNotExist:
            pass

    # @mention detection
    mentions = re.findall(r'@(\w+)', instance.text)
    if mentions:
        from django.contrib.auth.models import User
        mentioned_users = User.objects.filter(username__in=mentions)
        for user in mentioned_users:
            create_notification(
                recipient=user,
                actor=instance.author,
                verb="mention",
                target_type="video",
                target_id=video.id,
                extra_data={"video_title": video.title, "mentioner": instance.author.username},
            )


@receiver(post_save, sender=Like)
def like_notification(sender, instance, created, **kwargs):
    if not created:
        return
    video = instance.video
    create_notification(
        recipient=video.author,
        actor=instance.author,
        verb="like",
        target_type="video",
        target_id=video.id,
        extra_data={"video_title": video.title},
    )


@receiver(post_save, sender=Creek)
def subscribe_notification(sender, instance, created, **kwargs):
    if not created:
        return
    recipient = instance.account.user
    create_notification(
        recipient=recipient,
        actor=instance.author,
        verb="subscribe",
        target_type="account",
        target_id=instance.account.id,
        extra_data={"username": instance.author.username},
    )
