from django import forms
from django.db import models

def mark_committed(obj, field_names):
    for name in field_names:
        file = getattr(obj, name, None)
        if file and hasattr(file, '_committed'):
            file._committed = True

class CloudinarySafeForm(forms.ModelForm):
    def save(self, commit=True):
        instance = super().save(commit=False)
        for field in instance._meta.fields:
            if isinstance(field, models.FileField):
                name = field.name
                val = self.cleaned_data.get(name)
                if val is None or isinstance(val, str) or val is False:
                    file = getattr(instance, name)
                    if file and hasattr(file, '_committed'):
                        file._committed = True
        if commit:
            instance.save()
        return instance
