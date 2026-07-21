from django.contrib import admin
from .models import Video, Comment, MediaProfile, CategoryVideo

class VideoAdmin(admin.ModelAdmin):
    list_display = ['title', 'author', 'is_approved', 'timestamp', 'view_count']
    list_filter = ['is_approved', 'category']
    search_fields = ['title', 'description']

admin.site.register(Video, VideoAdmin)
admin.site.register(Comment)
admin.site.register(MediaProfile)
admin.site.register(CategoryVideo)
