from django.contrib.auth import authenticate
from django.contrib.auth.tokens import default_token_generator
from django.core.mail import send_mail
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from rest_framework import status
from rest_framework.status import HTTP_400_BAD_REQUEST, HTTP_205_RESET_CONTENT, HTTP_200_OK
from rest_framework.views import APIView, Response
from rest_framework.parsers import MultiPartParser, FormParser
from django.contrib.auth.models import User
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework_simplejwt.tokens import RefreshToken, AccessToken
from rest_framework.decorators import api_view, permission_classes
from .tokens import account_activation_token
from django.contrib.auth import get_user_model
from .models import Profile, PlanChoices, SecurityCode
from urllib.parse import quote
from django.utils.encoding import force_bytes, force_str
from django.utils.http import urlsafe_base64_encode, urlsafe_base64_decode
from urllib.parse import unquote
from media.models import MediaProfile
from media.Serializers import MediaProfileSerializer
import os
import random
import string
from django.core.mail import EmailMultiAlternatives
from django.conf import settings
from django.utils import timezone
from datetime import timedelta


# ---------------------------
# Helpers
# ---------------------------
def _generate_otp(length=6):
    return ''.join(random.choices(string.digits, k=length))


def _create_security_code(user, purpose, expiry_minutes=15):
    SecurityCode.objects.filter(user=user, purpose=purpose, used=False).update(used=True)
    code = _generate_otp()
    expires = timezone.now() + timedelta(minutes=expiry_minutes)
    SecurityCode.objects.create(user=user, code=code, purpose=purpose, expires_at=expires)
    return code


def _verify_security_code(user, code, purpose):
    try:
        sc = SecurityCode.objects.filter(
            user=user, purpose=purpose, used=False
        ).order_by('-created_at').first()
        if not sc:
            return False, "No verification code found. Request a new one."
        if sc.expires_at < timezone.now():
            return False, "Code expired. Request a new one."
        if sc.code != code:
            return False, "Incorrect code."
        sc.used = True
        sc.save(update_fields=["used"])
        return True, "ok"
    except Exception:
        return False, "Verification failed."


def _mask_email(email):
    if not email or "@" not in email:
        return None
    local, domain = email.split("@", 1)
    if len(local) <= 1:
        masked_local = local[0] + "***"
    else:
        visible = max(1, len(local) // 10)
        masked_local = local[:visible] + "***"
    return f"{masked_local}@{domain}"


def _set_auth_cookies(response, access_token, refresh_token):
    production = not settings.DEBUG
    cookie_secure = production
    cookie_samesite = "None" if production else "Lax"
    access_max_age = int(os.environ.get("JWT_ACCESS_TOKEN_LIFETIME_MINUTES", 15)) * 60
    refresh_max_age = int(os.environ.get("JWT_REFRESH_TOKEN_LIFETIME_DAYS", 30)) * 86400
    response.set_cookie(
        key="access_token", value=access_token, httponly=True,
        secure=cookie_secure, samesite=cookie_samesite, max_age=access_max_age, path="/",
    )
    response.set_cookie(
        key="refresh_token", value=refresh_token, httponly=True,
        secure=cookie_secure, samesite=cookie_samesite, max_age=refresh_max_age, path="/",
    )
    response.set_cookie(
        key="authenticated", value="true", httponly=False,
        secure=cookie_secure, samesite=cookie_samesite, max_age=access_max_age, path="/",
    )


def _clear_auth_cookies(response):
    production = not settings.DEBUG
    response.delete_cookie("access_token", path="/", samesite="None" if production else "Lax", secure=production)
    response.delete_cookie("refresh_token", path="/", samesite="None" if production else "Lax", secure=production)
    response.delete_cookie("authenticated", path="/", samesite="None" if production else "Lax", secure=production)


def _send_code_email(user, code, subject, purpose_label, to_email=None):
    recipient = to_email or user.email
    text_content = (
        f"Your CreekTube {purpose_label} code is: {code}\n\n"
        f"This code expires in 15 minutes. If you didn't request this, ignore this email."
    )
    html_content = f"""
    <div style="font-family: Arial, sans-serif; max-width: 480px; margin: auto; padding: 24px; border: 1px solid #eee; border-radius: 8px;">
        <h2 style="color: #1a1a1a;">CreekTube</h2>
        <p>Your <strong>{purpose_label}</strong> verification code is:</p>
        <p style="text-align: center; margin: 32px 0; font-size: 32px; font-weight: bold; letter-spacing: 8px; color: #ff3d3d;">{code}</p>
        <p style="font-size: 13px; color: #666;">This code expires in 15 minutes. If you didn't request this, you can safely ignore this email.</p>
        <hr style="border: none; border-top: 1px solid #eee; margin: 24px 0;">
        <p style="font-size: 12px; color: #999;">— The CreekTube Team</p>
    </div>
    """
    try:
        email_message = EmailMultiAlternatives(
            subject=subject, body=text_content,
            from_email=settings.DEFAULT_FROM_EMAIL, to=[recipient],
        )
        email_message.attach_alternative(html_content, "text/html")
        email_message.send(fail_silently=False)
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"Failed to send email to {recipient}: {e}")


# ---------------------------
# Auth Views (unchanged from before, abbreviated)
# ---------------------------
class VerifyEmailView(APIView):
    def get(self, request, uidb64, token):
        try:
            uid = force_str(urlsafe_base64_decode(uidb64))
            user = User.objects.get(pk=uid)
            new_email = request.query_params.get('new_email')
            if user is not None and default_token_generator.check_token(user, token):
                if not new_email:
                    return Response({"error": "Missing new email address"}, status=400)
                user.email = new_email
                user.save()
                return Response({"success": "Email updated successfully!"}, status=200)
            return Response({"error": "Invalid or expired token"}, status=400)
        except (TypeError, ValueError, OverflowError, User.DoesNotExist):
            return Response({"error": "Invalid verification link"}, status=400)


class JWTRegisterView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        import re
        email = request.data.get("email")
        username = request.data.get("username")
        password = request.data.get("password")
        if not email or not username or not password:
            return Response({"error": "Email, username, and password are required"}, status=400)

        # Username validation: 3-30 chars, alphanumeric + underscores only
        if len(username) < 3:
            return Response({"error": "Username must be at least 3 characters"}, status=400)
        if len(username) > 30:
            return Response({"error": "Username must be 30 characters or fewer"}, status=400)
        if not re.match(r'^[a-zA-Z0-9_]+$', username):
            return Response({"error": "Username can only contain letters, numbers, and underscores"}, status=400)

        # Password validation: min 8 chars, must contain letter + number
        if len(password) < 8:
            return Response({"error": "Password must be at least 8 characters"}, status=400)
        if not re.search(r'[A-Za-z]', password):
            return Response({"error": "Password must contain at least one letter"}, status=400)
        if not re.search(r'[0-9]', password):
            return Response({"error": "Password must contain at least one number"}, status=400)

        if User.objects.filter(username=username).exists() or User.objects.filter(email=email).exists():
            return Response({"message": "If an account exists, a verification email has been sent"}, status=200)
        user = User.objects.create_user(username=username, email=email, password=password, is_active=True)
        Profile.objects.get_or_create(user=user)
        MediaProfile.objects.get_or_create(user=user)
        uidb64 = urlsafe_base64_encode(force_bytes(user.pk)).rstrip("=")
        token = account_activation_token.make_token(user)
        verify_url = f"{settings.FRONTEND_URL}/verify-email?uid={uidb64}&token={quote(token, safe='')}"
        if settings.DEFAULT_FROM_EMAIL:
            try:
                send_mail('register', 'message', settings.DEFAULT_FROM_EMAIL, [email], fail_silently=False)
            except Exception:
                pass
        return Response({"message": "If an account exists, a verification email has been sent"}, status=200)

User = get_user_model()


class JWTLoginView(APIView):
    def post(self, request):
        email = request.data.get("email")
        username = request.data.get("username")
        password = request.data.get("password")
        if not email or not username or not password:
            return Response({"error": "All fields are required"}, status=400)
        try:
            user_obj = User.objects.get(email=email, username=username)
        except User.DoesNotExist:
            return Response({"error": "Email or username or password is incorrect"}, status=400)
        user = authenticate(username=username, password=password)
        if user is None:
            return Response({"error": "Invalid credentials"}, status=400)
        MediaProfile.objects.get_or_create(user=user)
        if user is not None:
            refresh = RefreshToken.for_user(user)
            return Response({"refresh": str(refresh), "access": str(refresh.access_token)})
        return Response({"error": "Password is incorrect"}, status=400)


class JWTLogoutView(APIView):
    permission_classes = [IsAuthenticated]
    def post(self, request, *args, **kwargs):
        refresh_token = request.data.get("refresh")
        if not refresh_token:
            return Response({"error": "Refresh token is required"}, status=HTTP_400_BAD_REQUEST)
        try:
            token = RefreshToken(refresh_token)
            token.blacklist()
            return Response({"message": "Logged out successfully"}, status=HTTP_205_RESET_CONTENT)
        except Exception as e:
            return Response({"error": "Invalid or expired refresh token"}, status=HTTP_400_BAD_REQUEST)


class UpdateProfileView(APIView):
    permission_classes = [IsAuthenticated]
    parser_classes = (MultiPartParser, FormParser)

    def put(self, request):
        user = request.user
        profile, created = Profile.objects.get_or_create(user=user)
        response_data = {"success": "Profile updated successfully", "email_verification_sent": False, "avatar_url": None}

        new_username = request.data.get("username")
        if new_username and new_username != user.username:
            import re
            if len(new_username) < 3:
                return Response({"error": "Username must be at least 3 characters"}, status=400)
            if len(new_username) > 30:
                return Response({"error": "Username must be 30 characters or fewer"}, status=400)
            if not re.match(r'^[a-zA-Z0-9_]+$', new_username):
                return Response({"error": "Username can only contain letters, numbers, and underscores"}, status=400)
            if User.objects.filter(username=new_username).exists():
                return Response({"error": "Username already taken"}, status=400)
            user.username = new_username
            user.save()

        new_email = request.data.get("email")
        password = request.data.get("password")
        if new_email and new_email != user.email:
            if not password or not user.check_password(password):
                return Response({"error": "Password required to change email address"}, status=400)
            token = default_token_generator.make_token(user)
            uid = urlsafe_base64_encode(force_bytes(user.pk))
            api_url = request.build_absolute_uri('/').rstrip('/')
            link = f"{api_url}/api/verify-email/{uid}/{token}/?new_email={new_email}"
            try:
                send_mail(subject="Verify Your New Email - CreekTube", message=f"Click the link below to verify your new email:\n\n{link}", from_email=None, recipient_list=[new_email], fail_silently=False)
                response_data["email_verification_sent"] = True
            except Exception as e:
                return Response({"error": f"Failed to send verification email: {str(e)}"}, status=500)

        bio = request.data.get("bio")
        avatar = request.FILES.get("avatar")
        if bio is not None:
            if len(bio) > 500:
                return Response({"error": "Bio must be 500 characters or fewer"}, status=400)
            profile.bio = bio
        if avatar:
            profile.avatar = avatar
        profile.save()
        if profile.avatar:
            response_data["avatar_url"] = profile.avatar.url
        return Response(response_data, status=200)


class JWTResetPasswordView(APIView):
    def post(self, request, *args, **kwargs):
        email = request.data.get("email")
        if not email:
            return Response({"error": "Email is required"}, status=HTTP_400_BAD_REQUEST)
        user = User.objects.filter(email=email).first()
        if user is None:
            return Response({"detail": "Email Sent!"}, status=status.HTTP_200_OK)
        token = default_token_generator.make_token(user)
        uid = urlsafe_base64_encode(force_bytes(user.pk))
        reset_link = f"{settings.FRONTEND_URL}/reset-password/{uid}/{token}/"
        send_mail(subject="Password Reset Request", message=f"Click the link to reset your password: {reset_link}", from_email=None, recipient_list=[email], fail_silently=False)
        return Response({"detail": "Email Sent!"}, status=status.HTTP_200_OK)


class CheckUserPLan(APIView):
    permission_classes = [IsAuthenticated]
    def get(self, request):
        user_plan = Profile.objects.get(user=request.user)
        return Response({"message": user_plan.get_plan_display()})


class CheckUserInfo(APIView):
    permission_classes = [IsAuthenticated]
    def get(self, request):
        user_profile, _ = Profile.objects.get_or_create(user=request.user)
        media_profile, _ = MediaProfile.objects.get_or_create(user=request.user)
        media = MediaProfileSerializer(media_profile).data
        avatar = None
        if user_profile.avatar:
            avatar = user_profile.avatar.url
        return Response({
            "id": media_profile.pk,
            "username": request.user.username,
            "email": request.user.email,
            "plan": user_profile.get_plan_display(),
            "avatar": avatar
        })


# ---------------------------
# Cookie-based JWT Auth Views
# ---------------------------
class CookieTokenLoginView(APIView):
    permission_classes = [AllowAny]
    def post(self, request):
        username = request.data.get("username")
        password = request.data.get("password")
        if not username or not password:
            return Response({"error": "All fields are required"}, status=400)
        try:
            user_obj = User.objects.get(username=username)
        except User.DoesNotExist:
            return Response({"error": "Username or password is incorrect"}, status=400)
        user = authenticate(username=username, password=password)
        if user is None:
            return Response({"error": "Invalid credentials"}, status=400)
        MediaProfile.objects.get_or_create(user=user)
        refresh = RefreshToken.for_user(user)
        access_token = str(refresh.access_token)
        refresh_token = str(refresh)
        response = Response({"access": access_token, "detail": "Login successful"})
        _set_auth_cookies(response, access_token, refresh_token)
        return response


class CookieTokenRefreshView(APIView):
    permission_classes = [AllowAny]
    def post(self, request):
        refresh_token = request.COOKIES.get("refresh_token") or request.data.get("refresh")
        if not refresh_token:
            return Response({"error": "Refresh token not found"}, status=401)
        try:
            refresh = RefreshToken(refresh_token)
            user = User.objects.filter(pk=refresh.payload.get("user_id"), is_active=True).first()
            if user is None:
                return Response({"error": "Account is deactivated"}, status=401)
            new_access = str(refresh.access_token)
            new_refresh = str(refresh)
            refresh.blacklist()
            response = Response({"access": new_access, "detail": "Token refreshed"})
            _set_auth_cookies(response, new_access, new_refresh)
            return response
        except Exception:
            return Response({"error": "Invalid or expired refresh token"}, status=401)


class CookieTokenLogoutView(APIView):
    permission_classes = [AllowAny]
    def post(self, request):
        refresh_token = request.COOKIES.get("refresh_token") or request.data.get("refresh")
        if refresh_token:
            try:
                token = RefreshToken(refresh_token)
                token.blacklist()
            except Exception:
                pass
        response = Response({"detail": "Logged out successfully"}, status=205)
        _clear_auth_cookies(response)
        return response


class ResetPasswordView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        token = request.query_params.get("token")
        uidb64 = request.query_params.get("uid")
        if not token or not uidb64:
            return Response({"error": "Missing token or user ID"}, status=400)
        try:
            uid = force_str(urlsafe_base64_decode(uidb64))
            user = User.objects.get(pk=uid)
        except (TypeError, ValueError, OverflowError, User.DoesNotExist):
            return Response({"error": "Invalid or expired token"}, status=400)
        if default_token_generator.check_token(user, token):
            return Response({"token": token, "uid": uidb64}, status=200)
        else:
            return Response({"error": "Invalid or expired token"}, status=400)

    def post(self, request):
        email = request.data.get("email")
        if not email:
            return Response({"error": "Email is required"}, status=400)
        user = User.objects.filter(email=email).first()
        if user is None:
            return Response({"detail": "Password reset email has been sent."}, status=200)

        profile, _ = Profile.objects.get_or_create(user=user)
        masked = _mask_email(profile.recovery_email) if profile.recovery_email else None

        token = default_token_generator.make_token(user)
        uid = urlsafe_base64_encode(force_bytes(user.pk))
        reset_link = f"{settings.FRONTEND_URL}/reset-password/{uid}/{token}/"

        try:
            subject = "Reset Your CreekTube Password"
            text_content = (
                f"Hi,\n\nWe received a request to reset your CreekTube password. "
                f"Click the link below to choose a new one:\n\n{reset_link}\n\n"
                f"If you didn't request this, you can safely ignore this email.\n\n"
                f"This link will expire in 24 hours.\n\n— The CreekTube Team"
            )
            html_content = f"""
            <div style="font-family: Arial, sans-serif; max-width: 480px; margin: auto; padding: 24px; border: 1px solid #eee; border-radius: 8px;">
                <h2 style="color: #1a1a1a;">CreekTube</h2>
                <p>Hi,</p>
                <p>We received a request to reset your CreekTube password. Click the button below to choose a new one:</p>
                <p style="text-align: center; margin: 32px 0;">
                    <a href="{reset_link}" style="background-color: #ff3d3d; color: #fff; padding: 12px 24px; text-decoration: none; border-radius: 6px; font-weight: bold;">Reset Password</a>
                </p>
                <p style="font-size: 13px; color: #666;">If the button doesn't work, copy and paste this link into your browser:<br><a href="{reset_link}">{reset_link}</a></p>
                <p style="font-size: 13px; color: #666;">This link will expire in 24 hours.</p>
                <hr style="border: none; border-top: 1px solid #eee; margin: 24px 0;">
                <p style="font-size: 12px; color: #999;">— The CreekTube Team</p>
            </div>
            """
            email_message = EmailMultiAlternatives(subject=subject, body=text_content, from_email=settings.DEFAULT_FROM_EMAIL, to=[email])
            email_message.attach_alternative(html_content, "text/html")
            email_message.send(fail_silently=False)
        except Exception as e:
            return Response({"error": f"Failed to send email: {str(e)}"}, status=500)

        return Response({
            "detail": "Password reset email has been sent.",
            "has_recovery_email": profile.recovery_email is not None and profile.recovery_email_verified,
            "masked_recovery_email": masked,
        }, status=200)


class ConfirmResetPasswordView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        token = request.data.get("token")
        uidb64 = request.data.get("uid")
        new_password = request.data.get("new_password")
        if not token or not uidb64 or not new_password:
            return Response({"error": "Token, uid, and new password are required"}, status=400)
        if len(new_password) < 8:
            return Response({"error": "Password must be at least 8 characters"}, status=400)
        try:
            uid = force_str(urlsafe_base64_decode(uidb64))
            user = User.objects.get(pk=uid)
        except (TypeError, ValueError, OverflowError, User.DoesNotExist):
            return Response({"error": "Invalid or expired reset link"}, status=400)
        if not default_token_generator.check_token(user, token):
            return Response({"error": "Invalid or expired reset link"}, status=400)
        user.set_password(new_password)
        user.save()
        return Response({"detail": "Password has been reset successfully"}, status=200)


# ---------------------------
# Recovery Email
# ---------------------------
class SetRecoveryEmail(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        recovery_email = request.data.get("recovery_email", "").strip().lower()
        if not recovery_email:
            return Response({"error": "Recovery email is required"}, status=400)
        if recovery_email == request.user.email:
            return Response({"error": "Recovery email must be different from your account email"}, status=400)

        profile, _ = Profile.objects.get_or_create(user=request.user)
        profile.recovery_email = recovery_email
        profile.recovery_email_verified = False
        profile.save(update_fields=["recovery_email", "recovery_email_verified"])

        code = _create_security_code(request.user, "recovery_email")
        _send_code_email(request.user, code, "Verify Your Recovery Email - CreekTube", "recovery email verification", to_email=recovery_email)

        return Response({"detail": "Verification code sent to your recovery email"}, status=200)


class VerifyRecoveryEmail(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        code = request.data.get("code", "").strip()
        if not code:
            return Response({"error": "Verification code is required"}, status=400)

        ok, msg = _verify_security_code(request.user, code, "recovery_email")
        if not ok:
            return Response({"error": msg}, status=400)

        profile, _ = Profile.objects.get_or_create(user=request.user)
        profile.recovery_email_verified = True
        profile.save(update_fields=["recovery_email_verified"])

        return Response({"detail": "Recovery email verified successfully"}, status=200)


class ResendRecoveryEmailCode(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        profile, _ = Profile.objects.get_or_create(user=request.user)
        if not profile.recovery_email:
            return Response({"error": "No recovery email set"}, status=400)

        code = _create_security_code(request.user, "recovery_email")
        _send_code_email(request.user, code, "Verify Your Recovery Email - CreekTube", "recovery email verification", to_email=profile.recovery_email)

        return Response({"detail": "New verification code sent"}, status=200)


class GetRecoveryEmailInfo(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        profile, _ = Profile.objects.get_or_create(user=request.user)
        masked = _mask_email(profile.recovery_email) if profile.recovery_email else None
        return Response({
            "has_recovery_email": profile.recovery_email is not None,
            "recovery_email_verified": profile.recovery_email_verified,
            "masked_recovery_email": masked,
        }, status=200)


# ---------------------------
# Password Change (with email verification)
# ---------------------------
class RequestPasswordChange(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        current_password = request.data.get("current_password")
        new_password = request.data.get("new_password")

        if not current_password or not new_password:
            return Response({"error": "Current password and new password are required"}, status=400)
        if not request.user.check_password(current_password):
            return Response({"error": "Current password is incorrect"}, status=400)
        if len(new_password) < 8:
            return Response({"error": "New password must be at least 8 characters"}, status=400)
        if current_password == new_password:
            return Response({"error": "New password must be different from current password"}, status=400)

        profile, _ = Profile.objects.get_or_create(user=request.user)
        if not profile.recovery_email or not profile.recovery_email_verified:
            return Response({
                "error": "You must set and verify a recovery email before changing your password",
                "needs_recovery_email": True,
            }, status=400)

        code = _create_security_code(request.user, "password_change")
        _send_code_email(request.user, code, "Verify Password Change - CreekTube", "password change")

        return Response({
            "detail": "Verification code sent to your recovery email",
            "masked_recovery_email": _mask_email(profile.recovery_email),
        }, status=200)


class ConfirmPasswordChange(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        code = request.data.get("code", "").strip()
        new_password = request.data.get("new_password")

        if not code or not new_password:
            return Response({"error": "Verification code and new password are required"}, status=400)
        if len(new_password) < 8:
            return Response({"error": "New password must be at least 8 characters"}, status=400)

        ok, msg = _verify_security_code(request.user, code, "password_change")
        if not ok:
            return Response({"error": msg}, status=400)

        request.user.set_password(new_password)
        request.user.save()

        return Response({"detail": "Password changed successfully"}, status=200)


# ---------------------------
# Forgot Password (with recovery email verification)
# ---------------------------
class ForgotPasswordRequest(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        email = request.data.get("email", "").strip()
        if not email:
            return Response({"error": "Email is required"}, status=400)

        user = User.objects.filter(email=email).first()
        if user is None:
            return Response({"detail": "If an account exists with this email, a recovery code has been sent."}, status=200)

        profile, _ = Profile.objects.get_or_create(user=user)
        if not profile.recovery_email or not profile.recovery_email_verified:
            token = default_token_generator.make_token(user)
            uid = urlsafe_base64_encode(force_bytes(user.pk))
            reset_link = f"{settings.FRONTEND_URL}/reset-password/{uid}/{token}/"
            _send_forgot_password_email(user, reset_link)
            return Response({
                "detail": "If an account exists with this email, a reset link has been sent to your account email.",
                "method": "email_link",
                "has_recovery_email": False,
            }, status=200)

        code = _create_security_code(user, "password_reset")
        _send_code_email(user, code, "Reset Your CreekTube Password", "password reset")

        return Response({
            "detail": "If an account exists with this email, a recovery code has been sent.",
            "method": "verification_code",
            "has_recovery_email": True,
            "masked_recovery_email": _mask_email(profile.recovery_email),
        }, status=200)


class ForgotPasswordConfirm(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        email = request.data.get("email", "").strip()
        code = request.data.get("code", "").strip()
        new_password = request.data.get("new_password", "")

        if not email or not code or not new_password:
            return Response({"error": "Email, code, and new password are required"}, status=400)
        if len(new_password) < 8:
            return Response({"error": "Password must be at least 8 characters"}, status=400)

        user = User.objects.filter(email=email).first()
        if user is None:
            return Response({"error": "Invalid request"}, status=400)

        ok, msg = _verify_security_code(user, code, "password_reset")
        if not ok:
            return Response({"error": msg}, status=400)

        user.set_password(new_password)
        user.save()

        return Response({"detail": "Password has been reset successfully"}, status=200)


class ForgotPasswordResendCode(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        email = request.data.get("email", "").strip()
        if not email:
            return Response({"error": "Email is required"}, status=400)

        user = User.objects.filter(email=email).first()
        if user is None:
            return Response({"detail": "If an account exists, a new code has been sent."}, status=200)

        profile, _ = Profile.objects.get_or_create(user=user)
        if not profile.recovery_email or not profile.recovery_email_verified:
            return Response({"error": "No verified recovery email found"}, status=400)

        code = _create_security_code(user, "password_reset")
        _send_code_email(user, code, "Reset Your CreekTube Password", "password reset")

        return Response({"detail": "New verification code sent"}, status=200)


def _send_forgot_password_email(user, reset_link):
    subject = "Reset Your CreekTube Password"
    text_content = (
        f"Hi,\n\nWe received a request to reset your CreekTube password. "
        f"Click the link below to choose a new one:\n\n{reset_link}\n\n"
        f"If you didn't request this, you can safely ignore this email.\n\n"
        f"This link will expire in 24 hours.\n\n— The CreekTube Team"
    )
    html_content = f"""
    <div style="font-family: Arial, sans-serif; max-width: 480px; margin: auto; padding: 24px; border: 1px solid #eee; border-radius: 8px;">
        <h2 style="color: #1a1a1a;">CreekTube</h2>
        <p>Hi,</p>
        <p>We received a request to reset your CreekTube password. Click the button below to choose a new one:</p>
        <p style="text-align: center; margin: 32px 0;">
            <a href="{reset_link}" style="background-color: #ff3d3d; color: #fff; padding: 12px 24px; text-decoration: none; border-radius: 6px; font-weight: bold;">Reset Password</a>
        </p>
        <p style="font-size: 13px; color: #666;">This link will expire in 24 hours.</p>
        <hr style="border: none; border-top: 1px solid #eee; margin: 24px 0;">
        <p style="font-size: 12px; color: #999;">— The CreekTube Team</p>
    </div>
    """
    email_message = EmailMultiAlternatives(subject=subject, body=text_content, from_email=settings.DEFAULT_FROM_EMAIL, to=[user.email])
    email_message.attach_alternative(html_content, "text/html")
    try:
        email_message.send(fail_silently=False)
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"Failed to send password reset email to {user.email}: {e}")
