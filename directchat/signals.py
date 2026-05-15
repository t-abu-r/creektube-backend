from django.db.models.signals import post_save
from django.dispatch import receiver
from django.contrib.auth.models import User

from .models import SenderModel, ReceiverModel, ChatKeyModel


@receiver(post_save, sender=User)
def create_sender_receiver_models(sender, instance, created, **kwargs):
    """Create sender and receiver models when a user is created"""
    if created:
        SenderModel.objects.create(user=instance)
        ReceiverModel.objects.create(user=instance)
        # Create a chat key for the user (for self-chat)
        ChatKeyModel.objects.create(usernames=[instance.username])


@receiver(post_save, sender=User)
def save_sender_receiver_models(sender, instance, **kwargs):
    """Save sender and receiver models when a user is saved"""
    if hasattr(instance, 'sendermodel'):
        instance.sendermodel.save()
    if hasattr(instance, 'receivermodel'):
        instance.receivermodel.save()
