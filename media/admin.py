from django.contrib import admin
from .models import Video, Comment, MediaProfile, CategoryVideo, Snip

class VideoAdmin(admin.ModelAdmin):
    list_display = ['title', 'author', 'is_approved', 'timestamp', 'view_count']
    list_filter = ['is_approved', 'category']
    search_fields = ['title', 'description']

class SnipAdmin(admin.ModelAdmin):
    list_display = ['title', 'author', 'is_approved', 'timestamp', 'view_count']
    list_filter = ['is_approved']
    search_fields = ['title']

admin.site.register(Video, VideoAdmin)
admin.site.register(Snip, SnipAdmin)
admin.site.register(Comment)
admin.site.register(MediaProfile)
admin.site.register(CategoryVideo)
