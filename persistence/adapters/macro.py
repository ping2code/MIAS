"""Whitelist parser facts and already-computed outcomes; never score or infer IDs."""
from copy import deepcopy
from datetime import datetime
from hashlib import sha256
from zoneinfo import ZoneInfo

from persistence.repository import _canonical

FACTS = (
    "agency", "release_category", "reference_period", "release_stage", "release_id",
    "revision_id", "original_published_at", "effective_at", "metrics", "data_source_url", "release_feed_url",
    "symbols", "direct_symbols", "related_symbols", "relevant", "relevance_rule",
    "relevance_reason", "relevance_evidence", "source_id", "native_id",
)
SCORE = ("impact_score", "original_impact_score", "impact_level", "quality_adjustment",
         "score_reasons", "scoring_engine", "scoring_version")
DECISION = ("alert_decision", "decision_engine", "decision_version", "decision_reason", "decision_context")
AI = ("ai_summary", "ai_sentiment", "ai_confidence", "ai_why_it_matters", "ai_event_type")


def select(event, fields):
    return deepcopy({key: event[key] for key in fields if key in event})


def instant(value):
    if value is None:
        return None
    result = datetime.fromisoformat(value) if isinstance(value, str) else value
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("Aware macro timestamp required")
    return result


def digest(value):
    return sha256(_canonical(value).encode()).hexdigest()


def validated_ai(event):
    """Already-validated enrichment only; never requests or repairs AI output."""
    if not all(key in event for key in AI):
        return None
    valid = (event["ai_sentiment"] in {"STRONGLY_BULLISH", "BULLISH", "NEUTRAL", "BEARISH", "STRONGLY_BEARISH"}
             and type(event["ai_confidence"]) is int and 0 <= event["ai_confidence"] <= 100
             and all(isinstance(event[k], str) and event[k].strip() for k in AI if k != "ai_confidence"))
    return select(event, (*AI, "ai_provider", "ai_model")) if valid else None


def adapt_macro(event, observed_at, *, make_current=True):
    """Version content hash is separate from the authoritative collector event_id."""
    if event.get("event_type") != "macro_release" or not event.get("event_id"):
        raise ValueError("Normalized macro identity required")
    published = instant(event.get("published_at"))
    precision = event.get("timestamp_precision", "unknown")
    publication_date = None
    if precision == "date" and published is not None:
        # HTML normalizer encodes date-only Eastern midnight as a UTC instant.
        publication_date = published.astimezone(ZoneInfo("America/New_York")).date()
        published = None
    normalized = dict(
        schema_version=1, normalizer_version="macro-shadow-v1",
        headline=event["headline"], summary=event["summary"],
        source_name=event["source"], publisher=event["publisher"],
        canonical_url=event.get("url"), event_type=event["event_type"],
        market_scope=event.get("market_scope"), published_at=published,
        publication_date=publication_date, timestamp_precision=precision,
        publication_basis=event.get("publication_basis", "unspecified"),
        stage=event.get("release_stage"), revision_key=event.get("revision_id"),
        attributes=select(event, FACTS),
    )
    histories = []
    if "impact_score" in event:
        histories.append(("score", select(event, SCORE)))
    if "alert_decision" in event:
        histories.append(("decision", dict(select(event, DECISION), score_snapshot=select(event, SCORE))))
    ai = validated_ai(event)
    if ai is not None:
        histories.append(("ai", ai))
    provenance = []
    for field in ("url", "data_source_url", "release_feed_url"):
        if not event.get(field):
            continue
        attrs = select(event, ("publisher", "published_at", "timestamp_precision", "publication_basis",
                               "release_id", "revision_id", "source_id", "native_id"))
        attrs["role"] = {"url": "release", "data_source_url": "structured_data", "release_feed_url": "feed"}[field]
        values = dict(source_name=event["source"], canonical_url=event[field],
                      document_id=event.get("native_id", event.get("release_id")), attributes=attrs)
        if field == "url" and event.get("source_hash"):
            values["content_hash"] = event["source_hash"]
        # Missing fetch time is explicitly marked; observation time is not publication time.
        attrs["retrieval_basis"] = "fetched_at" if event.get("fetched_at") else "shadow_observation"
        key = digest(values)
        values["retrieved_at"] = instant(event.get("fetched_at")) or observed_at
        provenance.append((key, values))
    return dict(record=dict(source_family="macro", event_key=event["event_id"],
                            identity_version="macro-v1", version_key="macro-content-v1:" + digest(normalized),
                            normalized=normalized, observed_at=observed_at, make_current=make_current),
                provenance=provenance, histories=histories)


def source_order_promotion(current, candidate, identity_fields, material):
    """Shared conservative ordering, evaluated under the repository's event lock.

    Observation/recording/fetch times, content hashes and numeric metric direction
    are never ordering evidence. Unknown order retains the current pointer.
    """
    if any(current["attributes"].get(key) != candidate["attributes"].get(key) for key in identity_fields):
        return False, "ambiguous"
    if _canonical(material(current)) == _canonical(material(candidate)):
        return False, "cosmetic"
    precision = candidate["timestamp_precision"]
    if precision != current["timestamp_precision"]:
        return False, "ambiguous"
    if precision in {"minute", "second"}:
        before, after = current["published_at"], candidate["published_at"]
    elif precision == "date":
        before, after = current["publication_date"], candidate["publication_date"]
    else:
        return False, "ambiguous"
    if before is None or after is None or after == before:
        return False, "ambiguous"
    if after < before:
        return False, "older"
    return True, "newer_material"


def macro_promotion(current, candidate):
    identity_fields = ("agency", "release_category", "reference_period", "release_id", "release_stage")

    def material(value):
        attrs = value["attributes"]
        facts = {key: attrs[key] for key in FACTS if key in attrs and key not in {
            "original_published_at", "data_source_url", "release_feed_url", "source_id", "native_id"}}
        # Prose is the only parser-owned release content for current BEA/Census
        # events. Whitespace-only edits and title/URL changes are cosmetic.
        return dict(summary=" ".join(value["summary"].split()), facts=facts,
                    event_type=value["event_type"], market_scope=value["market_scope"],
                    stage=value["stage"], revision_key=value["revision_key"])

    return source_order_promotion(current, candidate, identity_fields, material)
