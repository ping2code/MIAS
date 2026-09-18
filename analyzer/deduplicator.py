import hashlib
import redis

from shared.config import (
    REDIS_HOST,
    REDIS_PORT,
    DEDUP_TTL_SECONDS,
)
from shared.logger import get_logger


logger = get_logger("deduplicator")


redis_client = redis.Redis(
    host=REDIS_HOST,
    port=REDIS_PORT,
    decode_responses=True,
    socket_connect_timeout=3,
    socket_timeout=3,
)


def create_fingerprint(event):
    headline = event.get(
        "headline",
        ""
    ).strip().lower()

    url = event.get(
        "url",
        ""
    ).strip().lower()

    raw_value = f"{headline}|{url}"

    return hashlib.sha256(
        raw_value.encode("utf-8")
    ).hexdigest()


def is_duplicate(event):
    fingerprint = create_fingerprint(event)

    key = f"mias:event:{fingerprint}"

    try:
        created = redis_client.set(
            key,
            "1",
            nx=True,
            ex=DEDUP_TTL_SECONDS
        )

        return not bool(created)

    except redis.RedisError as error:
        logger.error(
            "Redis deduplication unavailable: %s",
            error
        )

        # Fail open:
        # continue processing instead of dropping the event
        return False
