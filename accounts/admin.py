from django.contrib import admin
from .models import Profile
from burst.admin_mixins import CloudinarySafeForm

class ProfileAdmin(admin.ModelAdmin):
    form = CloudinarySafeForm

admin.site.register(Profile, ProfileAdmin)
