"""Pure Treasury release and auction normalization; no configuration or clients."""

import hashlib
import json
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from collector.macro_normalizer import Document, parse_publication


EASTERN = ZoneInfo("America/New_York")
PUBLISHER = "U.S. Department of the Treasury"
HOSTS = {"home.treasury.gov", "www.treasurydirect.gov"}


def official_url(url):
    parts = urlsplit(url)
    if (parts.scheme != "https" or parts.hostname not in HOSTS or parts.username
            or parts.password or parts.port not in (None, 443)):
        raise ValueError("Unexpected Treasury source URL")
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), parts.query, ""))


def publication(value):
    if not value:
        return None, "unknown"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:T00:00:00)?", value):
        date = datetime.strptime(value[:10], "%Y-%m-%d").replace(tzinfo=EASTERN)
        return date.astimezone(timezone.utc).isoformat(), "date"
    date = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if date.tzinfo is None:
        raise ValueError("Unverified Treasury timestamp timezone")
    return date.astimezone(timezone.utc).isoformat(), "second"


def treasury_identity(event):
    fields = [event[k] for k in ("agency", "release_id", "release_stage", "revision_id")]
    return hashlib.sha256(json.dumps([1, *fields], separators=(",", ":")).encode()).hexdigest()


def base_event(category, headline, url, release_id, stage, published=None, precision="unknown"):
    event = dict(source=PUBLISHER, publisher=PUBLISHER, agency="treasury",
                 headline=headline, url=official_url(url), summary="", published_at=published,
                 original_published_at=published, timestamp_precision=precision,
                 publication_basis="source" if published else "unverified",
                 treasury_category=category, release_id=release_id, release_stage=stage,
                 revision_id="original", reference_period=None, metrics={},
                 event_type="treasury_release", market_scope="US Treasury / rates",
                 symbols=[], direct_symbols=[], related_symbols=[], relevant=True)
    event["event_id"] = treasury_identity(event)
    return event


def classify_release(title, body):
    text = f"{title} {body}"
    # Advice and historical discussions are not adopted Treasury policy.
    if re.search(r"\b(?:TBAC|Borrowing Advisory Committee)\b", title, re.I):
        return "routine_release"
    if re.search(r"\b(?:remarks|historical perspectives|FAQ|description of)\b", title, re.I):
        return "routine_release"
    if re.search(r"quarterly refunding statement", title, re.I):
        return "quarterly_refunding"
    if re.search(r"(?:marketable borrowing estimates|borrowing estimates)", title, re.I):
        return "borrowing_estimates"
    if re.search(r"debt.limit|extraordinary measures", text, re.I):
        if re.search(r"(?:unable to (?:satisfy|meet|pay)|exhaust.{0,60}(?:cash|resources|measures)|(?:cash|resources).{0,60}exhaust)", body, re.I):
            return "debt_limit"
        if re.search(r"(?:begin|employ|initiat|extend|suspend|terminat|end).{0,100}extraordinary measures|extraordinary measures.{0,100}(?:begin|extend|exhaust|end)", body, re.I):
            return "extraordinary_measures"
    if (re.search(r"issuance|auction sizes|buyback|financing policy", text, re.I)
            and re.search(r"Treasury.{0,150}(?:will (?:increase|decrease|reduce|change|introduce)|is increasing|announces? (?:increased|new)|has decided)", text, re.I)):
        return "issuance_policy"
    if (re.search(r"Treasury.{0,120}(?:establishes|launches|activates)", text, re.I)
            and re.search(r"emergency (?:financing|liquidity|guarantee)|financial stability (?:facility|program)", text, re.I)):
        return "material_press_release"
    return "routine_release"


def normalize_release(html, url):
    url = official_url(url)
    if not re.fullmatch(r"/news/press-releases/[a-zA-Z0-9-]+", urlsplit(url).path):
        raise ValueError("Unexpected Treasury release path")
    root = Document(html).root
    title = next((n.attrs.get("content", "") for n in root.find("meta")
                  if n.attrs.get("property") == "og:title"), "")
    if not title:
        title = next((n.text() for n in root.find("h1") if "page-title" in n.attrs.get("class", "")), "")
    bodies = root.find(css_class="field--name-field-news-body")
    dates = root.find(css_class="field--name-field-news-publication-date")
    if not title or not bodies or len(bodies[0].text()) < 40:
        raise ValueError("Treasury release title/body missing")
    body = bodies[0].text()
    stamp = None
    if dates:
        times = dates[0].find("time")
        stamp = times[0].attrs.get("datetime") if times else None
    published, precision = publication(stamp)
    event = base_event(classify_release(title, body), title, url,
                       "press:" + urlsplit(url).path.rsplit("/", 1)[-1], "release", published, precision)
    event["summary"] = body[:12000]
    # Only an explicit correction date can version a reused release URL.
    corrections = []
    for node in bodies[0].find("p"):
        if re.match(r"^(?:Correction|Corrected on)\s*[:–-]", node.text(), re.I):
            corrected, cp = parse_publication(node.text())
            if corrected:
                corrections.append((corrected, cp))
    if corrections:
        corrected, cp = max(corrections)
        if not published or corrected < published:
            raise ValueError("Unverified correction chronology")
        event.update(published_at=corrected, timestamp_precision=cp, revision_id=corrected)
        event["event_id"] = treasury_identity(event)
    return event


def normalize_debt_letter(text, url):
    """Content and date must come from extracted document text, never link labels."""
    url = official_url(url)
    text = " ".join(text.split())
    if len(text) < 100 or not re.search(r"debt limit|extraordinary measures", text, re.I):
        raise ValueError("Debt-limit letter text unavailable")
    # Letterhead/date must be near the beginning, not a future date in the body.
    published, precision = parse_publication(text[:250])
    if not published:
        raise ValueError("Debt-limit letter date unavailable")
    title = "Treasury debt-limit letter — " + published[:10]
    event = base_event(classify_release(title, text), title, url,
                       "letter:" + urlsplit(url).path + ":" + published[:10], "letter", published, precision)
    event["summary"] = text[:12000]
    event["publication_basis"] = "letter_date"
    return event


def number(value, *, positive=False):
    try:
        result = Decimal(str(value))
        if not result.is_finite() or abs(result) > Decimal("1e18") or (positive and result <= 0):
            raise ValueError("Invalid Treasury numeric field")
        return float(result)
    except (InvalidOperation, TypeError) as error:
        raise ValueError("Invalid Treasury numeric field") from error


def normalize_auction(row, stage):
    cusip = row["cusip"]
    if not re.fullmatch(r"[A-Z0-9]{9}", cusip):
        raise ValueError("Invalid auction CUSIP")
    auction_date = row["auctionDate"][:10]
    datetime.strptime(auction_date, "%Y-%m-%d")
    security = row["securityType"]
    if security not in {"Bill", "Note", "Bond"} or stage not in {"announcement", "result"}:
        raise ValueError("Unsupported auction type/stage")
    filename = row.get("pdfFilenameAnnouncement" if stage == "announcement" else "pdfFilenameCompetitiveResults", "")
    # Actual result file presence + numeric result fields, not the calendar alone.
    match = re.fullmatch(r"([AR])_(\d{8})_\d+\.pdf", filename)
    if not match or match[1] != ("A" if stage == "announcement" else "R"):
        raise ValueError("Auction release document unavailable")
    source_date = row["announcementDate"][:10] if stage == "announcement" else auction_date
    if match[2] != source_date.replace("-", ""):
        raise ValueError("Auction document/date mismatch")
    published, precision = publication(source_date)
    subtype = "TIPS" if row.get("tips") == "Yes" else "FRN" if row.get("floatingRate") == "Yes" else security
    # Query URL provides stable, official provenance without guessing PDF directories.
    url = f"https://www.treasurydirect.gov/TA_WS/securities/search?format=json&cusip={cusip}&auctionDate={auction_date}"
    event = base_event("auction_" + stage,
                       f"Treasury {row['securityTerm']} {subtype} auction {stage} — {auction_date}",
                       url, f"auction:{cusip}:{auction_date}:{security}", stage, published, precision)
    metrics = {"offering_amount_usd": number(row["offeringAmount"], positive=True)}
    if stage == "result":
        metrics.update(total_accepted_usd=number(row["totalAccepted"], positive=True),
                       bid_to_cover=number(row["bidToCoverRatio"], positive=True))
        rate_field = "highDiscountMargin" if subtype == "FRN" else "highDiscountRate" if security == "Bill" else "highYield"
        metrics[rate_field + "_percent"] = number(row[rate_field])
    event.update(cusip=cusip, auction_date=auction_date, security_type=subtype,
                 security_term=row["securityTerm"], reopening=row.get("reopening") == "Yes",
                 source_document=filename, publication_basis="announcement_date" if stage == "announcement" else "result_document_date",
                 reference_period=auction_date, metrics=metrics)
    event["summary"] = f"{event['headline']}. CUSIP {cusip}. " + json.dumps(metrics, sort_keys=True) + ". No auction surprise or tail comparison is available."
    return event
