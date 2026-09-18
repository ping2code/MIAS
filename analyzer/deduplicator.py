import hashlib


_seen_fingerprints = set()


def create_fingerprint(event):
    headline = event.get("headline", "").strip().lower()
    url = event.get("url", "").strip().lower()

    raw_value = f"{headline}|{url}"

    return hashlib.sha256(
        raw_value.encode("utf-8")
    ).hexdigest()


def is_duplicate(event):
    fingerprint = create_fingerprint(event)

    if fingerprint in _seen_fingerprints:
        return True

    _seen_fingerprints.add(fingerprint)
    return False
