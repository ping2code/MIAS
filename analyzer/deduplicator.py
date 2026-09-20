import hashlib
import redis

from shared.config import (
    REDIS_HOST,
    REDIS_PORT,
    DEDUP_TTL_SECONDS,
    NEAR_DUPLICATE_THRESHOLD,
    HEADLINE_TTL_SECONDS,
)

from shared.logger import get_logger

from analyzer.headline_similarity import (
    normalize_headline,
    headline_similarity,
)

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


def is_duplicate(event, namespace="event"):
    fingerprint = create_fingerprint(event)

    key = f"mias:{namespace}:{fingerprint}"

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
def is_near_duplicate_headline(event):
    headline = event.get("headline", "").strip()

    if not headline:
        return False

    normalized = normalize_headline(headline)

    try:
        keys = redis_client.scan_iter(
            match="mias:news:headline:*"
        )

        for key in keys:
            existing_headline = redis_client.get(key)

            if not existing_headline:
                continue

            similarity = headline_similarity(
                normalized,
                existing_headline
            )

            if similarity >= NEAR_DUPLICATE_THRESHOLD:
                logger.info(
                    "Near-duplicate headline skipped "
                    "similarity=%.2f headline=%s",
                    similarity,
                    headline,
                )
                return True

        headline_hash = hashlib.sha256(
            normalized.encode("utf-8")
        ).hexdigest()

        redis_client.set(
            f"mias:news:headline:{headline_hash}",
            normalized,
            ex=HEADLINE_TTL_SECONDS,
        )

        return False

    except redis.RedisError as error:
        logger.error(
            "Redis headline deduplication unavailable: %s",
            error,
        )
        return False
