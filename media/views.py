from django.core.serializers import serialize
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated, IsAdminUser
from accounts.models import Profile
from rest_framework.response import Response
from rest_framework.status import HTTP_400_BAD_REQUEST
from rest_framework.views import APIView
from django.utils import timezone
from .permissions import IsModerator
from .Serializers import *
from accounts.serializers import ProfileSerializer
from .models import Video, Comment, CategoryVideo, MediaProfile, Like, DisPike, Creek
from django.db.models import Case, When, Q, IntegerField, Count
from . import ranking
from django.views.decorators.csrf import csrf_exempt
import os
from django.conf import settings
from django.utils.decorators import method_decorator
from rest_framework.parsers import MultiPartParser, FormParser

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
    """
    Personalized feed.

    Instead of hard-bucketing by category (which let a user's top category
    completely bury every other category, and never let a stale favorite
    fall out of favor), every video gets a single composite score blending:
      - interest affinity (how well it matches the user's category weights)
      - engagement (net likes vs dislikes, log-dampened)
      - recency (exponential decay, so old videos fade out gracefully)
      - a bonus for creators the user follows ("creeked")
    See media/ranking.py for the scoring itself.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        profile, _ = MediaProfile.objects.get_or_create(user=request.user)
        user_interest = profile.categories
        creeked_author_ids = set(
            Creek.objects.filter(author=request.user)
            .values_list('account__user_id', flat=True)
        )

        # Pull like/dislike counts in one query rather than N+1-ing them
        # inside the scoring loop.
        approved_videos = (
            Video.objects.filter(is_approved=True)
            .select_related('category')
            .annotate(num_likes=Count('likes', distinct=True), num_dislikes=Count('dispikes', distinct=True))
        )

        # Cap how many candidates we score in Python. For a small/medium
        # catalog this is effectively "all of them"; it keeps the endpoint
        # bounded as the video count grows without needing a real search
        # index yet.
        CANDIDATE_LIMIT = 500
        candidates = list(approved_videos.order_by('-timestamp')[:CANDIDATE_LIMIT])

        ranked = ranking.rank_videos(candidates, user_interest, creeked_author_ids)

        # Simple pagination: ?page=1&page_size=20
        try:
            page = max(int(request.query_params.get('page', 1)), 1)
        except (TypeError, ValueError):
            page = 1
        try:
            page_size = min(max(int(request.query_params.get('page_size', 20)), 1), 100)
        except (TypeError, ValueError):
            page_size = 20

        start = (page - 1) * page_size
        page_videos = ranked[start:start + page_size]

        serializer = VideoSerializer(page_videos, many=True, context={'request': request})
        return Response({
            "results": serializer.data,
            "page": page,
            "page_size": page_size,
            "count": len(ranked),
        }, status=200)

# GuestGetVideo
class GuestGetVideo(APIView):
    """
    Logged-out feed: no personalization possible, but it should still be a
    "hot" ranking (engagement + recency decay) rather than an all-time like
    count, which let one old viral video permanently camp at #1 forever and
    starved new uploads of any visibility.
    """
    permission_classes = [AllowAny]

    def get(self, request):
        approved_videos = (
            Video.objects.filter(is_approved=True)
            .select_related('category')
            .annotate(num_likes=Count('likes', distinct=True), num_dislikes=Count('dispikes', distinct=True))
        )

        CANDIDATE_LIMIT = 500
        candidates = list(approved_videos.order_by('-timestamp')[:CANDIDATE_LIMIT])
        # No user interests or creeks for a guest; interest affinity falls
        # back to the exploration floor for every video, so ranking is
        # purely engagement + recency.
        ranked = ranking.rank_videos(candidates, user_interests={}, creeked_author_ids=set())

        try:
            page = max(int(request.query_params.get('page', 1)), 1)
        except (TypeError, ValueError):
            page = 1
        try:
            page_size = min(max(int(request.query_params.get('page_size', 20)), 1), 100)
        except (TypeError, ValueError):
            page_size = 20

        start = (page - 1) * page_size
        page_videos = ranked[start:start + page_size]

        serializer = VideoSerializer(page_videos, many=True, context={'request': request})
        return Response({
            "results": serializer.data,
            "page": page,
            "page_size": page_size,
            "count": len(ranked),
        }, status=200)

# Studio things
class GetOwnVideo(APIView):
    permission_classes = [IsAuthenticated]
    def get(self, request):
        videos = Video.objects.filter(author=request.user)
        serializer = VideoSerializer(videos, many=True, context={'request': request})
        return Response(serializer.data, status=200)

class Categories(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        categories = CategoryVideo.objects.annotate(video_count=Count('videos'))
        serializer = CategoryVideoSerializer(categories, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)

class Studio(APIView):
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]

    def patch(self, request):
        video_id = request.data.get("id")
        if not video_id:
            return Response({"detail": "Video ID required"}, status=400)

        video = get_object_or_404(Video, id=video_id, author=request.user)

        title = request.data.get("title")
        description = request.data.get("description")
        thumbnail = request.data.get("thumbnail")
        video_file = request.data.get("video")
        category = request.data.get("category")

        if title:
            video.title = title
        if description:
            video.description = description
        if thumbnail:
            video.thumbnail = thumbnail
        if video_file:
            video.video = video_file
            video.is_approved = False  # re-approve after video change
        if category:
            category_obj, _ = CategoryVideo.objects.get_or_create(name=category, slug=category)
            video.category = category_obj

        from burst.admin_mixins import mark_committed
        unchanged = [f for f in ['thumbnail', 'video'] if not request.data.get(f)]
        mark_committed(video, unchanged)
        video.save()
        return Response(VideoSerializer(video, context={'request': request}).data, status=200)



class StudioVideoDelete(APIView):
    permission_classes = [IsAuthenticated]

    def delete(self, request, video_id):
        video = get_object_or_404(Video, id=video_id, author=request.user)
        video.delete()
        return Response(status=204)

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

        # Boost category score (clamped so a binge on one category can't
        # grow its score forever and permanently drown out everything else)
        profile, _ = MediaProfile.objects.get_or_create(user=request.user)
        if video.category:
            profile.categories = ranking.adjust_category_score(
                profile.categories, video.category.slug, ranking.WATCH_BOOST
            )
            profile.save()

        related_videos = approved_videos.filter(category=video.category).exclude(id=video_id).order_by('-timestamp')[:5]

        video_author_channel = MediaProfile.objects.filter(user=video.author).first()

        try:
            like = Like.objects.get(video=video, author=request.user)
            if_liked = True
        except Like.DoesNotExist:
            like = None
            if_liked = False

        try:
            dispike = DisPike.objects.get(video=video, author=request.user)
            if_dispiked = True
        except DisPike.DoesNotExist:
            dispike = None
            if_dispiked = False

        try:
            creek = Creek.objects.get(account=video_author_channel, author=request.user)
            if_creeked = True
        except Creek.DoesNotExist:
            creek = None
            if_creeked = False

        like_count = Like.objects.filter(video=video).count()
        dispike_count = DisPike.objects.filter(video=video).count()
        creek_count = Creek.objects.filter(account=video_author_channel).count() if video_author_channel else 0
        creek_count = creek_count


        return Response({
            "video": VideoSerializer(video, context={'request': request}).data,
            "related_videos": VideoSerializer(related_videos, many=True, context={'request': request}).data,
            "like": LikeSerializer(like).data if if_liked else False,
            "like_count": like_count,
            "dispike": DisPikeSerializer(dispike).data if if_dispiked else False,
            "dispike_count": dispike_count,
            "creek": CreekSerializer(creek).data if if_creeked else False,
            "creek_count": creek_count,
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

        # Guest users don't have likes, dispikes, or creek relationships
        if_liked = False
        if_dispiked = False
        if_creeked = False
        like = None
        dispike = None
        creek = None

        # Get video author for creek count
        video_author_channel = MediaProfile.objects.filter(user=video.author).first()

        like_count = Like.objects.filter(video=video).count()
        dispike_count = DisPike.objects.filter(video=video).count()
        creek_count = Creek.objects.filter(account=video_author_channel).count() if video_author_channel else 0

        return Response({
            "video": VideoSerializer(video, context={'request': request}).data,
            "related_videos": VideoSerializer(related_videos, many=True, context={'request': request}).data,
            "like": LikeSerializer(like).data if if_liked else False,
            "like_count": like_count,
            "dispike": DisPikeSerializer(dispike).data if if_dispiked else False,
            "dispike_count": dispike_count,
            "creek": CreekSerializer(creek).data if if_creeked else False,
            "creek_count": creek_count,
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
# Interactable Video Features
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

class PikeVideo(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        video_id = request.data.get("id")

        if not video_id:
            return Response({"detail": "Video ID required"}, status=status.HTTP_400_BAD_REQUEST)

        video = get_object_or_404(Video, id=video_id)

        like, created = Like.objects.get_or_create(author=request.user, video=video)

        if not created:
            like.delete()
            return Response({"liked": False}, status=status.HTTP_200_OK)

        return Response({"liked": True}, status=status.HTTP_201_CREATED)

class DisPikeVideo(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        video_id = request.data.get("id")

        if not video_id:
            return Response({"detail": "Video ID required"}, status=status.HTTP_400_BAD_REQUEST)

        video = get_object_or_404(Video, id=video_id)

        dispike, created = DisPike.objects.get_or_create(author=request.user, video=video)

        # Previously a dislike had zero effect on the recommendation
        # algorithm, so a user could dislike every video in a category and
        # still keep getting fed that category. Now it nudges that
        # category's score down (and undoing the dislike nudges it back up),
        # same clamp as the positive watch-boost.
        profile, _ = MediaProfile.objects.get_or_create(user=request.user)
        if video.category:
            delta = -ranking.DISLIKE_PENALTY if created else ranking.DISLIKE_PENALTY
            profile.categories = ranking.adjust_category_score(profile.categories, video.category.slug, delta)
            profile.save()

        if not created:
            dispike.delete()
            return Response({"dispike": False}, status=status.HTTP_200_OK)

        return Response({"dispike": True}, status=status.HTTP_201_CREATED)

class CreekAccount(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        account_id = request.data.get("id")

        if not account_id:
            return Response({"detail": "Account ID required"}, status=status.HTTP_400_BAD_REQUEST)

        account = get_object_or_404(MediaProfile, id=account_id)

        creek, created = Creek.objects.get_or_create(author=request.user, account=account)

        if not created:
            creek.delete()
            return Response({"creek": False}, status=status.HTTP_200_OK)

        return Response({"creek": True}, status=status.HTTP_201_CREATED)
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
        creek_count = Creek.objects.filter(account=profile_media).count()
        try:
            user_profile = Profile.objects.get(user=profile_media.user)
            profile_data = ProfileSerializer(user_profile, context={"request": request}).data
        except Profile.DoesNotExist:
            profile_data = {"avatar_url": None, "bio": None}

        return Response({
            "profile": profile_data,
            "account": MediaProfileSerializer(profile_media).data,
            "videos": VideoSerializer(videos, many=True).data,
            "creek_count": creek_count,
        }, status=200)