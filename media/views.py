from django.core.serializers import serialize
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated, IsAdminUser
from accounts.models import Profile
from rest_framework.response import Response
from rest_framework.views import APIView
from django.utils import timezone
from .permissions import IsModerator
from .Serializers import VideoSerializer, MediaProfileSerializer
from accounts.serializers import ProfileSerializer
from .models import Video, Comment, CategoryVideo, MediaProfile as Profile, MediaProfile
from django.db.models import Case, When, Q, IntegerField, Count
from django.views.decorators.csrf import csrf_exempt
import os
from django.conf import settings
from django.utils.decorators import method_decorator


# ---------------------------
# Set Interests API (logged-in users)
# ---------------------------
class SetInterests(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        categories = request.data.get("categories")  # expect a list
        if not categories or not isinstance(categories, list):
            return Response({"detail": "Send a list of categories"}, status=status.HTTP_400_BAD_REQUEST)

        # Validate categories
        for c in categories:
            if c not in CategoryVideo.values:
                return Response({"detail": f"Invalid category: {c}"}, status=status.HTTP_400_BAD_REQUEST)

        profile, _ = Profile.objects.get_or_create(user=request.user)

        # Initialize scores if first time
        profile.categories = {c: profile.categories.get(c, 10) for c in categories}
        profile.save()

        return Response({"detail": "Interests set successfully", "categories": profile.categories})


# ---------------------------
# Get Videos API (feed)
# ---------------------------
class LoginGetVideo(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        approved_videos = Video.objects.filter(is_approved=True)

        user_interest = request.user.mediaprofile.categories

        # Sort categories by priority score (highest first)
        desired_order = sorted(user_interest.items(), key=lambda x: x[1], reverse=True)
        desired_order = [cat for cat, score in desired_order]

        # Annotate each video with a "priority" based on its category slug
        when_statements = [
            When(category__slug=cat_slug, then=pos)
            for pos, cat_slug in enumerate(desired_order)
        ]

        videos = approved_videos.annotate(
            category_order=Case(
                *when_statements,
                default=len(desired_order),  # categories not in user's interest come after
                output_field=IntegerField(),
            )
        ).order_by('category_order', '-timestamp')

        serializer = VideoSerializer(videos, many=True, context={'request': request})
        return Response(serializer.data, status=200)
# GuestGetVideo
class GuestGetVideo(APIView):
    permission_classes = [AllowAny]
    def get(self, request):
        # guest feed
        approved_videos = Video.objects.filter(is_approved=True)
        videos = approved_videos.annotate(num_likes=Count('likes')) \
                        .order_by('-num_likes', '-timestamp')

        serializer = VideoSerializer(videos, many=True, context={'request': request})
        return Response(serializer.data, status=200)

class GetOwnVideo(APIView):
    permission_classes = [IsAuthenticated]
    def get(self, request):
        videos = Video.objects.filter(author=request.user)
        serializer = VideoSerializer(videos, many=True, context={'request': request})
        return Response(serializer.data, status=200)


# ---------------------------
# Watch Video API (boost logged-in user categories)
# ---------------------------

class LoginWatchVideo(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        video_id = request.data.get("video_id")
        if not video_id:
            return Response({"detail": "Video ID not provided"}, status=status.HTTP_400_BAD_REQUEST)

        approved_videos = Video.objects.filter(is_approved=True)
        video = get_object_or_404(approved_videos.prefetch_related("comments__author"), id=video_id)

        # Boost category score
        profile, _ = MediaProfile.objects.get_or_create(user=request.user)
        if video.category:
            cat_slug = video.category.slug
            categories = profile.categories
            categories[cat_slug] = categories.get(cat_slug, 0) + 1
            profile.categories = categories
            profile.save()

        related_videos = approved_videos.filter(category=video.category).exclude(id=video_id).order_by('-timestamp')[:5]

        return Response({
            "video": VideoSerializer(video, context={'request': request}).data,
            "related_videos": VideoSerializer(related_videos, many=True, context={'request': request}).data
        }, status=status.HTTP_200_OK)

class GuestWatchVideo(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        video_id = request.data.get("video_id")
        if not video_id:
            return Response({"detail": "Video ID not provided"}, status=status.HTTP_400_BAD_REQUEST)

        approved_videos = Video.objects.filter(is_approved=True)
        video = get_object_or_404(approved_videos, id=video_id)
        video_category = video.category

        related_videos = approved_videos.filter(
            category=video_category
        ).exclude(id=video_id).order_by('-timestamp')[:5]

        return Response({
            "video": VideoSerializer(video, context={'request': request}).data,
            "related_videos": VideoSerializer(related_videos, many=True, context={'request': request}).data,
        }, status=status.HTTP_200_OK)


class SearchVideo(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        title = request.query_params.get("q")

        if not title:
            return Response(
                {"detail": "Title not provided"},
                status=status.HTTP_400_BAD_REQUEST
            )

        videos = Video.objects.filter(
            Q(title__icontains=title) |
            Q(description__icontains=title)
        ).order_by("-id")[:10]

        serializer = VideoSerializer(videos, many=True)

        return Response(serializer.data, status=status.HTTP_200_OK)

class IfModerator(APIView):
    permission_classes = [IsModerator]

    def get(self, request):
        return Response({"detail": "You are a moderator"}, status=status.HTTP_200_OK)


class ModPanel(APIView):
    permission_classes = [IsModerator]

    def get(self, request):
        unapproved_videos = Video.objects.filter(is_approved=False)
        serializer = VideoSerializer(unapproved_videos, many=True, context={'request': request})
        return Response(serializer.data, status=status.HTTP_200_OK)

    def post(self, request):
        video_id = request.data.get("id")
        if not video_id:
            return Response({"detail": "Video ID not provided"}, status=status.HTTP_400_BAD_REQUEST)
        video = get_object_or_404(Video, id=video_id)
        video.is_approved = not video.is_approved
        video.save()
        serializer = VideoSerializer(video)
        return Response(serializer.data, status=status.HTTP_200_OK)

    def delete(self, request):
        video_id = request.data.get("id")
        if not video_id:
            return Response({"detail": "Video ID not provided"}, status=status.HTTP_400_BAD_REQUEST)
        video = get_object_or_404(Video, id=video_id)
        video.delete()
        return Response({"detail": "Video deleted"}, status=status.HTTP_204_NO_CONTENT)

# ---------------------------
# Comment Video API
# ---------------------------
@method_decorator(csrf_exempt, name='dispatch')
class CommentVideo(APIView):
    def get(self, request):
        video_id = request.query_params.get('video_id')
        if not video_id:
            return Response({'message': 'No video ID provided'}, status=status.HTTP_400_BAD_REQUEST)
        
        video = get_object_or_404(Video, id=video_id)
        comments = Comment.objects.filter(video=video).order_by('timestamp')
        
        return Response([{
            'id': c.id,
            'text': c.text,
            'author': c.author.username,
            'timestamp': c.timestamp
        } for c in comments], status=status.HTTP_200_OK)

@method_decorator(csrf_exempt, name='dispatch')
class UploadCommentVideo(APIView):
    permission_classes = [IsAuthenticated]
    def post(self, request):
            author = request.user
            comment_text = request.data.get('comment')
            video_id = request.data.get('video_id')

            if not comment_text:
                return Response({'message': 'No comment provided'}, status=status.HTTP_400_BAD_REQUEST)

            video = get_object_or_404(Video, id=video_id)

            comment = Comment.objects.create(author=author, video=video, text=comment_text)

            return Response({
                'message': 'Comment added successfully',
                'comment': {
                    'id': comment.id,
                    'text': comment.text,
                    'author': author.username,
                    'video_id': video.id,
                    'timestamp': comment.timestamp
                }
            }, status=status.HTTP_201_CREATED)


# ---------------------------
# Upload Video API
# ---------------------------
from rest_framework.parsers import MultiPartParser, FormParser

class UploadVideo(APIView):
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]  # ← add this

    def post(self, request):
        author = request.user
        video_file = request.data.get("video")
        category = request.data.get('category')
        title = request.data.get("title")
        description = request.data.get("description")
        thumbnail = request.data.get("thumbnail")

        if not video_file:
            return Response({"message": "No video provided"}, status=400)

        category_obj, _ = CategoryVideo.objects.get_or_create(
            name=category,
            slug=category
        )

        video_instance = Video.objects.create(
            video=video_file,
            author=author,
            title=title,
            category=category_obj,
            description=description,
            thumbnail=thumbnail,
            timestamp=timezone.now(),
            is_approved=False
        )

        serializer = VideoSerializer(video_instance, context={'request': request})
        return Response(serializer.data, status=201)

class Account(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        id = request.data.get("id")

        if not id:
            return Response({"error": "ID is required"}, status=400)

        try:
            profile_media = MediaProfile.objects.get(id=id)
        except MediaProfile.DoesNotExist:
            return Response({"error": "Profile not found"}, status=404)

        videos = Video.objects.filter(author=profile_media.user, is_approved=True)

        try:
            user_profile = Profile.objects.get(user=profile_media.user)
            profile_data = ProfileSerializer(user_profile).data
            avatar = profile_data.get("avatar_url")
        except Profile.DoesNotExist:
            avatar = None

        return Response({
            "avatar": avatar,
            "account": MediaProfileSerializer(profile_media).data,
            "videos": VideoSerializer(videos, many=True).data
        }, status=200)