from django.contrib import admin

from .models import Video, Comment, MediaProfile, CategoryVideo

# Register your models here.
admin.site.register(Video)
admin.site.register(Comment)
admin.site.register(MediaProfile)
admin.site.register(CategoryVideo)