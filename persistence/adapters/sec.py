"""Whitelist SEC filing facts and already-computed outcomes; never score, enrich or infer IDs.

Runtime identity stays the collector's Redis fingerprint (headline|url), supplied
as ``sec_fingerprint``; it is deterministic, so the durable key survives Redis
TTL expiry and restarts. The accession number is the strongest native filing
fact: it is validated against the normalized URL and recorded as provenance.
"""
from datetime import date
import re

from persistence.adapters.macro import digest, select, source_order_promotion

EVENT_TYPE = "sec_filing"
# Parser-owned facts copied verbatim. CIK, report date, item codes, amendment
# relations and filing categories are not exposed by the normalizer and are never invented.
FACTS = ("sec_form", "accession_number", "symbols", "direct_symbols", "related_symbols", "relevant")
SCORE = ("impact_score", "impact_level", "quality_adjustment", "score_reasons")
DECISION = ("alert_decision",)
IDENTITY_FACTS = ("sec_form", "accession_number")
FINGERPRINT_CHARS = set("0123456789abcdef")
ACCESSION = re.compile(r"\d{10}-\d{2}-\d{6}")
# Exactly the archive URL shape built by collector/sec_normalizer.py. The primary
# document may contain a path (insider forms: "xslF345X05/form4.xml") or be empty.
ARCHIVE_URL = re.compile(r"https://www\.sec\.gov/Archives/edgar/data/\d+/(\d{18})/(.*)")


def filing_document(event):
    """Return (accession, primary_document or None); fail closed if URL and accession disagree."""
    accession, url = event.get("accession_number"), event.get("url")
    match = ARCHIVE_URL.fullmatch(url) if isinstance(url, str) else None
    if not isinstance(accession, str) or not ACCESSION.fullmatch(accession) or match is None:
        raise ValueError("SEC accession number and archive URL required")
    if match.group(1) != accession.replace("-", ""):
        raise ValueError("SEC archive URL does not match accession number")
    return accession, match.group(2) or None


def filing_date(value):
    """The submissions API gives a filing date only; never upgraded to a time."""
    if value in (None, ""):
        return None
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("SEC filing date must be YYYY-MM-DD")
    return date.fromisoformat(value)


def adapt_sec(event, observed_at, *, make_current=True):
    """Version content hash is separate from the authoritative collector fingerprint."""
    fingerprint = event.get("sec_fingerprint")
    if (event.get("event_type") != EVENT_TYPE or not isinstance(fingerprint, str) or len(fingerprint) != 64
            or not set(fingerprint) <= FINGERPRINT_CHARS or not event.get("headline") or not event.get("sec_form")):
        raise ValueError("Normalized SEC event with collector fingerprint required")
    accession, primary_document = filing_document(event)
    filed = filing_date(event.get("published_at"))
    attributes = select(event, FACTS)
    attributes["primary_document"] = primary_document
    normalized = dict(
        schema_version=1, normalizer_version="sec-shadow-v1",
        headline=event["headline"], summary=event.get("summary") or "",
        source_name=event.get("source") or "SEC EDGAR", publisher=event.get("publisher") or "SEC",
        canonical_url=event["url"], event_type=EVENT_TYPE, market_scope=event.get("market_scope"),
        published_at=None, publication_date=filed,
        timestamp_precision="date" if filed else "unknown",
        publication_basis="sec_submissions_filing_date" if filed else "unverified",
        stage=None, revision_key=None, attributes=attributes,
    )
    histories = []
    if "impact_score" in event:
        histories.append(("score", select(event, SCORE)))
    if "alert_decision" in event:
        histories.append(("decision", dict(select(event, DECISION), score_snapshot=select(event, SCORE))))
    # SEC has no AI path: no AI history is ever written.
    attrs = dict(role="sec_filing", publisher=normalized["publisher"], accession_number=accession,
                 sec_form=event["sec_form"], filing_date=event.get("published_at") or None,
                 primary_document=primary_document, timestamp_precision=normalized["timestamp_precision"],
                 publication_basis=normalized["publication_basis"], retrieval_basis="shadow_observation")
    values = dict(source_name=normalized["source_name"], canonical_url=event["url"], document_id=accession,
                  attributes=attrs)
    key = digest(values)
    values["retrieved_at"] = observed_at
    return dict(record=dict(source_family="sec", event_key=fingerprint, identity_version="sec-v1",
                            version_key="sec-content-v1:" + digest(normalized),
                            normalized=normalized, observed_at=observed_at, make_current=make_current),
                provenance=[(key, values)], histories=histories)


def sec_promotion(current, candidate):
    """Filings are immutable: one fingerprint normally has exactly one version.

    Only a changed normalizer output for the same collector identity can add a
    version; the shared source-order contract then holds it (same filing date is
    ``ambiguous``). A changed form or accession is never merged.
    """
    def material(value):
        attrs = value["attributes"]
        return dict(summary=" ".join(value["summary"].split()),
                    facts={key: attrs[key] for key in (*FACTS, "primary_document") if key in attrs},
                    event_type=value["event_type"], market_scope=value["market_scope"],
                    stage=value["stage"], revision_key=value["revision_key"])

    return source_order_promotion(current, candidate, IDENTITY_FACTS, material)
