from django.contrib import admin
from .models import Video, Comment, MediaProfile, CategoryVideo
from burst.admin_mixins import CloudinarySafeForm

class VideoAdmin(admin.ModelAdmin):
    form = CloudinarySafeForm

admin.site.register(Video, VideoAdmin)
admin.site.register(Comment)
admin.site.register(MediaProfile)
admin.site.register(CategoryVideo)
