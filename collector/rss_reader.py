from time import monotonic

import feedparser

from analyzer.scoring_engine import calculate_impact_score
from alert_engine.decision_engine import evaluate_alert
from analyzer.openai_analyzer import analyze_market_event
from analyzer.ai_alert_quality import adjust_alert_quality
from alert_engine.formatter import format_alert
from analyzer.relevance_detector import detect_symbols
from collector.normalizer import normalize_entry

from analyzer import deduplicator
from analyzer.deduplicator import (
    is_duplicate,
    is_near_duplicate_headline,
)

from alert_engine.telegram_notifier import send_telegram_alert
from shared.logger import get_logger
from shared.config import RSS_ENTRY_LIMIT, NEWS_PERSISTENCE_SHADOW_ENABLED


logger = get_logger("collector")
_shadow_last_failure = float("-inf")


def _shadow(event, outcome):
    """Opt-in historical copy of the final collector state; never touches Redis, AI or delivery."""
    global _shadow_last_failure
    if not NEWS_PERSISTENCE_SHADOW_ENABLED:
        return
    try:
        from persistence.news_shadow import submit_news
        submit_news(dict(event, news_fingerprint=deduplicator.create_fingerprint(event),
                         news_collector_outcome=outcome))
    except Exception:
        # Even import/initialization/enqueue failures cannot change collector outcomes.
        now = monotonic()
        if now - _shadow_last_failure >= 60:
            _shadow_last_failure = now
            logger.warning("News shadow submission failed")


def read_feed(feed_url, source_label=None):

    stats = {
        "fetched": 0,
        "relevant": 0,
        "duplicates": 0,
        "processed": 0,
    }

    try:
        feed = feedparser.parse(feed_url)

    except Exception as error:
        logger.error(
            "RSS fetch failed for %s: %s",
            feed_url,
            error
        )
        return [], stats

    if feed.bozo:
        logger.warning(
            "RSS feed parse warning for %s: %s",
            feed_url,
            feed.bozo_exception
        )

    source_name = (
        source_label
        or feed.feed.get("title", "Unknown Feed")

    )    

    logger.info(
        "Feed loaded: %s",
        source_name
    )

    if not feed.entries:
        logger.warning(
            "RSS feed returned no entries: %s",
            feed_url
        )
        return [], stats

    events = []

    for entry in feed.entries[:RSS_ENTRY_LIMIT]:

        stats["fetched"] += 1

        event = normalize_entry(
            entry,
            source_name=source_name
        )

        event = detect_symbols(event)

        if event["relevant"]:
            stats["relevant"] += 1

            if is_duplicate(event, namespace="news:event"):
                stats["duplicates"] += 1

                logger.info(
                    "Duplicate skipped: %s",
                    event["headline"]
                )

                continue

            if is_near_duplicate_headline(event):
                stats["duplicates"] += 1
                _shadow(event, "near_duplicate_suppressed")
                continue

            event = calculate_impact_score(event)
            event = evaluate_alert(event)

            stats["processed"] += 1

            logger.info(
                "Processed event symbol=%s score=%s decision=%s",
                event.get("symbols"),
                event.get("impact_score"),
                event.get("alert_decision")
            )

            if event["alert_decision"] == "ALERT":

                try:
                    event = analyze_market_event(event)

                    event = adjust_alert_quality(event)
                    event = evaluate_alert(event)

                    logger.info(
                        "OpenAI analysis completed "
                        "sentiment=%s confidence=%s "
                        "event_type=%s original_score=%s "
                        "adjustment=%s adjusted_score=%s decision=%s",
                        event.get("ai_sentiment"),
                        event.get("ai_confidence"),
                        event.get("ai_event_type"),
                        event.get("original_impact_score"),
                        event.get("quality_adjustment"),
                        event.get("impact_score"),
                        event.get("alert_decision"),
                    )    

                except Exception as error:
                    logger.error(
                        "OpenAI analysis failed: %s",
                        error
                    )


            if event["alert_decision"] == "ALERT":
                message = format_alert(event)

                print(message)

                try:
                    result = send_telegram_alert(message)

                    logger.info(
                        "Telegram alert sent message_id=%s",
                        result["result"]["message_id"]
                    )

                except Exception as error:
                    logger.error(
                        "Telegram alert failed: %s",
                        error
                    )

            _shadow(event, "processed")

        events.append(event)

    return events, stats


if __name__ == "__main__":

    feed_url = (
        "https://feeds.finance.yahoo.com/rss/2.0/"
        "headline?s=META,NVDA&region=US&lang=en-US"
    )

    events, stats = read_feed(feed_url)

    relevant_events = [
        event
        for event in events
        if event["relevant"]
    ]

    print("\nMIAS PIPELINE STATS")
    print("=" * 80)

    print(f"Fetched     : {stats['fetched']}")
    print(f"Relevant    : {stats['relevant']}")
    print(f"Duplicates  : {stats['duplicates']}")
    print(f"Processed   : {stats['processed']}")

    print("\nRELEVANT MIAS EVENTS")
    print("=" * 80)

    for event in relevant_events:
        print(f"Decision  : {event['alert_decision']}")
        print(f"Source    : {event['source']}")
        print(f"Headline  : {event['headline']}")
        print(f"Symbols   : {event['symbols']}")
        print(f"Direct    : {event.get('direct_symbols', [])}")
        print(f"Related   : {event.get('related_symbols', [])}")
        print(f"Published : {event['published_at']}")
        print(f"Impact    : {event['impact_score']}/100")
        print(f"Level     : {event['impact_level']}")
        print(f"Reasons   : {event['score_reasons']}")
        print(f"URL       : {event['url']}")
        print("-" * 80)
