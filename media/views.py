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
from .models import (Video, Comment, CommentLike, CategoryVideo, MediaProfile, Like,
                     DisPike, Creek, WatchEvent, UploadRateLimit, Notification, Snip, SnipLike)
from django.db.models import Count, Q, Sum, Avg, F
from . import ranking
import logging
from django.db.models.functions import TruncDate
logger = logging.getLogger(__name__)
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser
from datetime import timedelta


# ---------------------------
# Spam Prevention Helpers
# ---------------------------
UPLOAD_RATE_LIMIT = 3  # max uploads per hour
UPLOAD_RATE_WINDOW = timedelta(hours=1)
VIEW_GAP = timedelta(minutes=2)  # min time between views from same user on same video
VIEW_MAX_PER_USER = 6  # max views per user per video/snip
COMMENT_SPAM_LIMIT = 6  # max comments per video
COMMENT_SPAM_WINDOW = timedelta(minutes=2)


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


def record_view(user, video=None, snip=None):
    """
    Create a watch event + update view_count atomically.
    Anti-spam: 2min gap between views, max 6 per user per item.
    Returns the created WatchEvent or None if blocked.
    """
    if not user or not user.is_authenticated:
        return None

    if video:
        events = WatchEvent.objects.filter(user=user, video=video)
    elif snip:
        events = WatchEvent.objects.filter(user=user, snip=snip)
    else:
        return None

    now = timezone.now()

    # Time gap check
    last = events.order_by('-timestamp').first()
    if last and (now - last.timestamp) < VIEW_GAP:
        return None

    # Max views check — delete oldest if exceeded
    if events.count() >= VIEW_MAX_PER_USER:
        events.order_by('timestamp').first().delete()

    event = WatchEvent.objects.create(user=user, video=video, snip=snip)

    # Recompute unique viewers for the denormalized counter
    if video:
        unique = WatchEvent.objects.filter(video=video).values('user').distinct().count()
        Video.objects.filter(pk=video.pk).update(view_count=unique)
    elif snip:
        unique = WatchEvent.objects.filter(snip=snip).values('user').distinct().count()
        Snip.objects.filter(pk=snip.pk).update(view_count=unique)

    return event


def check_comment_spam(user, video):
    """Returns True if user is under limit, False if spam detected."""
    cutoff = timezone.now() - COMMENT_SPAM_WINDOW
    recent = Comment.objects.filter(
        author=user, video=video, timestamp__gte=cutoff
    ).count()
    return recent < COMMENT_SPAM_LIMIT


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
        category_param = request.query_params.get('category', '').strip().lower()

        if category_param in ['shortform-videos', 'snips']:
            snips = Snip.objects.filter(is_approved=True, visibility="public").select_related('author').order_by('-timestamp')
            total = snips.count()
            try:
                page = max(int(request.query_params.get('page', 1)), 1)
            except (TypeError, ValueError):
                page = 1
            try:
                page_size = min(max(int(request.query_params.get('page_size', 20)), 1), 100)
            except (TypeError, ValueError):
                page_size = 20

            start = (page - 1) * page_size
            page_snips = snips[start:start + page_size]
            results = [
                {
                    "id": s.id,
                    "title": s.title,
                    "description": s.description,
                    "thumbnail": s.video,
                    "video": s.video,
                    "timestamp": s.timestamp,
                    "is_approved": s.is_approved,
                    "author": s.author.username,
                    "author_id": getattr(getattr(s.author, "mediaprofile", None), "id", s.author.id),
                    "author_avatar": Profile.objects.filter(user=s.author).first().avatar.url if Profile.objects.filter(user=s.author).exists() else None,
                    "category": "shortform-videos",
                    "category_name": "Shortform Videos",
                    "view_count": s.view_count,
                    "is_snip": True,
                }
                for s in page_snips
            ]
            return Response({
                "results": results,
                "page": page,
                "page_size": page_size,
                "count": total,
            }, status=200)

        user_interest = profile.categories
        creeked_author_ids = set(
            Creek.objects.filter(author=request.user)
            .values_list('account__user_id', flat=True)
        )

        approved_videos = (
            Video.objects.filter(is_approved=True, visibility="public")
            .select_related('category')
            .annotate(
                num_likes=Count('likes', distinct=True),
                num_dislikes=Count('dispikes', distinct=True),
            )
        )

        if category_param:
            approved_videos = approved_videos.filter(category__slug=category_param)

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
        # Single video by ID (used for SSR/metadata)
        video_id = request.query_params.get('video_id')
        if video_id:
            video = Video.objects.filter(id=video_id, is_approved=True, visibility="public").select_related('author').first()
            if not video:
                return Response({"detail": "Video not found"}, status=404)
            serializer = VideoSerializer(video, many=False, context={'request': request})
            return Response(serializer.data, status=200)

        category_param = request.query_params.get('category', '').strip().lower()

        if category_param in ['shortform-videos', 'snips']:
            snips = Snip.objects.filter(is_approved=True, visibility="public").select_related('author').order_by('-timestamp')
            total = snips.count()
            try:
                page = max(int(request.query_params.get('page', 1)), 1)
            except (TypeError, ValueError):
                page = 1
            try:
                page_size = min(max(int(request.query_params.get('page_size', 20)), 1), 100)
            except (TypeError, ValueError):
                page_size = 20

            start = (page - 1) * page_size
            page_snips = snips[start:start + page_size]
            # Serialize Snips
            snip_serializer = SnipSerializer(page_snips, many=True, context={'request': request})

            # Add avatar
            # Profile = Profile.objects.filter(user=s.author).first()
            results = [
                {
                    "id": s.id,
                    "title": s.title,
                    "description": s.description,
                    "thumbnail": s.video,
                    "video": s.video,
                    "timestamp": s.timestamp,
                    "is_approved": s.is_approved,
                    "author": s.author.username,
                    "author_id": getattr(getattr(s.author, "mediaprofile", None), "id", s.author.id),
                    "author_avatar": Profile.objects.filter(user=s.author).first().avatar.url if Profile.objects.filter(user=s.author).exists() else None,
                    "category": "shortform-videos",
                    "category_name": "Shortform Videos",
                    "view_count": s.view_count,
                    "is_snip": True,
                }
                for s in snip_serializer.instance
            ]
            return Response({
                "results": results,
                "page": page,
                "page_size": page_size,
                "count": total,
            }, status=200)

        approved_videos = (
            Video.objects.filter(is_approved=True, visibility="public")
            .select_related('category')
            .annotate(
                num_likes=Count('likes', distinct=True),
                num_dislikes=Count('dispikes', distinct=True),
            )
        )

        if category_param:
            approved_videos = approved_videos.filter(category__slug=category_param)

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
        CategoryVideo.objects.get_or_create(
            slug="shortform-videos",
            defaults={"name": "Shortform Videos"}
        )
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
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def patch(self, request):
        item_id = request.data.get("id")
        item_type = request.data.get("type", "video")
        if not item_id:
            return Response({"detail": "ID required"}, status=400)

        title = request.data.get("title")
        description = request.data.get("description")
        visibility = request.data.get("visibility")

        if item_type == "snip":
            snip = get_object_or_404(Snip, id=item_id, author=request.user)
            if title:
                snip.title = title
            if description is not None:
                snip.description = description
            if visibility in ("public", "unlisted", "private"):
                snip.visibility = visibility
            snip.save()
            return Response(SnipSerializer(snip, context={'request': request}).data, status=200)

        video = get_object_or_404(Video, id=item_id, author=request.user)
        thumbnail_url = request.data.get("thumbnail_url")
        video_url = request.data.get("video_url")
        category = request.data.get("category")

        if title:
            video.title = title
        if description:
            video.description = description
        if thumbnail_url:
            video.thumbnail = thumbnail_url
        if video_url:
            video.video = video_url
            video.is_approved = False
        if category:
            category_obj, _ = CategoryVideo.objects.get_or_create(
                slug=category,
                defaults={"name": category.replace("-", " ").title()},
            )
            video.category = category_obj
        if visibility in ("public", "unlisted", "private"):
            video.visibility = visibility

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

        # Allow public + unlisted for everyone, private only for owner
        video = get_object_or_404(Video.objects.filter(is_approved=True).prefetch_related("comments__author"), id=video_id)
        approved_videos = Video.objects.filter(is_approved=True, visibility="public")
        if video.visibility == "private" and video.author != request.user:
            return Response({"detail": "Video not found"}, status=404)

        # Boost category score
        profile, _ = MediaProfile.objects.get_or_create(user=request.user)
        if video.category:
            profile.categories = ranking.adjust_category_score(
                profile.categories, video.category.slug, ranking.WATCH_BOOST
            )
            profile.save()

        # Record watch event with spam prevention
        record_view(request.user, video=video)

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

        approved_videos = Video.objects.filter(is_approved=True, visibility="public")
        video = get_object_or_404(Video.objects.filter(is_approved=True), id=video_id)
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
            duration = max(0, min(int(duration), 86400))
        except (TypeError, ValueError):
            duration = 0

        video = get_object_or_404(Video, id=video_id)
        event = (
            WatchEvent.objects.filter(user=request.user, video=video)
            .order_by('-timestamp')
            .first()
        )
        if event and event.duration_watched < duration:
            event.duration_watched = duration
            event.save(update_fields=['duration_watched'])

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
        unapproved_snips = Snip.objects.filter(is_approved=False)
        video_serializer = VideoSerializer(unapproved_videos, many=True, context={'request': request})
        snip_serializer = SnipSerializer(unapproved_snips, many=True, context={'request': request})
        return Response({
            "videos": video_serializer.data,
            "snips": snip_serializer.data
        }, status=status.HTTP_200_OK)

    def post(self, request):
        video_id = request.data.get("id")
        if not video_id:
            return Response({"detail": "Video ID not provided"}, status=status.HTTP_400_BAD_REQUEST)
        try:
            video = Video.objects.get(id=video_id)
            video.is_approved = not video.is_approved
            video.save()
        except:
            snips = Snip.objects.filter(id=video_id)
            if snips.exists():
                video = snips.first()
                video.is_approved = not video.is_approved
                video.save()
                snip_serializer = SnipSerializer(video, context={'request': request})
                return Response(snip_serializer.data, status=status.HTTP_200_OK)
            else:
                return Response({"detail": "Video not found"}, status=status.HTTP_404_NOT_FOUND)
        # video = get_object_or_404(Video, id=video_id)
        
        serializer = VideoSerializer(video, context={'request': request})
        return Response(serializer.data, status=status.HTTP_200_OK)

    def delete(self, request):
        video_id = request.data.get("id")
        if not video_id:
            return Response({"detail": "Video ID not provided"}, status=status.HTTP_400_BAD_REQUEST)
        try:
            video = Video.objects.get(id=video_id)
        except Video.DoesNotExist:
            snips = Snip.objects.filter(id=video_id)
            if snips.exists():
                video = snips.first()
                video.delete()
                return Response({"detail": "Snip deleted"}, status=status.HTTP_204_NO_CONTENT)
            else:
                return Response({"detail": "Video not found"}, status=status.HTTP_404_NOT_FOUND)
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

        # Spam check
        if not check_comment_spam(author, video):
            return Response(
                {'message': 'You are posting too many comments. Please wait before posting again.'},
                status=status.HTTP_429_TOO_MANY_REQUESTS
            )

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

        if account.user == request.user:
            return Response({"detail": "You cannot creek your own channel"}, status=status.HTTP_400_BAD_REQUEST)

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
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def post(self, request):
        # Rate limit check
        if not check_upload_rate_limit(request.user):
            return Response(
                {"message": "Upload rate limit reached. Please wait before uploading again."},
                status=429
            )

        author = request.user
        video_url = request.data.get("video_url")
        video_public_id = request.data.get("video_public_id", "")
        thumbnail_url = request.data.get("thumbnail_url")
        thumbnail_public_id = request.data.get("thumbnail_public_id", "")

        if video_url and video_public_id and thumbnail_url and thumbnail_public_id:
            video_file = video_url
            thumbnail_file = thumbnail_url
        else:
            video_file = request.data.get("video")
            thumbnail_file = request.data.get("thumbnail")

        category = request.data.get('category')
        title = request.data.get("title")
        description = request.data.get("description")
        visibility = request.data.get("visibility", "public")
        if visibility not in ("public", "unlisted", "private"):
            visibility = "public"

        if not video_file:
            return Response({"message": "No video provided"}, status=400)

        # Auto-generate thumbnail from Cloudinary video URL if none provided
        if not thumbnail_file and video_url and video_public_id:
            from django.conf import settings
            cloud_name = getattr(settings, 'CLOUDINARY_STORAGE', {}).get('CLOUD_NAME', '')
            if cloud_name:
                thumbnail_file = f"https://res.cloudinary.com/{cloud_name}/video/upload/so_0,e_preview/{video_public_id}.jpg"

        category_obj, _ = CategoryVideo.objects.get_or_create(
            slug=category,
            defaults={"name": category.replace("-", " ").title()},
        )

        try:
            video_instance = Video.objects.create(
                video=video_file,
                author=author,
                title=title,
                category=category_obj,
                description=description,
                thumbnail=thumbnail_file or "",
                video_public_id=video_public_id,
                thumbnail_public_id=thumbnail_public_id,
                visibility=visibility,
                timestamp=timezone.now(),
                is_approved=False,
            )
        except Exception as e:
            # Cleanup orphaned Cloudinary uploads if DB save fails
            from .cloudinary_utils import delete_cloudinary_resource
            delete_cloudinary_resource(video_public_id, "video")
            delete_cloudinary_resource(thumbnail_public_id, "image")
            logger.error("UploadVideo DB save failed for %s: %s", author.username, e)
            return Response({"message": "Upload failed. Please try again."}, status=500)

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

    def get(self, request):
        id = request.query_params.get("id")
        if not id:
            return Response({"error": "ID is required"}, status=400)

        try:
            profile_media = MediaProfile.objects.get(id=id)
        except MediaProfile.DoesNotExist:
            return Response({"error": "Profile not found"}, status=404)

        user = profile_media.user
        videos = Video.objects.filter(author=user, is_approved=True)
        snips = Snip.objects.filter(author=user, is_approved=True)
        banner = profile_media.banner if profile_media.banner else None
        creek_count = Creek.objects.filter(account=profile_media).count()

        is_creeked = False
        if request.user.is_authenticated:
            is_creeked = Creek.objects.filter(author=request.user, account=profile_media).exists()

        try:
            user_profile = Profile.objects.get(user=user)
            profile_data = ProfileSerializer(user_profile, context={"request": request}).data
        except Profile.DoesNotExist:
            profile_data = {"avatar_url": None, "bio": None}

        total_views = 0
        for video in videos:
            total_views += video.view_count
        for snip in snips:
            total_views += snip.view_count

        # Featured video (most viewed public video)
        featured = videos.filter(visibility="public").order_by('-view_count').first()

        return Response({
            "profile": profile_data,
            "account": {
                **MediaProfileSerializer(profile_media, context={"request": request}).data,
                "banner": banner,
            },
            "stats": {
                "videos": videos.count(),
                "snips": snips.count(),
                "total_views": total_views,
                "creeks": creek_count,
                "joined": user.date_joined.strftime("%b %Y") if user.date_joined else None,
            },
            "featured": VideoSerializer(featured, context={'request': request}).data if featured else None,
            "videos": VideoSerializer(videos, many=True, context={'request': request}).data,
            "snips": SnipSerializer(snips, many=True, context={'request': request}).data,
            "creek_count": creek_count,
            "creek": is_creeked,
        }, status=200)


class NotificationList(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        limit = request.query_params.get("limit", 50)
        try:
            limit = min(int(limit), 100)
        except (TypeError, ValueError):
            limit = 50

        notifications = Notification.objects.filter(recipient=request.user)[:limit]
        serializer = NotificationSerializer(notifications, many=True, context={'request': request})
        return Response(serializer.data, status=status.HTTP_200_OK)


class NotificationUnreadCount(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        count = Notification.objects.filter(recipient=request.user, is_read=False).count()
        return Response({"count": count}, status=status.HTTP_200_OK)


class NotificationMarkRead(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        notification_id = request.data.get("id")
        if notification_id:
            Notification.objects.filter(id=notification_id, recipient=request.user).update(is_read=True)
        return Response({"status": "ok"}, status=status.HTTP_200_OK)


class NotificationMarkAllRead(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        Notification.objects.filter(recipient=request.user, is_read=False).update(is_read=True)
        return Response({"status": "ok"}, status=status.HTTP_200_OK)


@method_decorator(csrf_exempt, name='dispatch')
class CommentLikeToggle(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        comment_id = request.data.get("comment_id")
        if not comment_id:
            return Response({"detail": "Comment ID required"}, status=status.HTTP_400_BAD_REQUEST)

        comment = get_object_or_404(Comment, id=comment_id)
        like, created = CommentLike.objects.get_or_create(author=request.user, comment=comment)

        if not created:
            like.delete()
            return Response({"liked": False, "likes_count": comment.likes.count()}, status=status.HTTP_200_OK)

        return Response({"liked": True, "likes_count": comment.likes.count()}, status=status.HTTP_201_CREATED)


@method_decorator(csrf_exempt, name='dispatch')
class CommentEdit(APIView):
    permission_classes = [IsAuthenticated]

    def patch(self, request):
        comment_id = request.data.get("comment_id")
        new_text = request.data.get("text")

        if not comment_id or not new_text:
            return Response({"detail": "Comment ID and text required"}, status=status.HTTP_400_BAD_REQUEST)

        comment = get_object_or_404(Comment, id=comment_id, author=request.user)
        comment.text = new_text
        comment.edited = True
        comment.save(update_fields=['text', 'edited'])

        return Response({
            'message': 'Comment updated',
            'comment': CommentSerializer(comment, context={'request': request}).data
        }, status=status.HTTP_200_OK)


@method_decorator(csrf_exempt, name='dispatch')
class CommentDelete(APIView):
    permission_classes = [IsAuthenticated]

    def delete(self, request, comment_id):
        comment = get_object_or_404(Comment, id=comment_id, author=request.user)
        comment.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class UserSettings(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        profile, _ = Profile.objects.get_or_create(user=request.user)
        return Response({
            "username": request.user.username,
            "email": request.user.email,
            "bio": profile.bio or "",
            "avatar": request.build_absolute_uri(profile.avatar.url) if profile.avatar else None,
            "notification_reply": profile.notification_reply,
        }, status=status.HTTP_200_OK)

    def put(self, request):
        profile, _ = Profile.objects.get_or_create(user=request.user)

        bio = request.data.get("bio")
        if bio is not None:
            profile.bio = bio

        notification_reply = request.data.get("notification_reply")
        if notification_reply is not None:
            profile.notification_reply = bool(notification_reply)

        profile.save(update_fields=['bio', 'notification_reply'])

        return Response({
            "message": "Settings updated",
            "notification_reply": profile.notification_reply,
        }, status=status.HTTP_200_OK)


# ---------------------------
# Snips (Short-form videos)
# ---------------------------
class UploadSnip(APIView):
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def post(self, request):
        if not check_upload_rate_limit(request.user):
            return Response(
                {"message": "Upload rate limit reached. Please wait before uploading again."},
                status=429,
            )

        video_url = request.data.get("video_url")
        video_public_id = request.data.get("video_public_id", "")
        thumbnail_url = request.data.get("thumbnail_url", "")
        thumbnail_public_id = request.data.get("thumbnail_public_id", "")
        video_file = request.FILES.get("video") or request.FILES.get("file") or request.data.get("video")

        if not video_url and video_file and not isinstance(video_file, str):
            from django.core.files.storage import default_storage
            filename = default_storage.save(f"snips/{request.user.id}_{int(timezone.now().timestamp())}_{video_file.name}", video_file)
            video_url = default_storage.url(filename)

        if not video_url and isinstance(request.data.get("video"), str):
            video_url = request.data.get("video")

        if not video_url:
            return Response({"message": "No video provided"}, status=400)

        title = request.data.get("title", "").strip()
        if not title:
            return Response({"message": "Title is required"}, status=400)

        visibility = request.data.get("visibility", "public")
        if visibility not in ("public", "unlisted", "private"):
            visibility = "public"

        # Auto-generate thumbnail from Cloudinary video URL if none provided
        if not thumbnail_url and video_url and video_public_id:
            from django.conf import settings
            cloud_name = getattr(settings, 'CLOUDINARY_STORAGE', {}).get('CLOUD_NAME', '')
            if cloud_name:
                thumbnail_url = f"https://res.cloudinary.com/{cloud_name}/video/upload/so_0,e_preview/{video_public_id}.jpg"

        try:
            snip = Snip.objects.create(
                author=request.user,
                title=title,
                description=request.data.get("description", ""),
                video=video_url,
                thumbnail=thumbnail_url,
                video_public_id=video_public_id,
                thumbnail_public_id=thumbnail_public_id,
                visibility=visibility,
                is_approved=False,
            )
        except Exception as e:
            from .cloudinary_utils import delete_cloudinary_resource
            delete_cloudinary_resource(video_public_id, "video")
            delete_cloudinary_resource(thumbnail_public_id, "image")
            logger.error("UploadSnip DB save failed for %s: %s", request.user.username, e)
            return Response({"message": "Upload failed. Please try again."}, status=500)

        record_upload(request.user)
        serializer = SnipSerializer(snip, context={"request": request})

        try:
            from channels.layers import get_channel_layer
            from asgiref.sync import async_to_sync
            channel_layer = get_channel_layer()
            async_to_sync(channel_layer.group_send)(
                "snips_feed",
                {
                    "type": "new_snip",
                    "snip": serializer.data,
                },
            )
        except Exception:
            pass

        return Response(serializer.data, status=201)


class SnipFeed(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        try:
            page = max(int(request.query_params.get("page", 1)), 1)
        except (TypeError, ValueError):
            page = 1
        try:
            page_size = min(max(int(request.query_params.get("page_size", 12)), 1), 50)
        except (TypeError, ValueError):
            page_size = 12

        approved_snips = Snip.objects.filter(is_approved=True, visibility="public").select_related("author")
        total = approved_snips.count()

        start = (page - 1) * page_size
        snips = approved_snips[start : start + page_size]

        serializer = SnipSerializer(snips, many=True, context={"request": request})
        return Response({
            "results": serializer.data,
            "page": page,
            "page_size": page_size,
            "count": total,
        }, status=200)


class WatchSnip(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        snip_id = request.query_params.get("id")
        if not snip_id:
            return Response({"detail": "Snip ID required"}, status=400)

        try:
            snip = Snip.objects.select_related("author").get(id=snip_id)
        except Snip.DoesNotExist:
            return Response({"detail": "Snip not found"}, status=404)

        if snip.visibility == "private" and snip.author != request.user:
            return Response({"detail": "Snip not found"}, status=404)

        # Record view with spam prevention (authenticated users only)
        if request.user.is_authenticated:
            record_view(request.user, snip=snip)
        else:
            Snip.objects.filter(id=snip.id).update(view_count=F("view_count") + 1)
            snip.refresh_from_db(fields=["view_count"])

        is_liked = False
        if request.user.is_authenticated:
            is_liked = SnipLike.objects.filter(author=request.user, snip=snip).exists()

        return Response({
            **SnipSerializer(snip, context={"request": request}).data,
            "is_liked": is_liked,
        }, status=200)


class LikeSnip(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        snip_id = request.data.get("id")
        if not snip_id:
            return Response({"detail": "Snip ID required"}, status=400)

        snip = get_object_or_404(Snip, id=snip_id)
        like, created = SnipLike.objects.get_or_create(author=request.user, snip=snip)

        if not created:
            like.delete()
            Snip.objects.filter(id=snip.id).update(like_count=F("like_count") - 1)
            return Response({"is_liked": False, "like_count": max(snip.like_count - 1, 0)}, status=200)

        Snip.objects.filter(id=snip.id).update(like_count=F("like_count") + 1)
        snip.refresh_from_db(fields=["like_count"])
        return Response({"is_liked": True, "like_count": snip.like_count}, status=201)


class GetOwnSnips(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        snips = Snip.objects.filter(author=request.user).order_by("-timestamp")
        serializer = SnipSerializer(snips, many=True, context={"request": request})
        return Response(serializer.data, status=200)


class SnipDelete(APIView):
    permission_classes = [IsAuthenticated]

    def delete(self, request, snip_id):
        snip = get_object_or_404(Snip, id=snip_id, author=request.user)
        snip.delete()
        return Response(status=204)


# ---------------------------
# Snip Comments
# ---------------------------
@method_decorator(csrf_exempt, name='dispatch')
class SnipCommentList(APIView):
    def get(self, request):
        snip_id = request.query_params.get('snip_id')
        if not snip_id:
            return Response({'detail': 'snip_id required'}, status=status.HTTP_400_BAD_REQUEST)

        snip = get_object_or_404(Snip, id=snip_id)
        top_level = Comment.objects.filter(snip=snip, parent=None).select_related('author').order_by('-is_pinned', '-timestamp')
        serializer = CommentSerializer(top_level, many=True, context={'request': request})
        return Response(serializer.data, status=status.HTTP_200_OK)


@method_decorator(csrf_exempt, name='dispatch')
class UploadSnipComment(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        author = request.user
        comment_text = request.data.get('comment')
        snip_id = request.data.get('snip_id')
        parent_id = request.data.get('parent_id')

        if not comment_text:
            return Response({'detail': 'No comment provided'}, status=status.HTTP_400_BAD_REQUEST)

        snip = get_object_or_404(Snip, id=snip_id)

        cutoff = timezone.now() - COMMENT_SPAM_WINDOW
        recent = Comment.objects.filter(
            author=author, snip=snip, timestamp__gte=cutoff
        ).count()
        if recent >= COMMENT_SPAM_LIMIT:
            return Response(
                {'detail': 'You are posting too many comments. Please wait before posting again.'},
                status=status.HTTP_429_TOO_MANY_REQUESTS
            )

        parent = None
        if parent_id:
            parent = get_object_or_404(Comment, id=parent_id, snip=snip)

        comment = Comment.objects.create(author=author, snip=snip, text=comment_text, parent=parent)
        return Response({
            'detail': 'Comment added',
            'comment': CommentSerializer(comment, context={'request': request}).data
        }, status=status.HTTP_201_CREATED)


@method_decorator(csrf_exempt, name='dispatch')
class PinSnipComment(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        comment_id = request.data.get("comment_id")
        if not comment_id:
            return Response({"detail": "Comment ID required"}, status=status.HTTP_400_BAD_REQUEST)

        comment = get_object_or_404(Comment, id=comment_id)
        if not comment.snip or comment.snip.author != request.user:
            return Response({"detail": "Only the snip author can pin comments"}, status=status.HTTP_403_FORBIDDEN)

        comment.is_pinned = not comment.is_pinned
        comment.save(update_fields=['is_pinned'])
        return Response({"is_pinned": comment.is_pinned}, status=status.HTTP_200_OK)


class SnipStudioComments(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        snips = Snip.objects.filter(author=request.user)
        comments = Comment.objects.filter(snip__in=snips).select_related('author', 'snip').order_by('-timestamp')
        data = [{
            'id': c.id,
            'text': c.text,
            'author': c.author.username,
            'snip_id': c.snip.id,
            'snip_title': c.snip.title,
            'timestamp': c.timestamp,
            'is_pinned': c.is_pinned,
        } for c in comments]
        return Response(data, status=200)

    def delete(self, request):
        comment_id = request.query_params.get('id')
        if not comment_id:
            return Response({'detail': 'Comment ID required'}, status=400)
        try:
            comment = Comment.objects.select_related('snip').get(
                id=comment_id, snip__author=request.user
            )
            comment.delete()
            return Response(status=204)
        except Comment.DoesNotExist:
            return Response({'detail': 'Comment not found'}, status=404)


# ---------------------------
# Snip Retention Tracking
# ---------------------------
class TrackSnipRetention(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        snip_id = request.data.get("snip_id")
        duration = request.data.get("duration", 0)

        if not snip_id:
            return Response({"detail": "Snip ID required"}, status=400)

        try:
            duration = max(0, min(int(duration), 86400))
        except (TypeError, ValueError):
            duration = 0

        snip = get_object_or_404(Snip, id=snip_id)
        event = (
            WatchEvent.objects.filter(user=request.user, snip=snip)
            .order_by('-timestamp')
            .first()
        )
        if event and event.duration_watched < duration:
            event.duration_watched = duration
            event.save(update_fields=['duration_watched'])

        return Response({"status": "ok"}, status=200)


# ---------------------------
# Analytics API
# ---------------------------


class ChannelAnalytics(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        
        user = request.user
        period = request.query_params.get("period", "7d")

        period_map = {"24h": 1, "7d": 7, "30d": 30, "90d": 90}
        days = period_map.get(period, 7)
        since = timezone.now() - timedelta(days=days)

        my_videos = Video.objects.filter(author=user)
        my_snips = Snip.objects.filter(author=user)
        v_ids = list(my_videos.values_list('id', flat=True))
        s_ids = list(my_snips.values_list('id', flat=True))

        video_events = WatchEvent.objects.filter(video_id__in=v_ids, timestamp__gte=since) if v_ids else WatchEvent.objects.none()
        snip_events = WatchEvent.objects.filter(snip_id__in=s_ids, timestamp__gte=since) if s_ids else WatchEvent.objects.none()

        # Daily views
        video_daily = list(
            video_events.annotate(day=TruncDate('timestamp'))
            .values('day').annotate(v=Count('id'), u=Count('user', distinct=True))
            .order_by('day')
        )
        snip_daily = list(
            snip_events.annotate(day=TruncDate('timestamp'))
            .values('day').annotate(v=Count('id'), u=Count('user', distinct=True))
            .order_by('day')
        )

        # Per-content retention
        video_ret = list(
            video_events.values('video_id', 'video__title')
            .annotate(avg_dur=Avg('duration_watched'), views=Count('id'), viewers=Count('user', distinct=True))
            .order_by('-views')[:10]
        )
        snip_ret = list(
            snip_events.values('snip_id', 'snip__title')
            .annotate(avg_dur=Avg('duration_watched'), views=Count('id'), viewers=Count('user', distinct=True))
            .order_by('-views')[:10]
        )

        # Top content
        top_v = list(my_videos.order_by('-view_count')[:5].values('id', 'title', 'view_count', 'timestamp'))
        top_s = list(my_snips.order_by('-view_count')[:5].values('id', 'title', 'view_count', 'timestamp'))

        return Response({
            "totals": {
                "video_views": my_videos.aggregate(s=Sum('view_count'))['s'] or 0,
                "snip_views": my_snips.aggregate(s=Sum('view_count'))['s'] or 0,
                "total_views": (my_videos.aggregate(s=Sum('view_count'))['s'] or 0) + (my_snips.aggregate(s=Sum('view_count'))['s'] or 0),
                "video_count": my_videos.count(),
                "snip_count": my_snips.count(),
            },
            "views_over_time": {
                "videos": [{"date": str(d['day']), "total_views": d['v'], "unique_viewers": d['u']} for d in video_daily],
                "snips": [{"date": str(d['day']), "total_views": d['v'], "unique_viewers": d['u']} for d in snip_daily],
            },
            "retention": {
                "videos": [{"id": r['video_id'], "title": r['video__title'], "avg_duration": round(r['avg_dur'] or 0, 1), "total_views": r['views'], "unique_viewers": r['viewers']} for r in video_ret],
                "snips": [{"id": r['snip_id'], "title": r['snip__title'], "avg_duration": round(r['avg_dur'] or 0, 1), "total_views": r['views'], "unique_viewers": r['viewers']} for r in snip_ret],
            },
            "top_content": {"videos": top_v, "snips": top_s},
        }, status=200)
