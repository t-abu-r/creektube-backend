from django.urls import path
from .views import *

urlpatterns = [
    path("login/", JWTLoginView.as_view(), name="Login"),
    path("register/", JWTRegisterView.as_view(), name="Register"),
    path("logout/", JWTLogoutView.as_view(), name="Logout"),
    path("cookie-login/", CookieTokenLoginView.as_view(), name="CookieLogin"),
    path("cookie-logout/", CookieTokenLogoutView.as_view(), name="CookieLogout"),
    path("cookie-refresh/", CookieTokenRefreshView.as_view(), name="CookieRefresh"),
    path("checkplan/", CheckUserPLan.as_view(), name="check-plan"),
    path("getuser/", CheckUserInfo.as_view(), name="check-user-info"),
    path("update-profile/", UpdateProfileView.as_view(), name="update-profile"),
    path("verify-email/<str:uidb64>/<str:token>/", VerifyEmailView.as_view(), name="verify-email"),

    # reset password
    path("reset-password/", ResetPasswordView.as_view(), name="reset-password"),
    path("change-password/", ChangePasswordView.as_view(), name="change-password"),
    path("confirm-reset-password/", ConfirmResetPasswordView.as_view(), name="confirm-reset-password"),
]