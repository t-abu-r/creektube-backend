import logging
import cloudinary.uploader

logger = logging.getLogger(__name__)


def delete_cloudinary_resource(public_id, resource_type="video"):
    """Delete a Cloudinary resource by public_id. Safe to call with empty/None."""
    if not public_id:
        return
    try:
        cloudinary.uploader.destroy(public_id, resource_type=resource_type, invalidate=True)
    except Exception as e:
        logger.warning("Failed to delete Cloudinary %s %s: %s", resource_type, public_id, e)
