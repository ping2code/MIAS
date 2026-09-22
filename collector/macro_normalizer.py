"""Source-specific HTML adapters for six official US statistical releases.

No configuration imports: parsing fixtures never loads credentials or services.
"""

import hashlib
import json
import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo


MACRO_SOURCES = (
    {"category": "cpi", "agency": "bls", "url": "https://www.bls.gov/feed/cpi.rss"},
    {"category": "ppi", "agency": "bls", "url": "https://www.bls.gov/feed/ppi.rss"},
    {"category": "employment", "agency": "bls", "url": "https://www.bls.gov/feed/empsit.rss"},
    {"category": "gdp", "agency": "bea", "url": "https://www.bea.gov/data/gdp/gross-domestic-product"},
    {"category": "pce", "agency": "bea", "url": "https://www.bea.gov/data/income-saving/personal-income"},
    {"category": "retail_sales", "agency": "census", "url": "https://www.census.gov/retail/sales.html"},
)
PUBLISHERS = {"bls": "U.S. Bureau of Labor Statistics", "bea": "U.S. Bureau of Economic Analysis",
              "census": "U.S. Census Bureau"}
HOSTS = {"bls": "www.bls.gov", "bea": "www.bea.gov", "census": "www.census.gov"}
MONTHS = "January February March April May June July August September October November December".split()
MONTH_PATTERN = "(?:" + "|".join(MONTHS) + ")"
DATE_PATTERN = rf"{MONTH_PATTERN}\s+\d{{1,2}},?\s+\d{{4}}"
EASTERN = ZoneInfo("America/New_York")


def clean_text(text):
    return " ".join(text.replace("\xa0", " ").split())


class Node:
    def __init__(self, tag="", attrs=()):
        self.tag, self.attrs, self.children = tag, dict(attrs), []

    def text(self):
        if self.tag in {"script", "style", "noscript"}:
            return ""
        return clean_text(" ".join(child.text() if isinstance(child, Node) else child
                                   for child in self.children))

    def find(self, tag=None, css_class=None):
        found = []
        for child in self.children:
            if isinstance(child, Node):
                if ((tag is None or child.tag == tag)
                        and (css_class is None or css_class in child.attrs.get("class", "").split())):
                    found.append(child)
                found.extend(child.find(tag, css_class))
        return found


class Document(HTMLParser):
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.root = Node()
        self.stack = [self.root]
        self.feed(html)
        self.close()

    def handle_starttag(self, tag, attrs):
        node = Node(tag, attrs)
        self.stack[-1].children.append(node)
        if tag not in self.VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def official_url(url, agency):
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname != HOSTS[agency]
            or parsed.username or parsed.password or parsed.port not in (None, 443)):
        raise ValueError("Unexpected official source URL")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def discover_bea_release(html, source):
    """Follow the labeled Current Release, never an arbitrary news link."""
    for link in Document(html).root.find("a"):
        if link.text().lower() == "current release":
            url = official_url(urljoin(source["url"], link.attrs.get("href", "")), "bea")
            if re.match(r"/news/\d{4}/", urlsplit(url).path):
                return url
    raise ValueError("BEA current-release link missing")


def parse_publication(text):
    date_match = re.search(DATE_PATTERN, text, re.I)
    if not date_match:
        return None, "unknown"
    date = datetime.strptime(date_match.group().replace(",", ""), "%B %d %Y")
    time = re.search(r"(\d{1,2}):(\d{2})\s*([ap])\.?m\.?", text, re.I)
    precision = "date"
    if time:
        hour, minute = int(time[1]), int(time[2])
        if not 1 <= hour <= 12 or minute > 59:
            raise ValueError("Invalid release time")
        date = date.replace(hour=hour % 12 + (12 if time[3].lower() == "p" else 0), minute=minute)
        precision = "minute"
    return date.replace(tzinfo=EASTERN).astimezone(timezone.utc).isoformat(), precision


def _period(category, headline, body):
    if category == "gdp":
        match = re.search(r"([1-4])(?:st|nd|rd|th)\s+Quarter\s+(\d{4})", headline, re.I)
        if not match:
            raise ValueError("GDP reference quarter missing")
        return f"{match[2]}-Q{match[1]}"
    text = body if category == "retail_sales" else headline
    pattern = (rf"sales for\s+({MONTH_PATTERN})\s+(\d{{4}})" if category == "retail_sales"
               else rf"({MONTH_PATTERN})\s+(\d{{4}})")
    match = re.search(pattern, text, re.I)
    if not match:
        raise ValueError("Reference month missing")
    return f"{match[2]}-{MONTHS.index(match[1].title()) + 1:02d}"


def _bea_prose(node):
    """Keep release paragraphs up to a real section boundary, not an inline link."""
    paragraphs = []
    for child in node.find():
        text = child.text()
        if re.match(r"^(?:Technical Notes|Annual Update of the National|Next release:)", text, re.I):
            break
        if child.tag == "p" and text:
            paragraphs.append(text)
    return " ".join(paragraphs)


def macro_identity(event):
    """Versioned, semantic identity independent of headline, URL and HTML layout."""
    fields = [event[key] for key in ("agency", "release_category", "reference_period",
                                   "release_stage", "release_id", "revision_id")]
    return hashlib.sha256(json.dumps([1, *fields], separators=(",", ":")).encode()).hexdigest()


def normalize_macro_release(html, source, url=None):
    category, agency = source["category"], source["agency"]
    url = official_url(url or source["url"], agency)
    root = Document(html).root
    if agency == "bls":
        blocks = root.find("pre")
        if not blocks:
            raise ValueError("BLS release text missing")
        text = " ".join(block.text() for block in blocks)
        title = {"cpi": "CONSUMER PRICE INDEX", "ppi": "PRODUCER PRICE INDEX(?:ES)?",
                 "employment": "(?:THE )?EMPLOYMENT SITUATION"}[category]
        match = re.search(rf"{title}\s*[-–—]\s*{MONTH_PATTERN}\s+\d{{4}}", text, re.I)
        if not match:
            raise ValueError("BLS release heading missing")
        headline = match.group()
        body = text[match.end():]
        header = text[:match.start()]
        publication = re.search(r"embargoed until\s+(.{0,180})", header, re.I)
        date_text = publication[1] if publication else ""
    elif agency == "bea":
        heads = root.find("h1")
        label = r"(?:GDP|Gross Domestic Product)" if category == "gdp" else "Personal Income and Outlays"
        headline = next((n.text() for n in heads if re.match(label, n.text(), re.I)), "")
        bodies = root.find(css_class="field--name-body")
        dates = root.find(css_class="field--name-field-release-date")
        if not headline or not bodies:
            raise ValueError("BEA release content missing")
        body = _bea_prose(bodies[0])
        header = root.text()
        date_text = dates[0].text() if dates else ""
    elif agency == "census" and category == "retail_sales":
        headline = next((n.text() for n in root.find("h2")
                         if n.text() == "Advance Monthly Sales for Retail and Food Services"), "")
        text = root.text()
        start = re.search(r"Advance estimates of U\.S\. retail and food services sales for", text, re.I)
        if not headline or not start:
            raise ValueError("Census advance retail release missing")
        body = text[start.start():].split("Additional Release Tables")[0]
        header = text[:start.start()]
        date = re.search(r"FOR IMMEDIATE RELEASE:\s*(.{0,120})", header, re.I)
        date_text = date[1] if date else ""
    else:
        raise ValueError("Unsupported macro release")

    period = _period(category, headline, body)
    stage = "advance" if category == "retail_sales" else "initial"
    if category == "gdp":
        match = re.search(r"\b(advance|second|third) estimate\b", headline, re.I)
        if not match:
            raise ValueError("GDP estimate stage missing")
        stage = match[1].lower()
    published, precision = parse_publication(date_text)
    original_published = published
    release_number = re.search(r"\b(?:USDL[-–—]\d{2}[-–—]\d+|BEA\s+\d{2}[-–—]\d+|CB\d{2}[-–—]\d+)\b", header)
    # A period/stage fallback remains stable when no report number is supplied.
    release_id = (re.sub(r"[–—]", "-", release_number.group()).replace(" ", "-")
                  if release_number else f"{agency}:{category}:{period}:{stage}")
    revision_id = "original"
    # Only explicitly dated correction notices qualify. Routine prior-period
    # revisions and footer modification dates do not create new versions.
    notices = [n.text() for n in root.find() if n.tag in {"p", "div"}
               and re.match(r"^(?:Correction|Corrected release|Corrected on)\s*[:–-]?\s*" + DATE_PATTERN, n.text(), re.I)]
    if notices:
        correction_date, correction_precision = max(parse_publication(n) for n in notices)
        if not original_published or correction_date < original_published:
            raise ValueError("Correction lacks valid publication chronology")
        revision_id = correction_date
        published, precision = correction_date, correction_precision
    # Keep release prose, not navigation/table footers or future-release notices.
    if agency == "bls":
        body = re.split(r"\bTable A\.", body, maxsplit=1, flags=re.I)[0]
    body = clean_text(body)
    if len(body) < 60:
        raise ValueError("Release body is incomplete")
    event = {
        "source": PUBLISHERS[agency], "publisher": PUBLISHERS[agency],
        "headline": headline, "url": url, "published_at": published,
        "summary": body[:6000], "symbols": [], "direct_symbols": [], "related_symbols": [],
        "relevant": True, "event_type": "macro_release", "market_scope": "US macro",
        "agency": agency, "release_category": category, "reference_period": period,
        "release_stage": stage, "release_id": release_id, "revision_id": revision_id,
        "original_published_at": original_published, "timestamp_precision": precision,
    }
    event["event_id"] = macro_identity(event)
    return event
