"""Pure normalization of official policy documents; no clients or configuration."""

import hashlib
import json
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

HOSTS = {
    "fr": {"www.federalregister.gov", "public-inspection.federalregister.gov", "www.govinfo.gov"},
    "bis": {"www.bis.gov"}, "ofac": {"ofac.treasury.gov"},
    "treasury": {"home.treasury.gov"}, "ustr": {"ustr.gov"},
    "whitehouse": {"www.whitehouse.gov"}, "ftc": {"www.ftc.gov"},
    "moea": {"www.moea.gov.tw"},
}
PUBLISHERS = {"fr": "Federal Register", "bis": "Bureau of Industry and Security",
              "ofac": "Office of Foreign Assets Control", "treasury": "U.S. Treasury",
              "ustr": "U.S. Trade Representative", "whitehouse": "The White House",
              "ftc": "Federal Trade Commission", "moea": "Taiwan Ministry of Economic Affairs"}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def official_url(url, agency=None):
    p = urlsplit(url)
    hosts = HOSTS[agency] if agency else set().union(*HOSTS.values())
    if p.scheme != "https" or p.hostname not in hosts or p.username or p.password or p.port not in (None, 443):
        raise ValueError("Unapproved official URL")
    # Preserve meaningful query parameters (MOEA article IDs, FR cursors).
    query = [(k, v) for k, values in parse_qs(p.query).items()
             if not k.startswith("utm_") for v in values]
    return urlunsplit(("https", p.hostname, p.path, urlencode(sorted(query)), ""))


def publication(raw, agency):
    if not raw:
        return None, "unknown"
    raw = raw.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        zone = "Asia/Taipei" if agency == "moea" else "America/New_York"
        dt = datetime.fromisoformat(raw).replace(tzinfo=ZoneInfo(zone))
        return dt.astimezone(timezone.utc).isoformat(), "date"
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        dt = parsedate_to_datetime(raw)
    if dt.tzinfo is None:
        raise ValueError("Publication timezone missing")
    return dt.astimezone(timezone.utc).isoformat(), "second"


def normalize_document(document):
    agency = document["agency"]
    url = official_url(document["url"], agency)
    title, body = document["headline"].strip(), document["body"].strip()
    if not title or len(body) < 80 or len(body) > 500_000:
        raise ValueError("Official document content unavailable")
    stamp, precision = publication(document.get("published_at"), agency)
    native = str(document.get("native_id") or url)
    event = dict(source=PUBLISHERS[agency], publisher=PUBLISHERS[agency], agency=agency,
                 headline=title, url=url, summary=body[:1800], body=body,
                 published_at=stamp, original_published_at=stamp, timestamp_precision=precision,
                 publication_basis=document.get("publication_basis", "source") if stamp else "unverified",
                 effective_at=document.get("effective_at"), scheduled_publication=document.get("scheduled_publication"),
                 document_id=f"{agency}:{native}", policy_id=None, event_id=None,
                 revision_id="original", policy_stage="unknown", legal_status="unclassified",
                 policy_action=None, event_type="policy_action", geopolitical_category="routine",
                 symbols=[], direct_symbols=[], related_symbols=[], relevant=False,
                 market_scope="Technology / government policy", relevance_reasons=[], evidence=[],
                 matched_entities=[], matched_products=[], matched_jurisdictions=[], policy_scope=[],
                 legal_references=list(document.get("legal_references", [])),
                 identity_anchors=list(document.get("identity_anchors", [])),
                 document_type=document.get("document_type"), metrics={})
    # FR document numbers are stable across public inspection and publication.
    if agency == "fr":
        if not re.fullmatch(r"\d{4}-\d{4,6}", native):
            raise ValueError("Invalid Federal Register document number")
        event["identity_anchors"].append("fr:" + native)
    if agency == "ofac" and re.fullmatch(r"\d{8}(?:_\d+)?", native):
        event["identity_anchors"].append("ofac:" + native)
    event["identity_anchors"] = sorted(set(event["identity_anchors"]))
    event["provenance"] = [{"document_id": event["document_id"], "agency": agency, "url": url,
                            "published_at": stamp, "content_sha256": digest(body)}]
    return event
