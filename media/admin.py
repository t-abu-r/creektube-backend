from django.contrib import admin
from .models import (Video, Comment, MediaProfile, CategoryVideo, Snip, ModActionLog,
                     SnipDislike, SnipSave, SnipFeedback, Creek)

class VideoAdmin(admin.ModelAdmin):
    list_display = ['title', 'author', 'is_approved', 'timestamp', 'view_count']
    list_filter = ['is_approved', 'category']
    search_fields = ['title', 'description']

class SnipAdmin(admin.ModelAdmin):
    list_display = ['title', 'author', 'is_approved', 'timestamp', 'view_count']
    list_filter = ['is_approved']
    search_fields = ['title']

class ModActionLogAdmin(admin.ModelAdmin):
    list_display = ['moderator', 'action', 'target', 'reason', 'created_at']
    list_filter = ['action', 'created_at']
    search_fields = ['target__username', 'moderator__username', 'reason']
    readonly_fields = ['target', 'moderator', 'action', 'reason', 'created_at']

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class SnipDislikeAdmin(admin.ModelAdmin):
    list_display = ['author', 'snip', 'created_at']
    search_fields = ['author__username', 'snip__title']


class SnipSaveAdmin(admin.ModelAdmin):
    list_display = ['author', 'snip', 'created_at']
    search_fields = ['author__username', 'snip__title']


class SnipFeedbackAdmin(admin.ModelAdmin):
    list_display = ['author', 'snip', 'kind', 'created_at']
    list_filter = ['kind', 'created_at']
    search_fields = ['author__username', 'snip__title']


admin.site.register(Video, VideoAdmin)
admin.site.register(Snip, SnipAdmin)
admin.site.register(Comment)
admin.site.register(MediaProfile)
admin.site.register(CategoryVideo)
admin.site.register(ModActionLog, ModActionLogAdmin)
admin.site.register(Creek)
admin.site.register(SnipDislike, SnipDislikeAdmin)
admin.site.register(SnipSave, SnipSaveAdmin)
admin.site.register(SnipFeedback, SnipFeedbackAdmin)
