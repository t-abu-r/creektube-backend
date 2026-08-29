from django.apps import AppConfig


class DirectchatConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'directchat'

    def ready(self):
        import directchat.signals
