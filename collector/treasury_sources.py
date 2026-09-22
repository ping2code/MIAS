"""Bounded official Treasury HTTP adapters; no service/configuration imports."""

import io
import json
import logging
import re
from datetime import timedelta
from urllib.parse import urlencode, urljoin, urlsplit

import requests

from collector.macro_normalizer import Document
from collector.treasury_normalizer import official_url
from collector.treasury_yields import YIELD_URL, DATASET, parse_yield_observations, normalize_yield_events


AUCTION_URL = "https://www.treasurydirect.gov/TA_WS/securities/search"
PRESS_URL = "https://home.treasury.gov/news/press-releases"
REFUNDING_URL = "https://home.treasury.gov/policy-issues/financing-the-government/quarterly-refunding/most-recent-quarterly-refunding-documents"
DEBT_URL = "https://home.treasury.gov/policy-issues/financial-markets-financial-institutions-and-fiscal-service/debt-limit"
INDEX_URLS = (PRESS_URL, REFUNDING_URL, DEBT_URL)
MAX_BYTES = 5_000_000
MAX_ROWS = 1000
MAX_DOCUMENTS = 40
MAX_PAGES = 5


def fetch(url, types):
    url = official_url(url)
    with requests.get(url, timeout=(5, 20), allow_redirects=False, stream=True,
                      headers={"User-Agent": "MIAS official Treasury collector"}) as response:
        response.raise_for_status()
        mime = response.headers.get("Content-Type", "").split(";")[0].lower().strip()
        if response.status_code != 200 or mime not in types:
            raise ValueError("Unexpected Treasury response status/type")
        data = bytearray()
        for chunk in response.iter_content(chunk_size=65536):
            data.extend(chunk)
            if len(data) > MAX_BYTES:
                raise ValueError("Treasury response exceeds size bound")
        return bytes(data)


def fetch_auctions(today, lookback_days=3):
    """Query announcement and auction dates separately, including future offerings."""
    records = {}
    for offset in range(lookback_days):
        day = (today - timedelta(days=offset)).isoformat()
        for field in ("announcementDate", "auctionDate"):
            url = AUCTION_URL + "?" + urlencode({"format": "json", field: day})
            rows = json.loads(fetch(url, {"application/json"}))
            if not isinstance(rows, list) or len(rows) >= MAX_ROWS:
                raise ValueError("Unexpected or potentially truncated auction response")
            for row in rows:
                if not isinstance(row, dict) or row.get(field, "")[:10] != day:
                    raise ValueError("Auction API filter contract mismatch")
                key = (row["cusip"], row["auctionDate"], row["securityType"])
                if key in records and records[key] != row:
                    # A state transition during retrieval can mix announcement/result
                    # snapshots. Retry a coherent fetch on the next poll.
                    raise ValueError("Auction changed during source retrieval")
                records[key] = row
    return list(records.values())


def fetch_yields(today):
    current = today.replace(day=1)
    previous = (current - timedelta(days=1)).replace(day=1)
    rows = []
    for month in (previous, current):
        url = YIELD_URL + "?" + urlencode({"data": DATASET, "field_tdr_date_value_month": month.strftime("%Y%m")})
        part = parse_yield_observations(fetch(url, {"text/xml", "application/xml", "application/atom+xml"}))
        if any(row["observation_date"][:7] != month.strftime("%Y-%m") for row in part):
            raise ValueError("Yield month filter contract mismatch")
        rows.extend(part)
    return normalize_yield_events(rows, today)


def discover_documents(html, index_url):
    root = Document(html).root
    main = root.find("main")
    scope = main[0] if main else root
    links = {}
    for node in scope.find("a"):
        href = node.attrs.get("href", "")
        if not href:
            continue
        try:
            url = official_url(urljoin(index_url, href))
        except ValueError:
            continue
        if urlsplit(url).hostname != "home.treasury.gov":
            continue
        path = urlsplit(url).path
        press = re.fullmatch(r"/news/press-releases/[a-zA-Z0-9-]+", path)
        letter = (index_url == DEBT_URL and path.startswith("/system/files/") and path.lower().endswith(".pdf")
                  and re.search(r"debt.limit|extraordinary measures", node.text(), re.I))
        if press or letter:
            links[url] = {"url": url, "kind": "letter" if letter else "release"}
    return list(links.values())


def fetch_index_documents(index_url):
    documents = {}
    visited = set()
    url = index_url
    for _ in range(MAX_PAGES):
        if url in visited:
            logging.getLogger("treasury_collector").warning("Treasury pagination repeated; retaining already discovered documents")
            return list(documents.values())[:MAX_DOCUMENTS]
        visited.add(url)
        html = fetch(url, {"text/html"}).decode("utf-8-sig")
        if index_url == PRESS_URL:
            root = Document(html).root
            manifests = [n.attrs["data-news-manifest"] for n in root.find()
                         if n.attrs.get("data-news-manifest")]
            if manifests:
                return fetch_press_manifest(manifests[0])
        for document in discover_documents(html, index_url):
            documents[document["url"]] = document
        root = Document(html).root
        next_links = [n for n in root.find("a") if "next" in n.attrs.get("rel", "").split()]
        if not next_links:
            if len(documents) > MAX_DOCUMENTS:
                logging.getLogger("treasury_collector").warning("Treasury index document limit reached; older documents deferred")
            return list(documents.values())[:MAX_DOCUMENTS]
        url = official_url(urljoin(index_url, next_links[0].attrs["href"]))
        if urlsplit(url).path != urlsplit(index_url).path:
            raise ValueError("Unexpected Treasury pagination path")
    raise ValueError("Treasury pagination bound exceeded")


def fetch_press_manifest(path):
    """Treasury's live press index advertises year-sharded JSON, not HTML paging."""
    if path != "/news-data/press-releases/manifest.json":
        raise ValueError("Unexpected Treasury news manifest")
    manifest = json.loads(fetch("https://home.treasury.gov" + path, {"application/json"}))
    if manifest.get("category") != "press-releases" or not isinstance(manifest.get("searchShards"), list):
        raise ValueError("Invalid Treasury news manifest")
    shards = sorted(manifest["searchShards"], key=lambda s: int(s["endYear"]), reverse=True)[:2]
    if not shards:
        raise ValueError("Treasury news shards missing")
    items = []
    for shard in shards:
        path = shard["path"]
        if not re.fullmatch(r"/news-data/press-releases/search/\d{4}\.json", path):
            raise ValueError("Unexpected Treasury news shard")
        payload = json.loads(fetch("https://home.treasury.gov" + path, {"application/json"}))
        rows = payload.get("items")
        if (payload.get("category") != "press-releases" or not isinstance(rows, list)
                or len(rows) != payload.get("count") or len(rows) != shard["count"]):
            raise ValueError("Incomplete Treasury news shard")
        items.extend(rows)
    documents = {}
    for row in sorted(items, key=lambda r: r["datetime"], reverse=True):
        url = official_url(urljoin(PRESS_URL, row["url"]))
        if (urlsplit(url).hostname != "home.treasury.gov"
                or not re.fullmatch(r"/news/press-releases/[a-zA-Z0-9-]+", urlsplit(url).path)):
            raise ValueError("Invalid Treasury news release link")
        documents[url] = {"url": url, "kind": "release"}
    if len(documents) > MAX_DOCUMENTS:
        logging.getLogger("treasury_collector").warning("Treasury news discovery limited to newest %s releases", MAX_DOCUMENTS)
    return list(documents.values())[:MAX_DOCUMENTS]


def extract_pdf_text(data):
    from pypdf import PdfReader
    if not data.startswith(b"%PDF-"):
        raise ValueError("Expected PDF document")
    try:
        reader = PdfReader(io.BytesIO(data), strict=True)
        if reader.is_encrypted or len(reader.pages) > 20:
            raise ValueError("Unsupported Treasury PDF")
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception as error:
        raise ValueError("Treasury PDF extraction failed") from error
    if len(text.strip()) < 100:
        raise ValueError("Treasury PDF text unavailable")
    return text
