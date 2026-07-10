from django.db import models

def mark_committed(obj, field_names):
    for name in field_names:
        file = getattr(obj, name, None)
        if file and hasattr(file, '_committed'):
            file._committed = True

class FileFieldSaveMixin:
    def save_model(self, request, obj, form, change):
        mark_committed(obj, [
            f.name for f in obj._meta.fields
            if isinstance(f, models.FileField) and f.attname not in request.FILES
        ])
        super().save_model(request, obj, form, change)
