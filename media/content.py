"""Unified content classification for CreekTube.

Every piece of content (native CreekTube video/snip or a YouTube reference)
has two orthogonal attributes:

* ``source_type``  -- CREEKTUBE | YOUTUBE
* ``content_type`` -- VIDEO | SNIP

A SNIP is content whose duration is known and strictly under 120 seconds.
Names alone ("Short", "Reel", "Clip", "TikTok", "Snip") never classify
content; duration must be available. Exactly 120 seconds is NOT a Snip by
default (the rule is "under 2 minutes").

Unknown duration (0) is treated as a normal VIDEO so we never mislabel
content we cannot measure.
"""

# A Snip is content with a duration UNDER this many seconds.
SNIP_MAX_DURATION_SECONDS = 120

VIDEO = "VIDEO"
SNIP = "SNIP"


def classify_content_type(duration):
    """Return VIDEO or SNIP for a duration in seconds.

    ``duration`` of 0/None means unknown and therefore VIDEO.
    Exactly ``SNIP_MAX_DURATION_SECONDS`` is NOT a snip.
    """
    if duration is None:
        return VIDEO
    try:
        duration = int(duration)
    except (TypeError, ValueError):
        return VIDEO
    if duration > 0 and duration < SNIP_MAX_DURATION_SECONDS:
        return SNIP
    return VIDEO


def is_snip(duration):
    """True when ``duration`` qualifies as a Snip."""
    return classify_content_type(duration) == SNIP
