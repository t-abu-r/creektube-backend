"""One-off backfill for auto-materialized YouTube rows.

Rows created before the system-account fix are owned by whoever happened to
watch/like the video, which made the author show up as that user and the
upload date default to "now". This command re-points every stored YouTube
row to the reserved ``youtube_system`` account (so the real channel name is
shown) and, when a ``YOUTUBE_API_KEY`` is available, refreshes the real
publish date, channel name, and channel id from the Data API.

Safe to re-run; creator-added rows (``AddYouTubeVideo``) are also normalized
so the whole YouTube surface behaves uniformly.
"""

from datetime import datetime, timezone

from django.core.management.base import BaseCommand
from django.utils.timezone import now

from media.models import Like, Video
from media.youtube import (
    YOUTUBE_SYSTEM_USERNAME,
    get_youtube_video_details,
    youtube_system_user,
)


class Command(BaseCommand):
    help = "Re-point stored YouTube rows to the system account and refresh real metadata."

    def handle(self, *args, **options):
        system_user = youtube_system_user()
        rows = Video.objects.filter(source_type="YOUTUBE").exclude(
            youtube_video_id=""
        )
        self.stdout.write(f"Found {rows.count()} stored YouTube row(s)")

        repointed = 0
        refreshed = 0
        for video in rows:
            if video.author_id != system_user.id:
                # Keep the CreekTube like associations intact: likes live on
                # the Like table keyed to the row, not the author.
                video.author = system_user
                repointed += 1
            details = get_youtube_video_details(video.youtube_video_id)
            if details:
                video.youtube_channel_name = (
                    details.get("youtube_channel_name")
                    or details.get("author")
                    or video.youtube_channel_name
                    or "YouTube"
                )
                video.youtube_channel_id = (
                    details.get("youtube_channel_id") or video.youtube_channel_id
                )
                published = details.get("timestamp")
                if published:
                    try:
                        parsed = datetime.fromisoformat(
                            str(published).replace("Z", "+00:00")
                        )
                        if parsed.tzinfo is None:
                            parsed = parsed.replace(tzinfo=timezone.utc)
                        video.timestamp = parsed
                    except ValueError:
                        pass
                refreshed += 1
            video.save(update_fields=[
                "author",
                "timestamp",
                "youtube_channel_name",
                "youtube_channel_id",
            ])

        self.stdout.write(self.style.SUCCESS(
            f"Re-pointed {repointed} row(s) to {YOUTUBE_SYSTEM_USERNAME}; "
            f"refreshed {refreshed} row(s) from the Data API"
        ))
