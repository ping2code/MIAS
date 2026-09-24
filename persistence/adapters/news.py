"""Whitelist News/RSS article facts and already-computed outcomes; never score, enrich, fetch or infer IDs.

Durable identity (``news-url-v1``) is the URL half of the collector's own exact
dedup fingerprint (``headline|url``), normalized exactly as the collector does it
(``strip().lower()``). The same URL is therefore one durable article even if its
headline changes; a different URL is always a different article. Near-duplicate
headline similarity, publisher, feed, AI output and story similarity never take
part in identity. Items without an http(s) URL (the normalizer emits ``"N/A"``)
fall back to the collector's full fingerprint (``news-fingerprint-v1``) so they
can never collide on a placeholder.
"""
from datetime import datetime
from hashlib import sha256

from persistence.adapters.macro import digest, select, source_order_promotion, validated_ai

URL_IDENTITY, FINGERPRINT_IDENTITY = "news-url-v1", "news-fingerprint-v1"
# Parser/relevance-owned facts copied verbatim. Source quality is recorded only as
# the collector wrote it (inside score_reasons); no separate value is derived.
FACTS = ("symbols", "direct_symbols", "related_symbols", "relevant")
SCORE = ("impact_score", "original_impact_score", "impact_level", "quality_adjustment", "score_reasons")
DECISION = ("alert_decision",)
PROCESSED, NEAR_DUPLICATE = "processed", "near_duplicate_suppressed"
FINGERPRINT_CHARS = set("0123456789abcdef")


def article_url(event):
    """The collector-normalized URL, or None when the item has no usable http(s) link."""
    url = event.get("url")
    if not isinstance(url, str):
        return None
    normalized = url.strip().lower()
    return normalized if normalized.startswith(("http://", "https://")) and "." in normalized else None


def article_identity(event):
    url = article_url(event)
    if url is not None:
        return URL_IDENTITY, sha256(f"{URL_IDENTITY}|{url}".encode()).hexdigest()
    return FINGERPRINT_IDENTITY, event["news_fingerprint"]


def publication(value):
    """(published_at, precision, basis, raw): RSS dates carry seconds; anything else is kept raw, never guessed."""
    if not value:
        return None, "unknown", "no_feed_date", None
    try:
        parsed = datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        parsed = None
    if parsed is None or parsed.tzinfo is None or parsed.utcoffset() is None:
        return None, "unknown", "unparsed_feed_date", str(value)
    return parsed, "second", "rss_published", None


def adapt_news(event, observed_at, *, make_current=True):
    """Version content hash is separate from the durable article identity."""
    fingerprint, outcome = event.get("news_fingerprint"), event.get("news_collector_outcome")
    if (not isinstance(fingerprint, str) or len(fingerprint) != 64 or not set(fingerprint) <= FINGERPRINT_CHARS
            or event.get("relevant") is not True or not isinstance(event.get("headline"), str)
            or outcome not in (PROCESSED, NEAR_DUPLICATE)):
        raise ValueError("Relevant normalized news event with collector fingerprint and outcome required")
    scored = "impact_score" in event and "alert_decision" in event
    if scored != (outcome == PROCESSED):
        raise ValueError("Processed news events carry a score and decision; suppressed ones never do")
    identity_version, event_key = article_identity(event)
    published, precision, basis, raw = publication(event.get("published_at"))
    publisher = event.get("publisher") or "Unknown"
    attributes = select(event, FACTS)
    if raw is not None:
        attributes["published_at_raw"] = raw
    normalized = dict(
        schema_version=1, normalizer_version="news-shadow-v1",
        headline=event["headline"], summary=event.get("summary") or "",
        source_name=publisher, publisher=publisher, canonical_url=event["url"] if article_url(event) else None,
        event_type=event.get("event_type"), market_scope=event.get("market_scope"),
        published_at=published, publication_date=None, timestamp_precision=precision, publication_basis=basis,
        stage=None, revision_key=None, attributes=attributes,
    )
    histories = []
    if outcome == PROCESSED:
        histories.append(("score", select(event, SCORE)))
        histories.append(("decision", dict(select(event, DECISION), collector_outcome=PROCESSED,
                                           score_snapshot=select(event, SCORE))))
        ai = validated_ai(event)  # Already-performed, validated enrichment only; never requested here.
        if ai is not None:
            histories.append(("ai", ai))
    else:
        # Suppressed before scoring: the collector exposes the outcome, not the similarity value.
        histories.append(("decision", dict(collector_outcome=NEAR_DUPLICATE)))
    attrs = dict(role="news_rss_item", feed=event.get("source"), publisher=publisher, collector_fingerprint=fingerprint,
                 identity=identity_version, published_at=event.get("published_at"), timestamp_precision=precision,
                 publication_basis=basis, retrieval_basis="shadow_observation")
    values = dict(source_name=event.get("source") or "Unknown Feed", canonical_url=str(event.get("url")),
                  document_id=None, attributes=attrs)
    key = digest(values)
    values["retrieved_at"] = observed_at
    return dict(record=dict(source_family="news", event_key=event_key, identity_version=identity_version,
                            version_key="news-content-v1:" + digest(normalized),
                            normalized=normalized, observed_at=observed_at, make_current=make_current),
                provenance=[(key, values)], histories=histories)


def news_promotion(current, candidate):
    """Shared source-order contract; never promotes on arrival order.

    A changed headline or summary for the same URL is a new immutable version;
    it becomes current only with a strictly later comparable publication time.
    Equal or unknown times keep the current version (``ambiguous``);
    whitespace-only edits are ``cosmetic``.
    """
    def material(value):
        attrs = value["attributes"]
        return dict(headline=" ".join(value["headline"].split()), summary=" ".join(value["summary"].split()),
                    publisher=value["publisher"], facts={key: attrs[key] for key in FACTS if key in attrs},
                    event_type=value["event_type"], market_scope=value["market_scope"])

    return source_order_promotion(current, candidate, (), material)
