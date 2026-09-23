"""Bounded official-source HTTP adapters. No dotenv, Redis or service imports."""

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

import feedparser
import requests

from collector.macro_normalizer import Document, parse_publication
from collector.geopolitical_normalizer import official_url

SOURCES = {
    "fr": "https://www.federalregister.gov/api/v1/documents.json",
    "fr_inspection": "https://www.federalregister.gov/api/v1/public-inspection-documents.json",
    "ftc": "https://www.ftc.gov/feeds/press-release.xml",
    "moea": "https://www.moea.gov.tw/Mns/english/news/NewsRSSdetail.aspx?Kind=6",
    "bis": "https://www.bis.gov/news-updates",
    "ofac": "https://ofac.treasury.gov/recent-actions",
    "treasury": "https://home.treasury.gov/news/press-releases",
    "ustr": "https://ustr.gov/about-us/policy-offices/press-office/press-releases",
    "whitehouse": "https://www.whitehouse.gov/presidential-actions/",
}
MAX_BYTES, MAX_PAGES, MAX_DOCUMENTS = 8_000_000, 20, 200
logger = logging.getLogger("geopolitical_collector")
AGENCIES = {"commerce-department", "industry-and-security-bureau", "foreign-assets-control-office",
            "trade-representative-office-of-united-states", "executive-office-of-the-president",
            "federal-trade-commission", "treasury-department"}


def fetch(url, types):
    url = official_url(url)
    with requests.get(url, timeout=(5, 25), allow_redirects=False, stream=True,
                      headers={"User-Agent": "MIAS official geopolitical collector"}) as response:
        response.raise_for_status()
        mime = response.headers.get("Content-Type", "").split(";")[0].lower().strip()
        if response.status_code != 200 or mime not in types:
            raise ValueError("Unexpected official response status/type")
        data = bytearray()
        for chunk in response.iter_content(65536):
            data.extend(chunk)
            if len(data) > MAX_BYTES:
                raise ValueError("Official document exceeds size limit")
        return bytes(data)


def pages(url):
    seen = set()
    base = urlsplit(url).path.replace("-", "_").removesuffix(".json")
    for _ in range(MAX_PAGES):
        if url in seen:
            raise ValueError("Repeated official API page")
        seen.add(url)
        data = json.loads(fetch(url, {"application/json"}))
        if not isinstance(data.get("results"), list):
            raise ValueError("Invalid official API schema")
        yield from data["results"]
        next_url = data.get("next_page_url")
        if not next_url:
            return
        url = official_url(next_url, "fr")
        if urlsplit(url).hostname != "www.federalregister.gov" or urlsplit(url).path.replace("-", "_").removesuffix(".json") != base:
            raise ValueError("Unexpected API pagination target")
    raise ValueError("Official API pagination incomplete")


def _fr_documents(inspection):
    key = "fr_inspection" if inspection else "fr"
    query = {"per_page": 100}
    if not inspection:
        query.update({"order": "newest", "conditions[publication_date][gte]":
                      (datetime.now(timezone.utc) - timedelta(days=4)).date().isoformat()})
    rows = list(pages(SOURCES[key] + "?" + urlencode(query)))
    documents = []
    for row in rows:
        if not any(a.get("slug") in AGENCIES for a in row.get("agencies", [])):
            continue
        # Discovery only: these terms never establish scoring or relevance.
        if not re.search(r"\b(?:export|trade|tariffs?|sanctions?|entity list|computing|semiconductor|chips?|data|antitrust|AI|Meta|NVIDIA|investment|ICTS|Taiwan|China|301|232)\b",
                         row.get("title", "") + " " + (row.get("abstract") or ""), re.I):
            continue
        if len(documents) >= MAX_DOCUMENTS:
            raise ValueError("FR document bound exceeded; source incomplete")
        number = row["document_number"]
        if not re.fullmatch(r"\d{4}-\d{4,6}", number):
            raise ValueError("Invalid FR number")
        detail = row if inspection else json.loads(fetch(
            "https://www.federalregister.gov/api/v1/documents/" + number + ".json", {"application/json"}))
        if detail["document_number"] != number:
            raise ValueError("FR detail identity mismatch")
        text_url = detail.get("raw_text_url")
        if not text_url:
            raise ValueError("FR full text unavailable")
        text_url = official_url(text_url, "fr")
        body = fetch(text_url, {"text/plain", "text/html"}).decode("utf-8-sig")
        anchors = ["fr:" + number]
        if detail.get("executive_order_number"):
            anchors.append("eo:" + str(detail["executive_order_number"]))
        documents.append(dict(agency="fr", url=detail["html_url"], native_id=number,
                              headline=detail["title"], body=body, identity_anchors=anchors,
                              published_at=detail.get("filed_at") if inspection else detail.get("publication_date"),
                              publication_basis="public_inspection_filed_at" if inspection else "publication_date",
                              scheduled_publication=detail.get("publication_date"),
                              effective_at=detail.get("effective_on"), document_type=detail.get("type"),
                              legal_references=detail.get("docket_ids", [])))
    return documents


def _anchors(body, url):
    anchors = set()
    for link in body.find("a"):
        try:
            target = official_url(urljoin(url, link.attrs.get("href", "")))
        except ValueError:
            continue
        p = urlsplit(target)
        # Only explicit current-action labels qualify; historical/background links do not.
        label = link.text()
        if re.search(r"\b(?:previous|prior|background|history|superseded)\b", label, re.I):
            continue
        match = re.search(r"/(\d{4}-\d{4,6})(?:/|\.pdf|$)", p.path)
        if p.hostname in {"www.federalregister.gov", "public-inspection.federalregister.gov"} and match and re.search(r"\b(?:this|new|final|interim) (?:rule|order|notice)\b", label, re.I):
            anchors.add("fr:" + match[1])
        eo = re.search(r"/eo-(\d+)\.pdf$", p.path)
        if p.hostname == "www.whitehouse.gov" and eo:
            anchors.add("eo:" + eo[1])
        if p.hostname == "ofac.treasury.gov" and re.fullmatch(r"/recent-actions/\d{8}(?:_\d+)?", p.path) and re.search(r"\b(?:this|new) (?:action|notice)\b", label, re.I):
            anchors.add("ofac:" + p.path.rsplit("/", 1)[1])
        case = re.match(r"/legal-library/browse/cases-proceedings/(\d{6,})-", p.path)
        if p.hostname == "www.ftc.gov" and case and re.search(r"\b(?:case|complaint|order|settlement)\b", label, re.I):
            anchors.add("ftc-case:" + case[1])
    # Multiple linked instruments need explicit decomposition, not guessed equivalence.
    return sorted(anchors) if len(anchors) == 1 else []


def parse_article(html, agency, url, *, headline=None, published=None, native_id=None):
    root = Document(html).root
    url = official_url(url, agency)
    main = (root.find("main") or [root])[0]
    selectors = {"ofac": "field--name-field-body", "treasury": "field--name-field-news-body",
                 "ustr": "field--name-body", "whitehouse": "entry-content",
                 "ftc": "field--name-body", "bis": "press-release-container", "moea": "newsContent"}
    bodies = main.find(css_class=selectors[agency])
    if not bodies and agency == "bis":
        bodies = main.find("article")
    if not bodies and agency == "moea":
        bodies = [n for n in main.find() if n.attrs.get("id", "").endswith("lblContent")]
    # Never fall back to whole-page navigation or related-article text.
    if not bodies:
        raise ValueError("Official article body selector unavailable")
    body = bodies[0]
    headings = main.find("h1")
    if agency == "bis":
        headings = [n for n in main.find("h2") if "text-primary-600" in (n.attrs.get("class") or "").split()]
    if not headings and agency == "ftc":
        headings = root.find(css_class="page-title")
    headline = headline or (headings[0].text() if headings else "")
    stamp = published
    if not stamp:
        metas = [n.attrs.get("content") for n in root.find("meta") if n.attrs.get("property") == "article:published_time"]
        if metas:
            stamp = metas[0]
        else:
            date_class = {"ofac": "field--name-field-release-date", "treasury": "field--name-field-news-publication-date",
                          "bis": "date", "ustr": "date-display-single"}.get(agency)
            dates = main.find(css_class=date_class) if date_class else []
            if dates:
                times = dates[0].find("time")
                stamp = times[0].attrs.get("datetime") if times else None
                if not stamp:
                    raw = dates[0].text()
                    numeric = re.search(r"\b(\d{2})/(\d{2})/(\d{4})\b", raw)
                    stamp = f"{numeric[3]}-{numeric[1]}-{numeric[2]}" if numeric else parse_publication(raw)[0]
    if agency == "ofac":
        native_id = urlsplit(url).path.rsplit("/", 1)[1]
    return dict(agency=agency, url=url, headline=headline, published_at=stamp,
                native_id=native_id or url, body=body.text(), identity_anchors=_anchors(body, url))


def _rss(agency):
    data = fetch(SOURCES[agency], {"application/rss+xml", "application/xml", "text/xml"})
    if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise ValueError("RSS entities forbidden")
    feed = feedparser.parse(data)
    if feed.bozo or len(feed.entries) > MAX_DOCUMENTS:
        raise ValueError("RSS malformed or entry bound exceeded")
    result = []
    for entry in feed.entries:
        url = official_url(entry["link"], agency)
        native = entry.get("id") if agency == "ftc" else parse_qs(urlsplit(url).query).get("news_id", [None])[0]
        if not native:
            raise ValueError("RSS identity unavailable")
        if agency == "moea":
            # Live feed has body text; retain complete description, not truncated dc:content.
            body = Document(entry.get("summary", "")).root.text()
            result.append(dict(agency=agency, url=url, native_id=native, headline=entry["title"],
                               published_at=entry.get("published"), body=body, identity_anchors=[]))
        else:
            try:
                html = fetch(url, {"text/html"}).decode("utf-8-sig")
                result.append(parse_article(html, agency, url, headline=entry["title"],
                                            published=entry.get("published"), native_id=native))
            except (ValueError, requests.RequestException) as error:
                result.append({"source_error": type(error).__name__, "agency": agency, "url": url})
    return result


def _index_links(root, agency, base):
    patterns = {"bis": r"/press-release/[^/]+/?$", "ofac": r"/recent-actions/\d{8}(?:_\d+)?/?$",
                "treasury": r"/news/press-releases/[\w-]+/?$",
                "ustr": r"/about(?:-us)?/policy-offices/press-office/press-releases/\d{4}/[^/]+/[^/]+/?$",
                "whitehouse": r"/presidential-actions/\d{4}/\d{2}/[^/]+/?$"}
    links = []
    for n in root.find("a"):
        try:
            url = official_url(urljoin(base, n.attrs.get("href", "")), agency)
        except ValueError:
            continue
        if re.fullmatch(patterns[agency], urlsplit(url).path) and url not in links:
            links.append(url)
    return links


def _html_documents(agency):
    index_dates = {}
    if agency == "treasury":
        # Reuse the existing pure official manifest discovery; no collector behavior changes.
        from collector.treasury_sources import fetch_index_documents, PRESS_URL
        links = [d["url"] for d in fetch_index_documents(PRESS_URL)]
    else:
        url, seen, links = SOURCES[agency], set(), []
        for _ in range(MAX_PAGES):
            if url in seen:
                raise ValueError("Repeated HTML discovery page")
            seen.add(url)
            root = Document(fetch(url, {"text/html"}).decode("utf-8-sig")).root
            if agency == "ustr":
                for listing in root.find("ul", css_class="listing"):
                    for item in listing.find("li"):
                        date = re.match(r"(\d{4}-\d{2}-\d{2})\b", item.text())
                        item_links = _index_links(item, agency, url)
                        if date and len(item_links) == 1:
                            index_dates[item_links[0]] = date[1]
            links.extend(u for u in _index_links(root, agency, url) if u not in links)
            if len(links) > MAX_DOCUMENTS:
                logger.warning("Geopolitical %s discovery capped at %s documents", agency, MAX_DOCUMENTS)
                links = links[:MAX_DOCUMENTS]
                break
            nxt = [a.attrs.get("href") for a in root.find("a") if "next" in (a.attrs.get("rel") or "").split()]
            if not nxt:
                break
            url = official_url(urljoin(url, nxt[0]), agency)
        else:
            raise ValueError("HTML pagination incomplete")
    if not links:
        raise ValueError("Official discovery returned no article links")
    documents = []
    for url in links:
        try:
            html = fetch(url, {"text/html"}).decode("utf-8-sig")
            documents.append(parse_article(html, agency, url, published=index_dates.get(url)))
        except (ValueError, requests.RequestException) as error:
            documents.append({"source_error": type(error).__name__, "agency": agency, "url": url})
    return documents


def fetch_documents(source):
    if source in {"fr", "fr_inspection"}:
        return _fr_documents(source == "fr_inspection")
    if source in {"ftc", "moea"}:
        return _rss(source)
    return _html_documents(source)
