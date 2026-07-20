from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from accounts.models import Profile
from rest_framework.response import Response
from rest_framework.status import HTTP_400_BAD_REQUEST
from rest_framework.views import APIView
from django.utils import timezone
from .permissions import IsModerator
from .Serializers import *
from accounts.serializers import ProfileSerializer
from .models import (Video, Comment, CategoryVideo, MediaProfile, Like,
                     DisPike, Creek, WatchEvent, UploadRateLimit)
from django.db.models import Count, Q
from . import ranking
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator
from rest_framework.parsers import MultiPartParser, FormParser
from datetime import timedelta


# ---------------------------
# Spam Prevention Helpers
# ---------------------------
UPLOAD_RATE_LIMIT = 3  # max uploads per hour
UPLOAD_RATE_WINDOW = timedelta(hours=1)
VIEW_DEDUP_WINDOW = timedelta(minutes=30)


def check_upload_rate_limit(user):
    """Returns True if user can upload (under rate limit), False if blocked."""
    cutoff = timezone.now() - UPLOAD_RATE_WINDOW
    recent_uploads = UploadRateLimit.objects.filter(
        user=user, uploaded_at__gte=cutoff
    ).count()
    return recent_uploads < UPLOAD_RATE_LIMIT


def record_upload(user):
    """Record an upload event for rate limiting."""
    UploadRateLimit.objects.create(user=user)


def record_view(user, video):
    """
    Record a watch event with spam prevention.
    Returns True if this counts as a new view (dedup), False if duplicate.
    """
    # Check for duplicate within dedup window
    cutoff = timezone.now() - VIEW_DEDUP_WINDOW
    is_duplicate = WatchEvent.objects.filter(
        user=user, video=video, timestamp__gte=cutoff
    ).exists()

    if not is_duplicate:
        Video.objects.filter(pk=video.pk).update(view_count=video.view_count + 1)

    return not is_duplicate


def get_or_create_session_id(user):
    """Generate a session ID based on user's recent activity (30min window)."""
    if not user or not user.is_authenticated:
        return ""
    cutoff = timezone.now() - VIEW_DEDUP_WINDOW
    last_event = (
        WatchEvent.objects.filter(user=user, timestamp__gte=cutoff)
        .order_by('-timestamp')
        .first()
    )
    if last_event and last_event.session_id:
        return last_event.session_id
    import uuid
    return uuid.uuid4().hex[:16]


# ---------------------------
# Set Interests API
# ---------------------------
class SetInterests(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        categories = request.data.get("categories")
        if not categories or not isinstance(categories, list):
            return Response({"detail": "Send a list of category slugs"}, status=status.HTTP_400_BAD_REQUEST)

        valid_slugs = set(
            CategoryVideo.objects.values_list("slug", flat=True)
        )
        for c in categories:
            if c not in valid_slugs:
                return Response({"detail": f"Unknown category: {c}"}, status=status.HTTP_400_BAD_REQUEST)

        profile, _ = MediaProfile.objects.get_or_create(user=request.user)
        existing = profile.categories or {}
        profile.categories = {c: existing.get(c, 10) for c in categories}
        profile.save()

        return Response({"detail": "Interests set successfully", "categories": profile.categories})


# ---------------------------
# Get Videos API (feed)
# ---------------------------
class LoginGetVideo(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        profile, _ = MediaProfile.objects.get_or_create(user=request.user)
        user_interest = profile.categories
        creeked_author_ids = set(
            Creek.objects.filter(author=request.user)
            .values_list('account__user_id', flat=True)
        )

        approved_videos = (
            Video.objects.filter(is_approved=True)
            .select_related('category')
            .annotate(
                num_likes=Count('likes', distinct=True),
                num_dislikes=Count('dispikes', distinct=True),
            )
        )

        CANDIDATE_LIMIT = 500
        candidates = list(approved_videos.order_by('-timestamp')[:CANDIDATE_LIMIT])

        # Build co-watch map from user's recent history
        user_recent_ids = ranking.get_user_recent_video_ids(request.user)
        candidate_ids = [v.id for v in candidates]
        cowatch_map = ranking.build_cowatch_map(user_recent_ids, candidate_ids) if user_recent_ids else {}

        ranked = ranking.rank_videos(
            candidates, user_interest, creeked_author_ids,
            cowatch_map=cowatch_map, user_recent_video_ids=user_recent_ids,
        )

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


class GuestGetVideo(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        approved_videos = (
            Video.objects.filter(is_approved=True)
            .select_related('category')
            .annotate(
                num_likes=Count('likes', distinct=True),
                num_dislikes=Count('dispikes', distinct=True),
            )
        )

        CANDIDATE_LIMIT = 500
        candidates = list(approved_videos.order_by('-timestamp')[:CANDIDATE_LIMIT])
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


# ---------------------------
# Studio
# ---------------------------
class GetOwnVideo(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        videos = Video.objects.filter(author=request.user).annotate(
            num_likes=Count('likes', distinct=True),
            num_dislikes=Count('dispikes', distinct=True),
        )
        serializer = VideoSerializer(videos, many=True, context={'request': request})
        return Response(serializer.data, status=200)


class Categories(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        categories = CategoryVideo.objects.annotate(video_count=Count('videos'))
        serializer = CategoryVideoSerializer(categories, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)


class CategoryManage(APIView):
    """Create or delete categories. Only moderators can manage."""
    permission_classes = [IsAuthenticated, IsModerator]

    def post(self, request):
        name = request.data.get("name", "").strip()
        slug = request.data.get("slug", "").strip().lower()
        if not name or not slug:
            return Response({"detail": "Name and slug are required"}, status=400)
        if CategoryVideo.objects.filter(slug=slug).exists():
            return Response({"detail": "Category with this slug already exists"}, status=400)
        cat = CategoryVideo.objects.create(name=name, slug=slug)
        return Response(CategoryVideoSerializer(cat).data, status=201)

    def delete(self, request):
        cat_id = request.query_params.get("id")
        if not cat_id:
            return Response({"detail": "Category ID required"}, status=400)
        try:
            cat = CategoryVideo.objects.get(id=cat_id)
            cat.delete()
            return Response(status=204)
        except CategoryVideo.DoesNotExist:
            return Response({"detail": "Category not found"}, status=404)


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
            video.is_approved = False
        if category:
            category_obj, _ = CategoryVideo.objects.get_or_create(
                slug=category,
                defaults={"name": category.replace("-", " ").title()},
            )
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
# Watch Video API
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
            profile.categories = ranking.adjust_category_score(
                profile.categories, video.category.slug, ranking.WATCH_BOOST
            )
            profile.save()

        # Record watch event with spam prevention
        # Check view dedup BEFORE creating the event
        session_id = get_or_create_session_id(request.user)
        record_view(request.user, video)
        WatchEvent.objects.create(
            user=request.user,
            video=video,
            session_id=session_id,
        )

        # Co-watch powered related videos
        user_recent_ids = ranking.get_user_recent_video_ids(request.user)
        if user_recent_ids:
            cowatch_map = ranking.build_cowatch_map(user_recent_ids, [video.id])
            # Get co-watch related video IDs, sorted by score
            sorted_cowatch = sorted(cowatch_map.items(), key=lambda x: x[1], reverse=True)
            cowatch_video_ids = [vid for vid, _ in sorted_cowatch[:10]]
            if cowatch_video_ids:
                cowatch_videos = list(
                    approved_videos.filter(id__in=cowatch_video_ids)
                    .select_related('category')
                    .annotate(
                        num_likes=Count('likes', distinct=True),
                        num_dislikes=Count('dispikes', distinct=True),
                    )
                )
                # Sort by co-watch score
                cowatch_order = {vid: i for i, vid in enumerate(cowatch_video_ids)}
                cowatch_videos.sort(key=lambda v: cowatch_order.get(v.id, 999))
                # Fill remaining with category-based
                remaining = 5 - len(cowatch_videos)
                if remaining > 0:
                    cat_vids = list(
                        approved_videos.filter(category=video.category)
                        .exclude(id=video.id)
                        .exclude(id__in=cowatch_video_ids)
                        .order_by('-timestamp')[:remaining]
                    )
                    cowatch_videos.extend(cat_vids)
                related_videos = cowatch_videos[:5]
            else:
                related_videos = list(
                    approved_videos.filter(category=video.category)
                    .exclude(id=video.id)
                    .order_by('-timestamp')[:5]
                )
        else:
            related_videos = list(
                approved_videos.filter(category=video.category)
                .exclude(id=video.id)
                .order_by('-timestamp')[:5]
            )

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

        like_count = Like.objects.filter(video=video).count()
        dispike_count = DisPike.objects.filter(video=video).count()
        video_author_channel = MediaProfile.objects.filter(user=video.author).first()
        creek_count = Creek.objects.filter(account=video_author_channel).count() if video_author_channel else 0

        return Response({
            "video": VideoSerializer(video, context={'request': request}).data,
            "related_videos": VideoSerializer(related_videos, many=True, context={'request': request}).data,
            "like": False,
            "like_count": like_count,
            "dispike": False,
            "dispike_count": dispike_count,
            "creek": False,
            "creek_count": creek_count,
        }, status=status.HTTP_200_OK)


# ---------------------------
# Watch Retention Tracking
# ---------------------------
class TrackRetention(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        video_id = request.data.get("video_id")
        duration = request.data.get("duration", 0)

        if not video_id:
            return Response({"detail": "Video ID required"}, status=400)

        try:
            duration = int(duration)
        except (TypeError, ValueError):
            duration = 0

        duration = max(0, min(duration, 86400))  # cap at 24 hours

        video = get_object_or_404(Video, id=video_id)
        session_id = get_or_create_session_id(request.user)

        # Update the most recent WatchEvent for this user+video+session
        recent = (
            WatchEvent.objects.filter(user=request.user, video=video, session_id=session_id)
            .order_by('-timestamp')
            .first()
        )
        if recent:
            recent.duration_watched = max(recent.duration_watched, duration)
            recent.save(update_fields=['duration_watched'])

        return Response({"status": "ok"}, status=200)


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
        ).order_by("-id")[:20]

        users = MediaProfile.objects.filter(
            Q(user__username__icontains=title)
        ).select_related('user')[:10]

        video_serializer = VideoSerializer(videos, many=True, context={'request': request})
        user_serializer = MediaProfileSerializer(users, many=True, context={'request': request})

        return Response({
            "videos": video_serializer.data,
            "users": user_serializer.data,
        }, status=status.HTTP_200_OK)


class SearchUsers(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        q = request.query_params.get("q", "").strip()
        if not q:
            return Response([], status=status.HTTP_200_OK)

        users = MediaProfile.objects.filter(
            Q(user__username__icontains=q)
        ).select_related('user')[:8]

        serializer = MediaProfileSerializer(users, many=True, context={'request': request})
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
        serializer = VideoSerializer(video, context={'request': request})
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
        top_level = Comment.objects.filter(video=video, parent=None).select_related('author').order_by('-is_pinned', '-timestamp')

        serializer = CommentSerializer(top_level, many=True, context={'request': request})
        return Response(serializer.data, status=status.HTTP_200_OK)


@method_decorator(csrf_exempt, name='dispatch')
class UploadCommentVideo(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        author = request.user
        comment_text = request.data.get('comment')
        video_id = request.data.get('video_id')
        parent_id = request.data.get('parent_id')

        if not comment_text:
            return Response({'message': 'No comment provided'}, status=status.HTTP_400_BAD_REQUEST)

        video = get_object_or_404(Video, id=video_id)

        parent = None
        if parent_id:
            parent = get_object_or_404(Comment, id=parent_id, video=video)

        comment = Comment.objects.create(author=author, video=video, text=comment_text, parent=parent)

        return Response({
            'message': 'Comment added successfully',
            'comment': CommentSerializer(comment, context={'request': request}).data
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


@method_decorator(csrf_exempt, name='dispatch')
class PinComment(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        comment_id = request.data.get("comment_id")
        if not comment_id:
            return Response({"detail": "Comment ID required"}, status=status.HTTP_400_BAD_REQUEST)

        comment = get_object_or_404(Comment, id=comment_id)

        if comment.video.author != request.user:
            return Response({"detail": "Only the video author can pin comments"}, status=status.HTTP_403_FORBIDDEN)

        comment.is_pinned = not comment.is_pinned
        comment.save(update_fields=['is_pinned'])

        return Response({"is_pinned": comment.is_pinned}, status=status.HTTP_200_OK)


class DisPikeVideo(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        video_id = request.data.get("id")
        if not video_id:
            return Response({"detail": "Video ID required"}, status=status.HTTP_400_BAD_REQUEST)

        video = get_object_or_404(Video, id=video_id)
        dispike, created = DisPike.objects.get_or_create(author=request.user, video=video)

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
# Upload Video API (with spam prevention)
# ---------------------------
class UploadVideo(APIView):
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request):
        # Rate limit check
        if not check_upload_rate_limit(request.user):
            return Response(
                {"message": "Upload rate limit reached. Please wait before uploading again."},
                status=429
            )

        author = request.user
        video_file = request.data.get("video")
        category = request.data.get('category')
        title = request.data.get("title")
        description = request.data.get("description")
        thumbnail = request.data.get("thumbnail")

        if not video_file:
            return Response({"message": "No video provided"}, status=400)

        category_obj, _ = CategoryVideo.objects.get_or_create(
            slug=category,
            defaults={"name": category.replace("-", " ").title()},
        )

        video_instance = Video.objects.create(
            video=video_file,
            author=author,
            title=title,
            category=category_obj,
            description=description,
            thumbnail=thumbnail,
            timestamp=timezone.now(),
            is_approved=False,
        )

        record_upload(request.user)

        serializer = VideoSerializer(video_instance, context={'request': request})
        return Response(serializer.data, status=201)


class StudioComments(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        videos = Video.objects.filter(author=request.user)
        comments = Comment.objects.filter(video__in=videos).select_related('author', 'video').order_by('-timestamp')
        data = [{
            'id': c.id,
            'text': c.text,
            'author': c.author.username,
            'video_id': c.video.id,
            'video_title': c.video.title,
            'timestamp': c.timestamp,
            'is_pinned': c.is_pinned,
        } for c in comments]
        return Response(data, status=200)

    def delete(self, request):
        comment_id = request.query_params.get('id')
        if not comment_id:
            return Response({'detail': 'Comment ID required'}, status=400)
        try:
            comment = Comment.objects.select_related('video').get(
                id=comment_id, video__author=request.user
            )
            comment.delete()
            return Response(status=204)
        except Comment.DoesNotExist:
            return Response({'detail': 'Comment not found'}, status=404)


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
            "videos": VideoSerializer(videos, many=True, context={'request': request}).data,
            "creek_count": creek_count,
        }, status=200)
