from rest_framework.permissions import BasePermission
from rest_framework.exceptions import NotFound


class IsModerator(BasePermission):
    def has_permission(self, request, view):
        if not request.user.is_authenticated:
            raise NotFound()

        # check profile field
        profile = getattr(request.user, "mediaprofile", None)
        if not profile or not profile.moderator:
            raise NotFound()

        return True