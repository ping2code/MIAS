"""Fixed test-only News/RSS replay corpus.

Rows are exactly what the real news collector (``collector.rss_reader.read_feed``)
submits to its shadow hook while reading synthetic RSS entries against an
in-memory Redis (exact dedup and near-duplicate headline keys, with expiry) at
fixed clocks. There is no network, live Redis, OpenAI or Telegram: the OpenAI
entry point is a deterministic stand-in returning MIAS-shaped enrichment (or
failing), and Telegram is a stub. URLs and headlines are synthetic
(``mias-fixture``) and shaped like the configured Yahoo/Google News feeds.
"""
from contextlib import redirect_stdout
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from fnmatch import fnmatchcase
from io import StringIO
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

# Prevent configuration imports from consulting local secrets.
with patch("dotenv.load_dotenv"), patch.dict(os.environ, {
    "ALERT_THRESHOLD": "70", "DISPLAY_THRESHOLD": "40", "RSS_ENTRY_LIMIT": "10",
    "REDIS_HOST": "localhost", "REDIS_PORT": "6379", "DEDUP_TTL_SECONDS": "86400",
    "HEADLINE_TTL_SECONDS": "86400", "NEAR_DUPLICATE_THRESHOLD": "0.80",
}, clear=True):
    from collector import rss_reader as news
    from analyzer import deduplicator, scoring_engine

MANIFEST = Path(__file__).parent / "fixtures/persistence/news_readiness_v1.json"
NOW = datetime(2026, 9, 24, 12, tzinfo=timezone.utc)
TTL = timedelta(seconds=86400)
FEEDS = dict(yahoo="Yahoo Finance META/NVDA", google_meta="Google News META", google_nvda="Google News NVDA")


class NewsMemoryRedis:
    """get/set(nx, ex)/scan_iter with expiry against a controllable clock (as the news dedup uses Redis)."""

    def __init__(self, now=NOW):
        self.data, self.now = {}, now

    def get(self, key):
        value, expiry = self.data.get(key, (None, self.now))
        return value if expiry > self.now else None

    def set(self, key, value, *, ex, nx=False):
        if nx and self.get(key) is not None:
            return None
        self.data[key] = (value, self.now + timedelta(seconds=ex))
        return True

    def scan_iter(self, match="*"):
        return [key for key in list(self.data) if fnmatchcase(key, match) and self.get(key) is not None]

    def live(self):
        return sorted((k, v) for k, (v, expiry) in self.data.items() if expiry > self.now)


class FailingRedis(NewsMemoryRedis):
    def set(self, key, value, **kwargs):
        raise deduplicator.redis.RedisError("unavailable")

    def scan_iter(self, match="*"):
        raise deduplicator.redis.RedisError("unavailable")


def rss_date(hours_before_now):
    return (NOW - timedelta(hours=hours_before_now)).strftime("%a, %d %b %Y %H:%M:%S +0000")


def entry(title, link, *, publisher=None, hours=2, summary="", published=True):
    item = dict(title=title, link=link, summary=summary)
    if published is True:
        item["published"] = rss_date(hours)
    elif published:
        item["published"] = published
    if publisher:
        item["source"] = {"title": publisher, "href": "https://publisher.example"}
    if link is None:
        item.pop("link")
    return item


REUTERS = "https://www.reuters.com/technology/mias-fixture-"
YAHOO = "https://finance.yahoo.com/news/mias-fixture-"
GOOGLE = "https://news.google.com/rss/articles/mias-fixture-"
ENTRIES = dict(
    meta_reuters=entry("Meta launches new AI model for Instagram creators", REUTERS + "meta-model",
                       publisher="Reuters", summary="Meta Platforms introduced a generative model for creators."),
    nvda_yahoo=entry("Nvidia earnings beat estimates on record data center revenue", YAHOO + "nvda-earnings",
                     summary="Nvidia reported quarterly results above expectations."),
    related_cnbc=entry("Chipmakers rally as server demand improves", GOOGLE + "chipmakers-rally", publisher="CNBC",
                       summary="Nvidia and peers gained as cloud providers raised spending plans."),
    meta_yahoo_publisher=entry("Meta expands WhatsApp business messaging tools", GOOGLE + "whatsapp-business",
                               publisher="Yahoo Finance", summary="New features for merchants."),
    low_quality=entry("Is Nvidia a buy before next week?", GOOGLE + "nvda-buy", publisher="Stock Picks Daily",
                      summary="A look at the chipmaker."),
    ai_penalty=entry("Opinion: Meta spending on AI looks set to keep rising", GOOGLE + "meta-opinion",
                     publisher="Bloomberg", summary="A columnist weighs capital expenditure."),
    ai_failure=entry("Nvidia unveils new AI networking chip", REUTERS + "nvda-networking", publisher="Reuters",
                     summary="The company showed a new switch."),
    no_symbol=entry("Apple shares slip after supplier warning", GOOGLE + "apple-supplier", publisher="Reuters",
                    summary="A component maker cut its outlook."),
    stale=entry("Nvidia stock review of the prior quarter", GOOGLE + "nvda-review", publisher="Barron's", hours=72,
                summary="Looking back at results."),
    missing_timestamp=entry("Meta Platforms shares traded higher on volume", GOOGLE + "meta-volume",
                            publisher="MarketBeat", published=False),
    unparsed_timestamp=entry("Instagram tests new reels layout", GOOGLE + "instagram-reels", publisher="Investing.com",
                             published="yesterday afternoon"),
    no_link=entry("Nvidia supplier update without a link", None, summary="Short item."),
    same_headline_same_publisher=entry("Meta launches new AI model for Instagram creators", REUTERS + "meta-model-2",
                                       publisher="Reuters", summary="Syndicated copy."),
    same_headline_other_publisher=entry("Meta launches new AI model for Instagram creators", GOOGLE + "meta-model-inv",
                                        publisher="Investing.com", summary="Syndicated copy."),
    headline_small_edit=entry("Meta launches new AI model for Instagram creators - update", REUTERS + "meta-model",
                              publisher="Reuters", summary="Meta Platforms introduced a generative model for creators."),
    headline_rewrite=entry("Record quarter lifts Nvidia as shares climb late", YAHOO + "nvda-earnings",
                           summary="Nvidia reported quarterly results above expectations."),
    near_below_a=entry("Nvidia stock rises as AI chip demand grows", GOOGLE + "nvda-demand", publisher="Reuters"),
    near_below_b=entry("Nvidia shares fall after new export limits", GOOGLE + "nvda-export", publisher="Reuters"),
    near_above_a=entry("Meta faces EU fine over data transfers", GOOGLE + "meta-eu-fine", publisher="Reuters"),
    near_above_b=entry("Meta faces EU fine over data transfer rules", GOOGLE + "meta-eu-fine-2", publisher="Investing.com"),
)

AI_RESULTS = {  # Keyed by fixture URL; anything else is not an ALERT candidate in this corpus.
    REUTERS + "meta-model": dict(summary="Meta released a creator model.", sentiment="BULLISH", confidence=78,
                                 why_it_matters="Creator tools may lift engagement.", event_type="product launch"),
    YAHOO + "nvda-earnings": dict(summary="Nvidia beat estimates.", sentiment="STRONGLY_BULLISH", confidence=90,
                                  why_it_matters="Data center demand remains strong.", event_type="earnings"),
    GOOGLE + "meta-opinion": dict(summary="A columnist expects higher spending.", sentiment="NEUTRAL", confidence=55,
                                  why_it_matters="Opinion on capital expenditure.", event_type="prediction article"),
    GOOGLE + "nvda-demand": dict(summary="Demand for AI chips is growing.", sentiment="BULLISH", confidence=70,
                                 why_it_matters="Supports revenue outlook.", event_type="analyst commentary"),
}


def fake_ai(event):
    """Deterministic stand-in for analyze_market_event: same field mapping, no network."""
    result = AI_RESULTS.get(event["url"])
    if result is None:
        raise RuntimeError("synthetic OpenAI failure")
    for key, value in result.items():
        event[f"ai_{key}"] = value
    return event


# (label, feed, entry names, clock, redis: "same" | "fresh" | "failing", telegram succeeds)
STEPS = [
    ("meta_reuters", "google_meta", ["meta_reuters"], NOW, "same", True),
    ("nvda_yahoo", "yahoo", ["nvda_yahoo"], NOW, "same", True),
    ("related_cnbc", "google_nvda", ["related_cnbc"], NOW, "same", True),
    ("meta_yahoo_publisher", "google_meta", ["meta_yahoo_publisher"], NOW, "same", True),
    ("low_quality", "google_nvda", ["low_quality"], NOW, "same", True),
    ("ai_penalty", "google_meta", ["ai_penalty"], NOW, "same", True),
    ("ai_failure", "google_nvda", ["ai_failure"], NOW, "same", False),
    ("no_symbol", "google_meta", ["no_symbol"], NOW, "same", True),
    ("stale", "google_nvda", ["stale"], NOW, "same", True),
    ("missing_timestamp", "google_meta", ["missing_timestamp"], NOW, "same", True),
    ("unparsed_timestamp", "google_meta", ["unparsed_timestamp"], NOW, "same", True),
    ("no_link", "yahoo", ["no_link"], NOW, "same", True),
    ("exact_duplicate", "google_meta", ["meta_reuters"], NOW + timedelta(minutes=30), "same", True),
    ("cross_feed_duplicate", "google_meta", ["related_cnbc"], NOW + timedelta(minutes=30), "same", True),
    ("same_headline_same_publisher", "google_meta", ["same_headline_same_publisher"], NOW + timedelta(hours=1), "same", True),
    ("same_headline_other_publisher", "google_meta", ["same_headline_other_publisher"], NOW + timedelta(hours=1), "same", True),
    ("headline_small_edit", "google_meta", ["headline_small_edit"], NOW + timedelta(hours=1), "same", True),
    ("headline_rewrite", "yahoo", ["headline_rewrite"], NOW + timedelta(hours=1), "same", True),
    ("near_duplicate_below", "google_nvda", ["near_below_a", "near_below_b"], NOW + timedelta(hours=1), "same", True),
    ("near_duplicate_above", "google_meta", ["near_above_a", "near_above_b"], NOW + timedelta(hours=1), "same", True),
    ("redis_fail_open", "google_nvda", ["low_quality"], NOW + timedelta(hours=2), "failing", True),
    ("repeat_after_ttl_expiry", "google_meta", ["meta_reuters"], NOW + TTL + timedelta(hours=2), "same", True),
    ("cross_feed_after_expiry", "google_meta", ["related_cnbc"], NOW + TTL + timedelta(hours=2), "same", True),
    ("repeat_after_restart", "google_nvda", ["low_quality", "stale"], NOW + TTL + timedelta(hours=3), "fresh", True),
]


def run_collector(feed, entries, redis, *, now=NOW, enabled=True, submit=None, telegram_ok=True, ai=fake_ai):
    """One real read_feed pass; returns observable collector outputs and captured shadow submissions."""
    submitted = []

    def capture(value, **kwargs):
        submitted.append((deepcopy(value), kwargs))
        if submit:
            return submit(value, **kwargs)

    def telegram(message):
        if not telegram_ok:
            raise RuntimeError("telegram unavailable")
        return {"ok": True, "result": {"message_id": 1}}

    from persistence import news_shadow
    redis.now = now
    log = Mock()
    parsed = SimpleNamespace(bozo=False, feed={"title": "ignored"}, entries=deepcopy(entries))
    out = StringIO()
    with patch.object(news.feedparser, "parse", return_value=parsed) as parse, \
         patch.object(deduplicator, "redis_client", redis), \
         patch.object(news, "NEWS_PERSISTENCE_SHADOW_ENABLED", enabled), \
         patch.object(news_shadow, "submit_news", side_effect=capture), \
         patch.object(news, "analyze_market_event", side_effect=ai) as analyze, \
         patch.object(news, "send_telegram_alert", side_effect=telegram) as send, \
         patch.object(scoring_engine, "datetime", wraps=datetime) as clock, \
         patch.object(news.logger, "info", log.info), patch.object(news.logger, "error", log.error), \
         patch.object(news.logger, "warning", log.warning), \
         patch.object(deduplicator.logger, "info", log.dedup_info), \
         patch.object(deduplicator.logger, "error", log.dedup_error), redirect_stdout(out):
        clock.now.side_effect = lambda tz=None: now
        events, stats = news.read_feed("https://feeds.example/mias-fixture", source_label=FEEDS[feed])
    # Log arguments as text: exception instances compare by identity, not by content.
    logs = [(name, tuple(str(arg) for arg in args)) for name, args, _ in log.mock_calls]
    outputs = dict(events=events, stats=stats, stdout=out.getvalue(), logs=logs,
                   redis=redis.live() if hasattr(redis, "live") else None,
                   telegram=[c.args for c in send.call_args_list], ai_calls=analyze.call_count,
                   feed_reads=parse.call_count)
    return outputs, submitted


def build_rows():
    redis, rows, outcomes = NewsMemoryRedis(), [], {}
    for label, feed, names, clock, mode, telegram_ok in STEPS:
        if mode == "fresh":
            redis = NewsMemoryRedis()  # New process whose Redis state was lost.
        target = FailingRedis() if mode == "failing" else redis
        outputs, submitted = run_collector(feed, [ENTRIES[n] for n in names], target, now=clock, telegram_ok=telegram_ok)
        outcomes[label] = dict(stats=outputs["stats"], ai_calls=outputs["ai_calls"], telegram=len(outputs["telegram"]),
                               submitted=[e["news_collector_outcome"] for e, _ in submitted],
                               decisions=[e.get("alert_decision") for e, _ in submitted])
        for index, (event, kwargs) in enumerate(submitted):
            rows.append(dict(label=label if index == 0 else f"{label}:{index}", event=event,
                             make_current=kwargs.get("make_current", True)))
    return rows, outcomes


def load_corpus():
    manifest = json.loads(MANIFEST.read_text())
    observed = datetime.fromisoformat(manifest["observed_at"])
    rows, _ = build_rows()
    for index, row in enumerate(rows):
        row["observed_at"] = (observed + timedelta(minutes=index)).isoformat()
        row["expect_current"] = row["label"] not in manifest["expected_not_current"]
    return manifest, rows
