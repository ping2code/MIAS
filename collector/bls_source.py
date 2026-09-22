"""Official BLS RSS release metadata and credential-free Public Data API v1.

No dotenv, Redis, AI or delivery imports. Never fetch a news.release HTML page.
"""

import json
import math
import re
from datetime import timezone
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree

import requests

from collector.macro_normalizer import Document, MONTHS, PUBLISHERS, macro_identity, official_url


API_URL = "https://api.bls.gov/publicAPI/v1/timeseries/data/"
RSS_URLS = {"cpi": "https://www.bls.gov/feed/cpi.rss",
            "ppi": "https://www.bls.gov/feed/ppi.rss",
            "employment": "https://www.bls.gov/feed/empsit.rss"}
# metric name, series ID, units, seasonal adjustment
SERIES = {
    "cpi": (("headline_cpi_sa", "CUSR0000SA0", "index", "SA"),
            ("headline_cpi_nsa", "CUUR0000SA0", "index", "NSA"),
            ("core_cpi_sa", "CUSR0000SA0L1E", "index", "SA"),
            ("core_cpi_nsa", "CUUR0000SA0L1E", "index", "NSA")),
    "ppi": (("final_demand_ppi_sa", "WPSFD4", "index", "SA"),
            ("final_demand_ppi_nsa", "WPUFD4", "index", "NSA")),
    "employment": (("payrolls", "CES0000000001", "thousand persons", "SA"),
                   ("unemployment_rate", "LNS14000000", "percent", "SA"),
                   ("average_hourly_earnings", "CES0500000003", "USD/hour", "SA")),
}
LABELS = {"cpi": "Consumer Price Index", "ppi": "Producer Price Index",
          "employment": "Employment Situation"}
MAX_BYTES = 2_000_000


class BLSDataError(ValueError):
    """Unavailable or inconsistent numeric release data; retry, never cache it."""


def _read(response, accepted_types):
    response.raise_for_status()
    mime = response.headers.get("Content-Type", "").split(";")[0].strip().lower()
    if response.status_code != 200 or mime not in accepted_types:
        raise ValueError("Unexpected BLS response status or content type")
    content = bytearray()
    for chunk in response.iter_content(chunk_size=65536):
        content.extend(chunk)
        if len(content) > MAX_BYTES:
            raise ValueError("BLS response exceeds size limit")
    return content.decode("utf-8-sig")


def fetch_bls_feed(source):
    url = source["url"]
    if url != RSS_URLS[source["category"]]:
        raise ValueError("Unexpected BLS RSS endpoint")
    with requests.get(url, timeout=(5, 20), allow_redirects=False, stream=True,
                      headers={"User-Agent": "MIAS official macro release collector"}) as response:
        return _read(response, {"application/rss+xml", "application/xml", "text/xml"})


def normalize_bls_feed(xml, source):
    """RSS item dates only. Channel build times and fetch times are not releases."""
    if re.search(r"<!\s*(?:DOCTYPE|ENTITY)", xml, re.I):
        raise ValueError("BLS RSS document declarations are unsupported")
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError as error:
        raise ValueError("Invalid BLS RSS") from error
    if root.tag != "rss":
        raise ValueError("Expected BLS RSS")
    category = source["category"]
    candidates = []
    for item in root.findall("./channel/item"):
        title = Document(item.findtext("title", "")).root.text()
        body = Document(item.findtext("description", "")).root.text()
        link = official_url(item.findtext("link", ""), "bls")
        # Links are provenance only, never fetched. Reject other release families.
        stem = {"cpi": "cpi", "ppi": "ppi", "employment": "empsit"}[category]
        if not re.search(rf"/news\.release/(?:archives/)?{stem}(?:_\d+)?\.htm$", link):
            continue
        stamp = item.findtext("pubDate")
        published = None
        if stamp:
            try:
                date = parsedate_to_datetime(stamp)
                if date.tzinfo is None:
                    raise ValueError("BLS publication timezone missing")
                published = date.astimezone(timezone.utc).isoformat()
            except (ValueError, TypeError, OverflowError) as error:
                raise ValueError("Invalid BLS publication date") from error
        # RSS headlines commonly include only the reference month. Resolve its
        # year against publication, never against the current/fetch date.
        month = re.search(r"\b(" + "|".join(MONTHS) + r")\b(?:\s+(\d{4}))?", title, re.I)
        if not month:
            month = re.search(r"\b(?:in|for)\s+(" + "|".join(MONTHS) + r")\b(?:\s+(\d{4}))?", body, re.I)
        if not month or (not month[2] and not published):
            raise ValueError("BLS RSS reference month/year missing")
        number = MONTHS.index(month[1].title()) + 1
        year = int(month[2]) if month[2] else date.year - (number > date.month)
        period = f"{year:04d}-{number:02d}"
        corrected = bool(re.match(r"^(?:Correction|Corrected)\b", title, re.I))
        if corrected and not published:
            raise ValueError("BLS correction date missing")
        event = {
            "source": PUBLISHERS["bls"], "publisher": PUBLISHERS["bls"],
            "headline": f"{LABELS[category]} — {MONTHS[number - 1]} {year}",
            "url": link, "published_at": published, "summary": body[:6000],
            "symbols": [], "direct_symbols": [], "related_symbols": [],
            "relevant": True, "event_type": "macro_release", "market_scope": "US macro",
            "agency": "bls", "release_category": category, "reference_period": period,
            "release_stage": "initial", "release_id": f"bls:{category}:{period}:initial",
            "revision_id": published if corrected else "original",
            "original_published_at": None if corrected else published,
            "timestamp_precision": "second" if published else "unknown",
            "release_feed_url": source["url"],
        }
        event["event_id"] = macro_identity(event)
        candidates.append(event)
    if not candidates:
        raise ValueError("BLS RSS release missing")
    # Do not silently fall back to an older entry if the newest period lacks a date.
    return max(candidates, key=lambda e: (e["reference_period"], e["published_at"] or ""))


def fetch_bls_data(category, period):
    year = int(period[:4])
    payload = {"seriesid": [s[1] for s in SERIES[category]],
               "startyear": str(year - 1), "endyear": str(year)}
    try:
        with requests.post(API_URL, json=payload, timeout=(5, 20), allow_redirects=False,
                           stream=True, headers={"User-Agent": "MIAS official macro release collector"}) as response:
            # BLS v1 POST currently serves JSON with text/plain content type.
            return json.loads(_read(response, {"application/json", "text/plain"}))
    except (requests.RequestException, ValueError) as error:
        raise BLSDataError("BLS API request failed") from error


def _offset(period, months):
    year, month = map(int, period.split("-"))
    year, zero_month = divmod(year * 12 + month - 1 + months, 12)
    return f"{year:04d}-{zero_month + 1:02d}"


def extract_bls_metrics(payload, category, period):
    """Match every series to the RSS reference month; never mix latest months."""
    try:
        if payload["status"] != "REQUEST_SUCCEEDED" or payload.get("message"):
            raise ValueError("BLS API reported an error or warning")
        series = payload["Results"]["series"]
        by_id = {s["seriesID"]: s["data"] for s in series}
        if len(by_id) != len(series):
            raise ValueError("Duplicate BLS series")
        metrics = {}
        for name, series_id, units, adjustment in SERIES[category]:
            observations = {}
            for row in by_id[series_id]:
                if not re.fullmatch(r"M(?:0[1-9]|1[0-2])", row["period"]):
                    continue  # Exclude M13 annual averages.
                key = f"{int(row['year']):04d}-{row['period'][1:]}"
                if key in observations:
                    raise ValueError("Duplicate BLS monthly observation")
                observations[key] = row

            def value(key):
                raw = observations[key]["value"]
                number = Decimal(raw)
                if not number.is_finite() or number < 0 or not math.isfinite(float(number)):
                    raise ValueError("Invalid BLS numeric observation")
                return number

            current = value(period)
            notes = observations[period].get("footnotes", [])
            if not isinstance(notes, list) or any(not isinstance(f, dict) for f in notes):
                raise ValueError("Invalid BLS observation footnotes")
            metric = {"series_id": series_id, "period": period, "value": float(current),
                      "units": units, "seasonal_adjustment": adjustment,
                      "footnotes": [f for f in notes if f]}
            # Require the comparison observation too: API release updates can lag.
            comparison = _offset(period, -12 if adjustment == "NSA" else -1)
            previous = value(comparison)
            metric["comparison_period"] = comparison
            metric["comparison_value"] = float(previous)
            if name == "payrolls":
                metric["monthly_change_persons"] = int((current - previous) * 1000)
            elif name == "unemployment_rate":
                metric["monthly_change_percentage_points"] = float(current - previous)
            else:
                if previous <= 0:
                    raise ValueError("Invalid BLS comparison denominator")
                key = "yoy_percent" if adjustment == "NSA" else "mom_percent"
                metric[key] = round(float((current / previous - 1) * 100), 4)
                if not math.isfinite(metric[key]):
                    raise ValueError("Invalid BLS computed change")
            metrics[name] = metric
        return metrics
    except (KeyError, TypeError, ValueError, InvalidOperation, OverflowError) as error:
        raise BLSDataError("BLS observations unavailable or inconsistent for release") from error


def enrich_bls_event(event, payload=None):
    category, period = event["release_category"], event["reference_period"]
    if payload is None:
        payload = fetch_bls_data(category, period)
    metrics = extract_bls_metrics(payload, category, period)
    facts = []
    for name, metric in metrics.items():
        fact = f"{name}: {metric['value']} {metric['units']} ({metric['seasonal_adjustment']})"
        for key in ("mom_percent", "yoy_percent", "monthly_change_persons", "monthly_change_percentage_points"):
            if key in metric:
                fact += f", {key}={metric[key]}"
        notes = [f["text"] for f in metric["footnotes"] if isinstance(f.get("text"), str)]
        if notes:
            fact += " [" + "; ".join(notes) + "]"
        facts.append(fact)
    # Put numeric facts first so bounded AI input retains them.
    summary = f"BLS {period}. " + "; ".join(facts) + ". Changes computed from current API observations; not a release-vintage archive. "
    return dict(event, metrics=metrics, data_source_url=API_URL,
                summary=(summary + event["summary"])[:6000])
