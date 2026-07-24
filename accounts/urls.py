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

    # Password reset (legacy link-based)
    path("reset-password/", ResetPasswordView.as_view(), name="reset-password"),
    path("confirm-reset-password/", ConfirmResetPasswordView.as_view(), name="confirm-reset-password"),

    # Password change (logged-in, with email verification)
    path("request-password-change/", RequestPasswordChange.as_view(), name="request-password-change"),
    path("confirm-password-change/", ConfirmPasswordChange.as_view(), name="confirm-password-change"),

    # Recovery email
    path("recovery-email/", GetRecoveryEmailInfo.as_view(), name="recovery-email-info"),
    path("set-recovery-email/", SetRecoveryEmail.as_view(), name="set-recovery-email"),
    path("verify-recovery-email/", VerifyRecoveryEmail.as_view(), name="verify-recovery-email"),
    path("resend-recovery-code/", ResendRecoveryEmailCode.as_view(), name="resend-recovery-code"),

    # Forgot password (with recovery email code flow)
    path("forgot-password/", ForgotPasswordRequest.as_view(), name="forgot-password-request"),
    path("forgot-password-confirm/", ForgotPasswordConfirm.as_view(), name="forgot-password-confirm"),
    path("forgot-password-resend/", ForgotPasswordResendCode.as_view(), name="forgot-password-resend"),
]
