"""Whitelist geopolitical parser facts and already-computed outcomes; never score, resolve or infer IDs."""
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from collector.geopolitical_normalizer import PUBLISHERS
from persistence.adapters.macro import digest, instant, select, source_order_promotion, validated_ai
from persistence.repository import _canonical

EVENT_TYPES = {"policy_action", "sanctions_action", "trade_action", "regulatory_action", "operational_disruption"}
# Parser/relevance/identity facts copied verbatim. The collector-resolved
# event_id/policy_id are authoritative; Redis alias state is never consulted.
FACTS = (
    "agency", "document_id", "document_type", "policy_id", "identity_status", "identity_anchors",
    "geopolitical_category", "policy_action", "policy_stage", "legal_status", "revision_id",
    "original_published_at", "effective_at", "scheduled_publication", "legal_references",
    "symbols", "direct_symbols", "related_symbols", "relevant", "relevance_reasons", "evidence",
    "matched_entities", "matched_products", "matched_jurisdictions", "policy_scope", "metrics",
)
RELEVANCE = ("symbols", "direct_symbols", "related_symbols", "relevant", "relevance_reasons", "evidence",
             "matched_entities", "matched_products", "matched_jurisdictions", "policy_scope")
SCORE = ("impact_score", "original_impact_score", "impact_level", "quality_adjustment",
         "score_reasons", "scoring_engine", "scoring_version")
DECISION = ("alert_decision", "initial_decision", "decision_engine", "decision_version",
            "decision_reason", "decision_context")
IDENTITY_FACTS = ("policy_id", "identity_status", "policy_stage", "legal_status", "revision_id",
                  "geopolitical_category", "policy_action")
# Evidence of first publication; the analyzed document is fixed by the collector cache.
NON_MATERIAL = {"original_published_at"}


def _zone(agency):
    return ZoneInfo("Asia/Taipei" if agency == "moea" else "America/New_York")


def _edition(entry):
    """Federal Register public-inspection vs published edition, from the official URL only."""
    if entry.get("agency") != "fr":
        return None
    parts = urlsplit(entry.get("url") or "")
    if parts.hostname == "public-inspection.federalregister.gov" or parts.path.startswith("/public-inspection/"):
        return "public_inspection"
    return "published"


def adapt_geopolitical(event, observed_at, *, make_current=True):
    """Version content hash is separate from the authoritative collector event_id."""
    if (event.get("event_type") not in EVENT_TYPES or not event.get("event_id") or not event.get("policy_id")
            or event.get("identity_status") != "resolved"):
        # Unresolved/withheld identities are never assigned an identity by persistence.
        raise ValueError("Resolved geopolitical identity required")
    published = instant(event.get("published_at"))
    precision = event.get("timestamp_precision", "unknown")
    publication_date = None
    if precision == "unknown" and published is not None:
        raise ValueError("Unverifiable geopolitical publication precision")
    if precision == "date" and published is not None:
        # Date-only sources are encoded as source-local midnight UTC instants.
        publication_date = published.astimezone(_zone(event.get("agency"))).date()
        published = None
    normalized = dict(
        schema_version=1, normalizer_version="geopolitical-shadow-v1",
        headline=event["headline"], summary=event["summary"],
        source_name=event["source"], publisher=event["publisher"],
        canonical_url=event.get("url"), event_type=event["event_type"],
        market_scope=event.get("market_scope"), published_at=published,
        publication_date=publication_date, timestamp_precision=precision,
        publication_basis=event.get("publication_basis", "unspecified"),
        stage=event.get("policy_stage"), revision_key=event.get("revision_id"),
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
    # Each official document known for the resolved action (companions, public
    # inspection/published editions) is durable evidence of document -> policy.
    for entry in event.get("provenance") or []:
        if not entry.get("url") or not entry.get("document_id"):
            continue
        attrs = dict(relation="policy_document", policy_id=event["policy_id"], event_id=event["event_id"],
                     document_id=entry["document_id"], agency=entry.get("agency"),
                     published_at=entry.get("published_at"),
                     analyzed_document=entry["document_id"] == event.get("document_id"))
        edition = _edition(entry)
        if edition:
            attrs["fr_edition"] = edition
        attrs["retrieval_basis"] = "fetched_at" if entry.get("fetched_at") else "shadow_observation"
        values = dict(source_name=PUBLISHERS.get(entry.get("agency"), event["source"]),
                      canonical_url=entry["url"], document_id=entry["document_id"], attributes=attrs)
        if entry.get("content_sha256"):
            values["content_hash"] = entry["content_sha256"]
        key = digest(values)
        values["retrieved_at"] = instant(entry.get("fetched_at")) or observed_at
        provenance.append((key, values))
    return dict(record=dict(source_family="geopolitical", event_key=event["event_id"],
                            identity_version="geopolitical-v1",
                            version_key="geopolitical-content-v1:" + digest(normalized),
                            normalized=normalized, observed_at=observed_at, make_current=make_current),
                provenance=provenance, histories=histories)


def content_material(value):
    """Substantive content of one analyzed document's version, excluding disclosure time.

    Parser-owned facts (minus first-publication evidence), whitespace-normalized
    summary, family, scope, stage and revision. Headline/URL are presentation.
    """
    attrs = value["attributes"]
    facts = {key: attrs[key] for key in FACTS if key in attrs and key not in NON_MATERIAL}
    return dict(summary=" ".join(value["summary"].split()), facts=facts,
                event_type=value["event_type"], market_scope=value["market_scope"],
                stage=value["stage"], revision_key=value["revision_key"])


# Action-level substance shared by every official document of one resolved action.
# Document-level facts (agency, document ID/type, body/summary, quotes, anchors,
# legal references, scheduled/first publication) are the analyzed document's
# presentation, not the action.
ACTION_FACTS = ("policy_id", "identity_status", "geopolitical_category", "policy_action", "policy_stage",
                "legal_status", "revision_id", "effective_at", "policy_scope", "metrics", "symbols",
                "direct_symbols", "related_symbols", "relevant", "relevance_reasons", "matched_entities",
                "matched_products", "matched_jurisdictions")


def action_material(value):
    attrs = value["attributes"]
    return dict(facts={key: attrs[key] for key in ACTION_FACTS if key in attrs}, event_type=value["event_type"],
                market_scope=value["market_scope"], stage=value["stage"], revision_key=value["revision_key"])


def same_document(first, second):
    return first["attributes"].get("document_id") == second["attributes"].get("document_id")


def substantively_equal(first, second):
    """Same analyzed document: full content; different companion documents: action-level substance."""
    view = content_material if same_document(first, second) else action_material
    return _canonical(view(first)) == _canonical(view(second))


def disclosure(value):
    """Comparable disclosure instant/date for a version, or None when not comparable."""
    precision = value["timestamp_precision"]
    if precision in {"minute", "second"}:
        return precision, value["published_at"]
    if precision == "date":
        return precision, value["publication_date"]
    return precision, None


def geopolitical_promotion(current, candidate):
    """Phase 2D source ordering for resolved policy actions, with disclosure-only handling (Phase 2N).

    Stages, families and revisions are part of the collector identity, so a
    proposal/final/amendment never competes here. Disclosure time is not content:

    - substantively equal (see ``substantively_equal``), identical disclosure: ``cosmetic``;
    - substantively equal, later disclosure: ``disclosure_only`` (held; a later
      companion/re-analysis never replaces current by timestamp alone);
    - substantively equal, strictly earlier comparable disclosure:
      ``earlier_disclosure`` (promoted: the earliest known disclosure wins, so
      every observation order converges on the same current version);
    - substantively equal, incomparable/missing disclosure: ``ambiguous``;
    - different companion documents that disagree on action-level facts:
      ``ambiguous`` (one document never overrides another by timestamp);
    - same document, changed content: the unchanged shared rules
      (``newer_material`` / ``older`` / ``ambiguous``).
    """
    if any(current["attributes"].get(key) != candidate["attributes"].get(key) for key in IDENTITY_FACTS):
        return False, "ambiguous"
    if substantively_equal(current, candidate):
        (before_precision, before), (after_precision, after) = disclosure(current), disclosure(candidate)
        if (before_precision, before) == (after_precision, after):
            return False, "cosmetic"
        if before_precision != after_precision or before is None or after is None:
            return False, "ambiguous"
        return (True, "earlier_disclosure") if after < before else (False, "disclosure_only")
    if not same_document(current, candidate):
        return False, "ambiguous"

    def material(value):
        return dict(content_material(value), disclosure=[value["published_at"], value["publication_date"]])

    return source_order_promotion(current, candidate, IDENTITY_FACTS, material)
