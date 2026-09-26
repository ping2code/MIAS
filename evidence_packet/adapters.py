"""Allow-list adapters: existing durable technical rows and post-decision news/SEC events → immutable packet records.

These adapters never compute, score, decide, deduplicate fuzzily, fetch or call
AI. They only copy approved fields.

**Technical (``adapt_technical_row``):**

- Input is a durable ``snapshot_row`` (JSON-safe; the ``technical_snapshots``
  row schema).
- Only ``ROW_FIELDS`` are copied (the existing ``MATERIAL_FIELDS``, plus
  ``provider_delay_seconds`` and ``content_hash``), via a JSON round trip into a private deep copy that is then
  frozen. A caller mutating its dict afterwards cannot change the packet.
- ``validate_row`` and ``content_hash`` (existing, unchanged) must pass.
- ``bar_end`` comes from ``ExchangeCalendar.bar_end`` for the row's bar start
  (``snapshot_timestamp``) and interval.

**News and SEC (``adapt_news_event``), reusing only the existing pure helpers:**

| Family | Identity | Publication time |
|---|---|---|
| RSS ``news`` | ``persistence.adapters.news.article_identity``: ``news-url-v1``, or ``news-fingerprint-v1`` from the caller-supplied ``news_fingerprint`` | ``publication``: second precision, or unknown |
| ``sec`` | the existing durable SEC identity ``sec-v1``, keyed by the caller-supplied collector ``sec_fingerprint``; accession and URL validated with ``filing_document`` | ``filing_date``: **date only, never turned into a timestamp** |

- Output is split into facts, relevance, upstream score and delivery.
- ``ai_*`` and every key not on the allow-list are never read into the packet.

**Summary normalization (``normalize_summary``):**

- strip HTML markup (``script``/``style`` content dropped);
- decode entities;
- collapse whitespace and trim;
- cap at 1,000 Unicode characters;
- missing or empty gives ``None``; nothing is ever paraphrased or invented.
"""
from datetime import datetime
from html.parser import HTMLParser
import json
import re
from types import SimpleNamespace

from evidence_packet import models as m
from evidence_packet.serialization import freeze
from market_data.models import Interval
from persistence.adapters.news import article_identity, article_url, publication
from persistence.adapters.sec import EVENT_TYPE as SEC_EVENT_TYPE, filing_date, filing_document
from persistence.technical_snapshot_repository import ROW_FIELDS, SnapshotRowError, content_hash, validate_row

SUMMARY_MAX_CHARS = 1000
HEX64 = re.compile(r"[0-9a-f]{64}")
SCORE_FIELDS = ("impact_score", "impact_level", "score_reasons", "original_impact_score", "quality_adjustment")


class AdapterError(ValueError):
    """Input rejected by an adapter; ``reason`` is a stable code (the message is never serialized)."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self._skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in ("br", "p", "div", "li", "tr"):
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1
        elif tag in ("p", "div", "li", "tr"):
            self.parts.append(" ")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def normalize_summary(value):
    if not isinstance(value, str):
        return None
    parser = _TextExtractor()
    parser.feed(value)
    parser.close()
    text = " ".join("".join(parser.parts).split())
    return text[:SUMMARY_MAX_CHARS] or None


def adapt_technical_row(row, *, symbol, interval, calendar):
    if not isinstance(row, dict) or any(key not in row for key in ROW_FIELDS):
        raise AdapterError(m.INVALID_ROW)
    try:
        copy = json.loads(json.dumps({key: row[key] for key in ROW_FIELDS}, allow_nan=False))
        mismatch = content_hash(copy) != copy["content_hash"]
    except (TypeError, ValueError):
        raise AdapterError(m.INVALID_ROW) from None
    if mismatch:
        raise AdapterError(m.CONTENT_HASH_MISMATCH)
    try:
        validate_row(copy)  # Existing checks (vocabularies, canonical volume, and the hash again).
    except SnapshotRowError:
        raise AdapterError(m.INVALID_ROW) from None
    if copy["symbol"] != symbol:
        raise AdapterError(m.SYMBOL_MISMATCH)
    if copy["interval"] != interval:
        raise AdapterError(m.INVALID_ROW)
    try:
        start = datetime.fromisoformat(copy["snapshot_timestamp"])
        if start.utcoffset() is None:
            raise ValueError
        bar_end = calendar.bar_end(SimpleNamespace(timestamp=start, interval=Interval.parse(interval)))
    except Exception:
        raise AdapterError(m.INVALID_ROW) from None
    return m.TechnicalTimeframeEvidence(interval=interval, bar_end=bar_end, row=freeze(copy))


def _strings(values):
    if values is None:
        return ()
    if not isinstance(values, (list, tuple)) or not all(isinstance(v, str) for v in values):
        raise AdapterError(m.INVALID_EVENT)
    return tuple(values)


def _relevance(event):
    relevant = event.get("relevant")
    return m.NewsRelevance(symbols=tuple(sorted(set(_strings(event.get("symbols"))))),
                           direct_symbols=tuple(sorted(set(_strings(event.get("direct_symbols"))))),
                           related_symbols=tuple(sorted(set(_strings(event.get("related_symbols"))))),
                           relevant=relevant if isinstance(relevant, bool) else None)


def _score(event):
    values = {key: event.get(key) for key in SCORE_FIELDS}
    for key in ("impact_score", "original_impact_score", "quality_adjustment"):
        value = values[key]
        if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
            raise AdapterError(m.INVALID_EVENT)
    if values["impact_level"] is not None and not isinstance(values["impact_level"], str):
        raise AdapterError(m.INVALID_EVENT)
    values["score_reasons"] = _strings(values["score_reasons"])  # Upstream order preserved (a list, not a set).
    return m.NewsUpstreamScore(**values)


def _text(value):
    return value if isinstance(value, str) and value else None


def adapt_news_event(news_input):
    """NewsEvidenceItem for an RSS or SEC event; AdapterError(reason) otherwise."""
    event, family = news_input.event, news_input.family
    if family not in m.SUPPORTED_FAMILIES:
        raise AdapterError(m.UNSUPPORTED_FAMILY)
    if news_input.collector_outcome == m.NEAR_DUPLICATE_SUPPRESSED:
        raise AdapterError(m.NEAR_DUPLICATE_SUPPRESSED)
    observed = news_input.observed_at
    if (not isinstance(event, dict) or news_input.collector_outcome != "processed" or not isinstance(observed, datetime)
            or observed.utcoffset() is None or not isinstance(event.get("headline"), str) or not event["headline"]):
        raise AdapterError(m.INVALID_EVENT)
    sec_form = accession = None
    if family == "news":
        if event.get("event_type") is not None:
            raise AdapterError(m.INVALID_EVENT)  # RSS items carry no event_type; other families are not news.
        fingerprint = event.get("news_fingerprint")
        if article_url(event) is None and not (isinstance(fingerprint, str) and HEX64.fullmatch(fingerprint)):
            raise AdapterError(m.INVALID_EVENT)
        identity_version, event_key = article_identity(event)
        published, precision, basis, _raw = publication(event.get("published_at"))
        published_date, canonical = None, event["url"] if article_url(event) else None
    else:
        fingerprint = event.get("sec_fingerprint")
        if event.get("event_type") != SEC_EVENT_TYPE or not (isinstance(fingerprint, str) and HEX64.fullmatch(fingerprint)):
            raise AdapterError(m.INVALID_EVENT)
        try:
            accession, _document = filing_document(event)
        except ValueError:
            raise AdapterError(m.INVALID_EVENT) from None
        sec_form = _text(event.get("sec_form"))
        identity_version, event_key, canonical = "sec-v1", fingerprint, event["url"]
        try:
            published_date = filing_date(event.get("published_at"))
        except ValueError:
            published_date = None
        published = None
        precision = "date" if published_date else "unknown"
        basis = "sec_submissions_filing_date" if published_date else "unverified"
    facts = m.NewsFacts(family=family, headline=event["headline"], summary=normalize_summary(event.get("summary")),
                        source=_text(event.get("source")), publisher=_text(event.get("publisher")),
                        canonical_url=canonical, published_at=published, publication_date=published_date,
                        timestamp_precision=precision, publication_basis=basis, sec_form=sec_form,
                        accession_number=accession)
    return m.NewsEvidenceItem(identity_version=identity_version, event_key=event_key, facts=facts,
                              relevance=_relevance(event), upstream_score=_score(event),
                              delivery=m.NewsDelivery(alert_decision=_text(event.get("alert_decision")),
                                                      collector_outcome=news_input.collector_outcome),
                              observed_at=observed)
