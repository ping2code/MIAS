"""Normalize official monetary-policy RSS entries into MIAS events."""

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import urlparse

from collector.normalizer import normalize_entry
from analyzer.relevance_detector import detect_symbols


class _TextParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def _plain_text(value):
    parser = _TextParser()
    parser.feed(str(value or ""))
    return " ".join(" ".join(parser.parts).split())


def normalize_fed_entry(entry):
    event = normalize_entry(entry, source_name="Federal Reserve")
    event["headline"] = _plain_text(entry.get("title"))
    event["summary"] = _plain_text(entry.get("summary"))
    event["url"] = str(entry.get("link") or "").strip()
    parsed_url = urlparse(event["url"])
    if (not event["headline"] or parsed_url.scheme != "https"
            or parsed_url.hostname != "www.federalreserve.gov"
            or parsed_url.username or parsed_url.password):
        raise ValueError("Fed entry requires a headline and official HTTPS URL")

    raw_date = entry.get("published") or entry.get("updated")
    event["published_at"] = None
    if raw_date:
        try:
            try:
                published = parsedate_to_datetime(raw_date)
            except (ValueError, TypeError):
                published = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
            if published.tzinfo is None:
                published = published.replace(tzinfo=timezone.utc)
            event["published_at"] = published.astimezone(timezone.utc).isoformat()
        except (ValueError, TypeError, AttributeError, OverflowError):
            pass

    headline = event["headline"].lower()
    if "minutes" in headline:
        category = "minutes"
    elif "economic projections" in headline:
        category = "economic_projections"
    elif "statement" in headline and ("fomc" in headline or "open market committee" in headline):
        category = "policy_statement"
    elif any(term in headline for term in (
        "federal funds", "interest rate", "policy rate", "discount rate",
        "monetary policy action",
    )):
        category = "policy_action"
    else:
        category = "policy_communication"

    event = detect_symbols(event)
    event.update(
        publisher="Federal Reserve",
        relevant=True,
        event_type="fed_policy",
        fed_category=category,
        market_scope="US macro",
    )
    return event
