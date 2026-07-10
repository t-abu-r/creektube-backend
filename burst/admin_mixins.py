from django import forms
from django.db import models

def mark_committed(obj, field_names):
    for name in field_names:
        file = getattr(obj, name, None)
        if file and hasattr(file, '_committed'):
            file._committed = True

FILE_HELP_TEXT = "Enter Cloudinary public ID (e.g. 'thumbnails/image.jpg')"

class CloudinarySafeForm(forms.ModelForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in list(self.fields.items()):
            if isinstance(field, forms.ImageField):
                initial = self.initial.get(name, '')
                self.fields[name] = forms.CharField(
                    required=False,
                    initial=initial,
                    help_text=FILE_HELP_TEXT,
                )
            elif isinstance(field, forms.FileField):
                initial = self.initial.get(name, '')
                self.fields[name] = forms.CharField(
                    required=False,
                    initial=initial,
                    help_text=FILE_HELP_TEXT,
                )

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
