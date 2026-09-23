"""Whitelist Treasury parser facts and already-computed outcomes; never score or infer IDs."""
from zoneinfo import ZoneInfo

from persistence.adapters.macro import digest, instant, select, source_order_promotion, validated_ai

EVENT_TYPES = {"treasury_release", "treasury_yield_observation"}
# Parser-owned facts. The collector's event_id stays authoritative; these fields
# are copied verbatim and never recomputed or filled in.
FACTS = (
    "agency", "treasury_category", "release_id", "release_stage", "revision_id",
    "reference_period", "original_published_at", "effective_at", "metrics",
    "symbols", "direct_symbols", "related_symbols", "relevant",
    # Auctions (TreasuryDirect securities record).
    "cusip", "auction_date", "security_type", "security_term", "reopening", "source_document",
    # Daily yield observations; observation date is not publication time.
    "observation_date",
)
SCORE = ("impact_score", "original_impact_score", "impact_level", "quality_adjustment",
         "score_reasons", "scoring_engine", "scoring_version")
# Collector decision context: the pre-freshness decision and alert eligibility.
DECISION = ("alert_decision", "initial_decision", "alert_eligible",
            "decision_engine", "decision_version", "decision_reason", "decision_context")
IDENTITY_FACTS = ("agency", "treasury_category", "release_id", "release_stage", "revision_id",
                  "reference_period", "cusip", "auction_date", "security_type",
                  "observation_date", "dataset")
# Document naming and first-publication echo are evidence, not release content.
NON_MATERIAL = {"original_published_at", "source_document"}


def _role(event):
    if event["event_type"] == "treasury_yield_observation":
        return "yield_dataset"
    if event.get("cusip"):
        return "auction_record"
    return "debt_limit_letter" if event.get("release_stage") == "letter" else "release"


def adapt_treasury(event, observed_at, *, make_current=True):
    """Version content hash is separate from the authoritative collector event_id."""
    if (event.get("event_type") not in EVENT_TYPES or event.get("agency") != "treasury"
            or not event.get("event_id")):
        raise ValueError("Normalized Treasury identity required")
    published = instant(event.get("published_at"))
    precision = event.get("timestamp_precision", "unknown")
    publication_date = None
    if precision == "date" and published is not None:
        # Auction and letter dates are encoded as Eastern midnight UTC instants.
        publication_date = published.astimezone(ZoneInfo("America/New_York")).date()
        published = None
    attributes = select(event, FACTS)
    release_id = event.get("release_id") or ""
    if event["event_type"] == "treasury_yield_observation" and release_id.startswith("yield:"):
        # Dataset is the collector-owned release_id component, not a new identity.
        attributes["dataset"] = release_id.split(":")[1]
    normalized = dict(
        schema_version=1, normalizer_version="treasury-shadow-v1",
        headline=event["headline"], summary=event["summary"],
        source_name=event["source"], publisher=event["publisher"],
        canonical_url=event.get("url"), event_type=event["event_type"],
        market_scope=event.get("market_scope"), published_at=published,
        publication_date=publication_date, timestamp_precision=precision,
        publication_basis=event.get("publication_basis", "unspecified"),
        stage=event.get("release_stage"), revision_key=event.get("revision_id"),
        attributes=attributes,
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
    if event.get("url"):
        attrs = select(event, ("publisher", "published_at", "timestamp_precision", "publication_basis",
                               "treasury_category", "release_id", "release_stage", "revision_id",
                               "cusip", "auction_date", "source_document", "observation_date"))
        attrs["role"] = _role(event)
        if "dataset" in attributes:
            attrs["dataset"] = attributes["dataset"]
        # Missing fetch time is explicitly marked; observation time is not publication time.
        attrs["retrieval_basis"] = "fetched_at" if event.get("fetched_at") else "shadow_observation"
        values = dict(source_name=event["source"], canonical_url=event["url"],
                      document_id=event.get("source_document") or event.get("release_id"),
                      attributes=attrs)
        if event.get("source_hash"):
            values["content_hash"] = event["source_hash"]
        key = digest(values)
        values["retrieved_at"] = instant(event.get("fetched_at")) or observed_at
        provenance.append((key, values))
    return dict(record=dict(source_family="treasury", event_key=event["event_id"],
                            identity_version="treasury-v1", version_key="treasury-content-v1:" + digest(normalized),
                            normalized=normalized, observed_at=observed_at, make_current=make_current),
                provenance=provenance, histories=histories)


def treasury_promotion(current, candidate):
    """Phase 2D source-ordering contract applied to Treasury facts.

    Yield observations have no verified publication time (precision unknown), so a
    changed observation is always retained but never promoted. Auction stages and
    corrected releases already have distinct collector identities.
    """
    def material(value):
        attrs = value["attributes"]
        facts = {key: attrs[key] for key in (*FACTS, "dataset") if key in attrs and key not in NON_MATERIAL}
        # Whitespace-only edits and headline/URL changes are cosmetic.
        return dict(summary=" ".join(value["summary"].split()), facts=facts,
                    event_type=value["event_type"], market_scope=value["market_scope"],
                    stage=value["stage"], revision_key=value["revision_key"])

    return source_order_promotion(current, candidate, IDENTITY_FACTS, material)
