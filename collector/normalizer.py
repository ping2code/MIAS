from datetime import datetime
from email.utils import parsedate_to_datetime


def normalize_entry(entry, source_name="Unknown"):
    published_raw = entry.get("published")

    published_at = None

    if published_raw:
        try:
            published_at = parsedate_to_datetime(
                published_raw
            ).isoformat()
        except Exception:
            published_at = published_raw

    publisher = (
        entry.get("source", {}).get("title")
        if isinstance(entry.get("source"), dict)
        else None
    )

    event = {
        "source": source_name,
        "publisher": publisher or "Unknown",
        "headline": entry.get("title", "N/A"),
        "url": entry.get("link", "N/A"),
        "published_at": published_at,
        "summary": entry.get("summary", ""),
    }

    return event
