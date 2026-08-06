from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from django.contrib.auth.models import User
from .models import Profile
from burst.admin_mixins import CloudinarySafeForm


class ProfileAdmin(admin.ModelAdmin):
    form = CloudinarySafeForm


class ModeratorUserAdmin(UserAdmin):
    actions = ["deactivate_accounts", "reactivate_accounts"]

    @admin.action(description="Deactivate selected accounts (hide all content)")
    def deactivate_accounts(self, request, queryset):
        updated = queryset.exclude(is_superuser=True).exclude(pk=request.user.pk).update(is_active=False)
        self.message_user(request, f"Deactivated {updated} account(s).")

    @admin.action(description="Reactivate selected accounts")
    def reactivate_accounts(self, request, queryset):
        updated = queryset.update(is_active=True)
        self.message_user(request, f"Reactivated {updated} account(s).")


admin.site.unregister(User)
admin.site.register(User, ModeratorUserAdmin)
admin.site.register(Profile, ProfileAdmin)
