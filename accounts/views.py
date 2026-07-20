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
from .models import Profile, PlanChoices
from urllib.parse import quote
from django.conf import settings
from django.utils.encoding import force_bytes, force_str
from django.utils.http import urlsafe_base64_encode, urlsafe_base64_decode
from urllib.parse import unquote
from media.models import MediaProfile
from media.Serializers import MediaProfileSerializer
import os
from datetime import timedelta
# from cloudinary.utils import cloudinary_url  # Commented out - using local storage


def _set_auth_cookies(response, access_token, refresh_token):
    """Set httpOnly cookies for access and refresh tokens."""
    production = not settings.DEBUG
    cookie_secure = production
    cookie_samesite = "None" if production else "Lax"
    access_max_age = int(os.environ.get("JWT_ACCESS_TOKEN_LIFETIME_MINUTES", 15)) * 60
    refresh_max_age = int(os.environ.get("JWT_REFRESH_TOKEN_LIFETIME_DAYS", 30)) * 86400
    response.set_cookie(
        key="access_token",
        value=access_token,
        httponly=True,
        secure=cookie_secure,
        samesite=cookie_samesite,
        max_age=access_max_age,
        path="/",
    )
    response.set_cookie(
        key="refresh_token",
        value=refresh_token,
        httponly=True,
        secure=cookie_secure,
        samesite=cookie_samesite,
        max_age=refresh_max_age,
        path="/",
    )
    response.set_cookie(
        key="authenticated",
        value="true",
        httponly=False,
        secure=cookie_secure,
        samesite=cookie_samesite,
        max_age=access_max_age,
        path="/",
    )


def _clear_auth_cookies(response):
    """Clear auth cookies."""
    response.delete_cookie("access_token", path="/")
    response.delete_cookie("refresh_token", path="/")
    response.delete_cookie("authenticated", path="/")

class VerifyEmailView(APIView):
    def get(self, request, uidb64, token):
        try:
            # 1. Decode the user ID
            uid = force_str(urlsafe_base64_decode(uidb64))
            user = User.objects.get(pk=uid)

            # 2. Extract the new email from the URL query params
            new_email = request.query_params.get('new_email')

            # 3. Check if the token is valid for this user
            if user is not None and default_token_generator.check_token(user, token):
                if not new_email:
                    return Response({"error": "Missing new email address"}, status=400)

                # 4. Success! Update the email
                user.email = new_email
                user.save()
                return Response({"success": "Email updated successfully!"}, status=200)

            return Response({"error": "Invalid or expired token"}, status=400)

        except (TypeError, ValueError, OverflowError, User.DoesNotExist):
            return Response({"error": "Invalid verification link"}, status=400)

class JWTRegisterView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        email = request.data.get("email")
        username = request.data.get("username")
        password = request.data.get("password")

        if not email or not username or not password:
            return Response({"error": "Email, username, and password are required"}, status=400)

        # Check if user exists
        if User.objects.filter(username=username).exists() or User.objects.filter(email=email).exists():
            # Don't leak info — just respond with generic message
            return Response({"message": "If an account exists, a verification email has been sent"}, status=200)


        user = User.objects.create_user(username=username, email=email, password=password, is_active=True)

        # Create profiles safely
        Profile.objects.get_or_create(user=user)
        MediaProfile.objects.get_or_create(user=user)

        # Generate UID and token
        uidb64 = urlsafe_base64_encode(force_bytes(user.pk)).rstrip("=")
        token = account_activation_token.make_token(user)
        verify_url = f"{settings.FRONTEND_URL}/verify-email?uid={uidb64}&token={quote(token, safe='')}"

        subject = 'register'
        message = 'message'

        # Send email safely
        if settings.DEFAULT_FROM_EMAIL:
            try:
                send_mail(subject, message, settings.DEFAULT_FROM_EMAIL, [email], fail_silently=False)
            except Exception:
                pass

        return Response({"message": "If an account exists, a verification email has been sent"}, status=200)
User = get_user_model()

class JWTLoginView(APIView):
    def post(self, request):
        email = request.data.get("email")
        username = request.data.get("username")
        password = request.data.get("password")

        # Make sure all fields are provided
        if not email or not username or not password:
            return Response({"error": "All fields are required"}, status=400)

        try:
            user_obj = User.objects.get(email=email, username=username)
        except User.DoesNotExist:
            return Response({"error": "Email or username or password is incorrect"}, status=400)

        # Authenticate using username + password
        user = authenticate(username=username, password=password)

        if user is None:
            return Response({"error": "Invalid credentials"}, status=400)

        if not user.is_active:
            user.is_active = True
            user.save()

        profile, created = MediaProfile.objects.get_or_create(user=user)

        if user is not None:
            refresh = RefreshToken.for_user(user)
            return Response({
                "refresh": str(refresh),
                "access": str(refresh.access_token)
            })

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

        response_data = {
            "success": "Profile updated successfully",
            "email_verification_sent": False,
            "avatar_url": None
        }

        # ---------- USERNAME UPDATE ----------
        new_username = request.data.get("username")
        if new_username and new_username != user.username:
            if User.objects.filter(username=new_username).exists():
                return Response({"error": "Username already taken"}, status=400)

            user.username = new_username
            user.save()

        # ---------- EMAIL UPDATE ----------
        new_email = request.data.get("email")
        password = request.data.get("password")

        if new_email and new_email != user.email:
            if not password or not user.check_password(password):
                return Response(
                    {"error": "Password required to change email address"},
                    status=400
                )

            token = default_token_generator.make_token(user)
            uid = urlsafe_base64_encode(force_bytes(user.pk))

            api_url = request.build_absolute_uri('/').rstrip('/')
            link = f"{api_url}/api/verify-email/{uid}/{token}/?new_email={new_email}"

            try:
                send_mail(
                    subject="Verify Your New Email - CreekTube",
                    message=f"Click the link below to verify your new email:\n\n{link}",
                    from_email=None,
                    recipient_list=[new_email],
                    fail_silently=False
                )

                response_data["email_verification_sent"] = True

            except Exception as e:
                return Response(
                    {"error": f"Failed to send verification email: {str(e)}"},
                    status=500
                )

        # ---------- PROFILE UPDATE ----------
        bio = request.data.get("bio")
        avatar = request.FILES.get("avatar")

        if bio is not None:
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

        try:
            user = User.objects.get(email=email)
        except User.DoesNotExist:
            return Response({"detail": "Email Sent!"}, status=status.HTTP_200_OK)

        token = default_token_generator.make_token(user)
        uid = urlsafe_base64_encode(force_bytes(user.pk))

        # Construct reset link (Next.js frontend)
        reset_link = f"{settings.FRONTEND_URL}/reset-password/{uid}/{token}/"

        # Send the email
        send_mail(
            subject="Password Reset Request",
            message=f"Click the link to reset your password: {reset_link}",
            from_email=None,  # Uses DEFAULT_FROM_EMAIL
            recipient_list=[email],
            fail_silently=False,
        )

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

        if not user.is_active:
            user.is_active = True
            user.save()

        MediaProfile.objects.get_or_create(user=user)

        refresh = RefreshToken.for_user(user)
        access_token = str(refresh.access_token)
        refresh_token = str(refresh)

        response = Response({
            "access": access_token,
            "detail": "Login successful",
        })
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