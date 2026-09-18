import hashlib
import redis


redis_client = redis.Redis(
    host="localhost",
    port=6379,
    decode_responses=True
)

TTL_SECONDS = 86400  # 24 hours


def create_fingerprint(event):
    headline = event.get("headline", "").strip().lower()
    url = event.get("url", "").strip().lower()

    raw_value = f"{headline}|{url}"

    return hashlib.sha256(
        raw_value.encode("utf-8")
    ).hexdigest()


def is_duplicate(event):
    fingerprint = create_fingerprint(event)

    key = f"mias:event:{fingerprint}"

    # SET NX means: set only if key does not already exist
    created = redis_client.set(
        key,
        "1",
        nx=True,
        ex=TTL_SECONDS
    )

    if created:
        return False

    return True
