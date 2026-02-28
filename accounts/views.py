from django.contrib.auth import authenticate
from django.contrib.auth.tokens import default_token_generator
from django.core.mail import send_mail
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from rest_framework import status
from rest_framework.status import HTTP_400_BAD_REQUEST, HTTP_205_RESET_CONTENT, HTTP_200_OK
from rest_framework.views import APIView, Response
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

class VerifyEmailView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, *args, **kwargs):
        uidb64 = request.query_params.get("uid")
        token = request.query_params.get("token")

        if not uidb64 or not token:
            return Response({"error": "Missing parameters"}, status=400)

        try:
            padding = 4 - len(uidb64) % 4
            uidb64_padded = uidb64 + ("=" * (padding % 4))
            uid = force_str(urlsafe_base64_decode(uidb64_padded))
            user = User.objects.get(pk=uid)
        except (TypeError, ValueError, OverflowError, User.DoesNotExist):
            return Response({"error": "Invalid link"}, status=400)

        if account_activation_token.check_token(user, token):
            user.is_active = True
            user.save()
            profile, _ = Profile.objects.get_or_create(user=user)
            profile.is_verified = True
            profile.save()
            return Response({"message": "Email verified successfully!"})
        else:
            return Response({"error": "Invalid or expired token"}, status=400)

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

        # Create user
        user = User.objects.create_user(username=username, email=email, password=password, is_active=False)

        # Create Profile safely
        Profile.objects.get_or_create(user=user)

        # Generate UID and token
        uidb64 = urlsafe_base64_encode(force_bytes(user.pk)).rstrip("=")
        token = account_activation_token.make_token(user)
        verify_url = f"http://127.0.0.1:3000/verify-email?uid={uidb64}&token={quote(token, safe='')}"

        # Send email safely
        try:
            send_mail(
                "Verify your CreekTube account",
                f"Hi {username},\n\nVerify your account: {verify_url}\n\nIgnore if you didn't register.",
                settings.DEFAULT_FROM_EMAIL,
                [email],
                fail_silently=False
            )
        except Exception as e:
            print("Email failed:", e)  # log error, do not crash

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
            return Response({"error": "Email or username is incorrect"}, status=400)

        # Authenticate using username + password
        user = authenticate(username=username, password=password)

        if user is None:
            return Response({"error": "Invalid credentials"}, status=400)

        if not user.is_active:
            user.is_active = True
            user.save()

        profile, created = MediaProfile.objects.get_or_create(user=user)
        if created:
            profile.user = user
            profile.categories = {"brainrot": 1}
            profile.moderator = False
            profile.save()

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

class JWTUpdateEmailView(APIView):
    permission_classes = [IsAuthenticated]

    def put(self, request):
        new_email = request.data.get("email")
        username = request.data.get("username")
        password = request.data.get("password")

        if not new_email or not username or not password:
            return Response({"error": "Username, email, and password are required"}, status=400)

        try:
            user = User.objects.get(username=username)

            # Verify password
            if not user.check_password(password):
                return Response({"error": "Incorrect password"}, status=400)

            # Check if new email is different
            if new_email == user.email:
                return Response({"error": "New email is the same as current email"}, status=400)

            # Generate token and send email verification
            token = default_token_generator.make_token(user)
            uid = urlsafe_base64_encode(force_bytes(user.pk))
            reset_email_link = f"https://creektube-production.up.railway.app/reset-email/{uid}/{token}"

            send_mail(
                subject="Email verification",
                message=f"Click the link below to verify your new email:\n{reset_email_link}",
                from_email=None,
                recipient_list=[new_email],
                fail_silently=False
            )

            return Response({"success": "Verification email sent"}, status=200)

        except User.DoesNotExist:
            return Response({"error": "User does not exist"}, status=404)
        except Exception as e:
            return Response({"error": str(e)}, status=400)


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
        reset_link = f"http://localhost:3000/reset-password/{uid}/{token}/"

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
        user_profile = Profile.objects.get(user=request.user)
        username = request.user.username

        return Response({"username": username, "plan": user_profile.get_plan_display()})
