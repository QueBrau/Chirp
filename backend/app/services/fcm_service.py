"""Content-free push notifications (log-only no-op until milestone 4)."""
import logging

logger = logging.getLogger(__name__)


async def send_content_free_push(user_id: str, title: str) -> None:
    """No-op push stub; logs the recipient id only (never message content).

    # TODO(milestone-4): wire FCM via Expo Notifications; payload stays content-free
    # ("New message from Maria") and deep-links into the thread.
    """
    logger.info("content-free push queued user_id=%s", user_id)
