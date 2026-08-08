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
        from media.models import ModActionLog
        queryset = queryset.exclude(is_superuser=True).exclude(pk=request.user.pk)
        for user in queryset:
            if not user.is_active:
                continue
            user.is_active = False
            user.save(update_fields=["is_active"])
            ModActionLog.objects.create(
                target=user,
                moderator=request.user,
                action="deactivate",
                reason="Deactivated via admin panel",
            )
        updated = queryset.count()
        self.message_user(request, f"Deactivated {updated} account(s).")

    @admin.action(description="Reactivate selected accounts")
    def reactivate_accounts(self, request, queryset):
        from media.models import ModActionLog
        updated = 0
        for user in queryset:
            if user.is_active:
                continue
            user.is_active = True
            user.save(update_fields=["is_active"])
            ModActionLog.objects.create(
                target=user,
                moderator=request.user,
                action="reactivate",
                reason="Reactivated via admin panel",
            )
            updated += 1
        self.message_user(request, f"Reactivated {updated} account(s).")


admin.site.unregister(User)
admin.site.register(User, ModeratorUserAdmin)
admin.site.register(Profile, ProfileAdmin)
