from django.db import models

class FileFieldSaveMixin:
    def save_model(self, request, obj, form, change):
        for field in obj._meta.fields:
            if isinstance(field, models.FileField) and field.name not in form.changed_data:
                file = getattr(obj, field.name)
                if file and hasattr(file, '_committed'):
                    file._committed = True
        super().save_model(request, obj, form, change)
