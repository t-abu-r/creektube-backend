from rest_framework.permissions import BasePermission
from rest_framework.exceptions import NotFound


def _profile(user):
    return getattr(user, "mediaprofile", None)


class IsModerator(BasePermission):
    """Any user holding a moderator-capable title or the legacy flag.

    A user qualifies when they carry at least one title with a ``mod.*``
    permission (or the legacy ``moderator`` boolean). Superusers always pass.
    """

    def has_permission(self, request, view):
        if not request.user.is_authenticated:
            raise NotFound()
        profile = _profile(request.user)
        if not profile:
            raise NotFound()
        if not profile.is_moderator():
            raise NotFound()
        return True


class HasPermission(BasePermission):
    """Require a specific permission string on the user's titles.

    e.g. ``HasPermission("mod.deactivate")`` lets a moderator with only the
    approve-permission still see the mod panel but NOT deactivate accounts.
    Superusers always pass.
    """

    message = "You do not have permission to do that"

    def __init__(self, permission):
        self.permission = permission
        super().__init__()

    def has_permission(self, request, view):
        if not request.user.is_authenticated:
            return False
        profile = _profile(request.user)
        if not profile:
            return False
        return profile.has_permission(self.permission)


class IsSuperUser(BasePermission):
    """Superuser-only (the admin panel)."""

    def has_permission(self, request, view):
        return bool(request.user.is_authenticated and request.user.is_superuser)
