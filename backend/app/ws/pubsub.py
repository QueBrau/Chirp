"""Redis pub/sub bridge: channel-per-user JSON event fan-out."""
import json

from redis.asyncio import Redis

from app.config import get_settings

_client: Redis | None = None


def get_redis() -> Redis:
    """Create the Redis client on first use (lazy; import never touches the network)."""
    global _client
    if _client is None:
        _client = Redis.from_url(get_settings().redis_url, decode_responses=True)
    return _client


async def publish_to_user(user_id: str, event: dict) -> None:
    """JSON-encode event and publish to Redis channel user:{user_id}.

    Event shape: {"type": "<event_type>", ...payload}. Never include ciphertext
    contents beyond the opaque base64 blob field "ciphertext".
    """
    await get_redis().publish(f"user:{user_id}", json.dumps(event))
