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

        # Create user
        user = User.objects.create_user(username=username, email=email, password=password, is_active=True)

        # Create Profile safely
        Profile.objects.get_or_create(user=user)

        # Generate UID and token
        uidb64 = urlsafe_base64_encode(force_bytes(user.pk)).rstrip("=")
        token = account_activation_token.make_token(user)
        verify_url = f"http://127.0.0.1:3000/verify-email?uid={uidb64}&token={quote(token, safe='')}"

        subject = 'register'
        message = 'message'

        # Send email safely
        try:
            send_mail(subject, message, settings.DEFAULT_FROM_EMAIL, [email], fail_silently=False)
        except Exception:
            pass  # don't crash if email fails

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


class UpdateProfileView(APIView):
    permission_classes = [IsAuthenticated]

    def patch(self, request):
        user = request.user
        data = request.data

        # 1. Handle Sensitive Email Change
        new_email = data.get("email")
        password = data.get("password")

        if new_email and new_email != user.email:
            if not password or not user.check_password(password):
                return Response({"error": "Password required to change email"}, status=400)

            # Email Verification Logic
            token = default_token_generator.make_token(user)
            uid = urlsafe_base64_encode(force_bytes(user.pk))
            # Note: Storing the pending email in session or a temporary field is best,
            # but for now, we'll send the link.
            link = f"https://creektube-production.up.railway.app/verify-email/{uid}/{token}/?new_email={new_email}"

            send_mail(
                "Verify Your New Email",
                f"Click here to verify: {link}",
                None,
                [new_email],
            )
            # We don't change user.email yet! Only after verification.

        # 2. Handle Profile Info (Bio, Avatar)
        profile, created = Profile.objects.get_or_create(user=user)

        if 'bio' in data:
            profile.bio = data.get('bio')

        if 'avatar' in request.FILES:
            profile.avatar = request.FILES['avatar']

        profile.save()

        # 3. Handle Username change (Optional)
        new_username = data.get("username")
        if new_username and new_username != user.username:
            if User.objects.filter(username=new_username).exists():
                return Response({"error": "Username already taken"}, status=400)
            user.username = new_username
            user.save()

        return Response({
            "success": "Profile updated successfully",
            "email_verification_sent": bool(new_email and new_email != user.email)
        }, status=200)

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
