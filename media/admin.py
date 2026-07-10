from django.contrib import admin
from .models import Video, Comment, MediaProfile, CategoryVideo

@admin.register(Video)
class VideoAdmin(admin.ModelAdmin):
    exclude = ('thumbnail', 'video')

admin.site.register(Comment)
admin.site.register(MediaProfile)
admin.site.register(CategoryVideo)
