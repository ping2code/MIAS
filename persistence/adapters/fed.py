"""Whitelist Federal Reserve parser facts and already-computed outcomes; never score or infer IDs."""
from persistence.adapters.macro import digest, instant, select, source_order_promotion, validated_ai

EVENT_TYPE = "fed_policy"
# Parser-owned facts copied verbatim. Identity is the collector's own Redis
# fingerprint (headline|url), supplied by the collector as ``fed_fingerprint``.
FACTS = ("fed_category", "symbols", "direct_symbols", "related_symbols", "relevant", "fed_source_feed")
SCORE = ("impact_score", "original_impact_score", "impact_level", "quality_adjustment",
         "score_reasons", "scoring_engine", "scoring_version")
DECISION = ("alert_decision", "decision_engine", "decision_version", "decision_reason", "decision_context")
IDENTITY_FACTS = ("fed_category",)
FINGERPRINT_CHARS = set("0123456789abcdef")


def adapt_fed(event, observed_at, *, make_current=True):
    """Version content hash is separate from the authoritative collector fingerprint."""
    fingerprint = event.get("fed_fingerprint")
    if (event.get("event_type") != EVENT_TYPE or not isinstance(fingerprint, str) or len(fingerprint) != 64
            or not set(fingerprint) <= FINGERPRINT_CHARS or not event.get("url")):
        raise ValueError("Normalized Fed event with collector fingerprint required")
    published = instant(event.get("published_at"))
    attributes = select(event, FACTS)
    # The normalizer keeps a parsed feed timestamp or nothing; precision is
    # labelled as derived rather than claimed by the source.
    attributes["timestamp_precision_basis"] = "parsed_feed_timestamp" if published else "no_valid_feed_date"
    normalized = dict(
        schema_version=1, normalizer_version="fed-shadow-v1",
        headline=event["headline"], summary=event.get("summary") or "",
        source_name=event.get("source") or "Federal Reserve", publisher=event.get("publisher") or "Federal Reserve",
        canonical_url=event["url"], event_type=EVENT_TYPE, market_scope=event.get("market_scope"),
        published_at=published, publication_date=None,
        timestamp_precision="second" if published else "unknown",
        publication_basis="fed_rss_published_or_updated" if published else "unverified",
        stage=event.get("fed_category"), revision_key=None, attributes=attributes,
    )
    histories = []
    if "impact_score" in event:
        histories.append(("score", select(event, SCORE)))
    if "alert_decision" in event:
        histories.append(("decision", dict(select(event, DECISION), score_snapshot=select(event, SCORE))))
    ai = validated_ai(event)
    if ai is not None:
        histories.append(("ai", ai))
    attrs = dict(role="fed_monetary_policy_release", fed_category=event.get("fed_category"),
                 published_at=event.get("published_at"), timestamp_precision=normalized["timestamp_precision"],
                 publication_basis=normalized["publication_basis"],
                 retrieval_basis="fetched_at" if event.get("fetched_at") else "shadow_observation")
    if event.get("fed_source_feed"):
        attrs["feed_url"] = event["fed_source_feed"]
    values = dict(source_name=normalized["source_name"], canonical_url=event["url"], document_id=None,
                  attributes=attrs)
    if event.get("source_hash"):
        values["content_hash"] = event["source_hash"]
    key = digest(values)
    values["retrieved_at"] = instant(event.get("fetched_at")) or observed_at
    return dict(record=dict(source_family="fed", event_key=fingerprint, identity_version="fed-v1",
                            version_key="fed-content-v1:" + digest(normalized),
                            normalized=normalized, observed_at=observed_at, make_current=make_current),
                provenance=[(key, values)], histories=histories)


def fed_promotion(current, candidate):
    """Phase 2D source-ordering contract applied to Fed releases.

    Headline/URL changes are new collector identities, so versions of one event
    differ only in prose/facts. A changed category is ambiguous, never merged.
    """
    def material(value):
        attrs = value["attributes"]
        facts = {key: attrs[key] for key in FACTS if key in attrs}
        return dict(summary=" ".join(value["summary"].split()), facts=facts,
                    event_type=value["event_type"], market_scope=value["market_scope"],
                    stage=value["stage"], revision_key=value["revision_key"])

    return source_order_promotion(current, candidate, IDENTITY_FACTS, material)
