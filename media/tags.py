"""Hashtag support for CreekTube.

Any user can write ``#whatever`` in a title or description and it becomes a
community tag. Tags power the ``/interests/<tag>`` pages and rank *above*
category interest in the feed algorithm (e.g. inside the Music category the
user may follow a specific person via ``#theneighbourhood``).
"""

import re

from .models import Tag

TAG_RE = re.compile(r"#([A-Za-z0-9_][A-Za-z0-9_-]*)")

MAX_TAGS_PER_ITEM = 12
MAX_TAG_LENGTH = 50


def extract_hashtags(*texts):
    """Return a de-duplicated, normalized list of tag names from text(s).

    Tags are lower-cased so ``#Music`` normalizes to ``music``. Tags over the
    length limit are skipped.
    """
    seen = set()
    tags = []
    for text in texts:
        if not text:
            continue
        for match in TAG_RE.findall(str(text)):
            name = match.strip().lower()
            if len(name) > MAX_TAG_LENGTH or name in seen:
                continue
            seen.add(name)
            tags.append(name)
    return tags


def ensure_tags(names):
    """Resolve a list of tag names into ``Tag`` instances (never raises)."""
    resolved = []
    for name in extract_hashtags(" ".join(names)):
        tag, _ = Tag.objects.get_or_create(name=name)
        resolved.append(tag)
    return resolved[:MAX_TAGS_PER_ITEM]


def apply_tags(instance, *texts):
    """Set the M2M tags on a Video/Snip instance from its title+description."""
    tags = ensure_tags([t for t in texts if t])
    instance.tags.set(tags)
    return tags


def tag_names_for(instance):
    """Return the normalized tag names for a Video/Snip instance (list)."""
    if instance is None:
        return []
    return list(instance.tags.order_by("name").values_list("name", flat=True))


def resolve_tag(name):
    """Return the ``Tag`` for a (possibly ``#``-prefixed) name, or None."""
    if not name:
        return None
    cleaned = str(name).strip().lstrip("#").lower()
    if not cleaned:
        return None
    try:
        return Tag.objects.get(name=cleaned)
    except Tag.DoesNotExist:
        return None
