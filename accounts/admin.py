from django.contrib import admin
from .models import Profile
from burst.admin_mixins import FileFieldSaveMixin

@admin.register(Profile)
class ProfileAdmin(FileFieldSaveMixin, admin.ModelAdmin):
    pass
