from django.contrib import admin
from .models import Video, Comment, MediaProfile, CategoryVideo
from burst.admin_mixins import FileFieldSaveMixin

@admin.register(Video)
class VideoAdmin(FileFieldSaveMixin, admin.ModelAdmin):
    pass

admin.site.register(Comment)
admin.site.register(MediaProfile)
admin.site.register(CategoryVideo)
