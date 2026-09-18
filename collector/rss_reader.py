import sys
from pathlib import Path

import feedparser

# Allow collector code to import modules from the MIAS project root
sys.path.append(str(Path(__file__).resolve().parent.parent))

from analyzer.scoring_engine import calculate_impact_score
from alert_engine.decision_engine import evaluate_alert
from alert_engine.formatter import format_alert
from analyzer.relevance_detector import detect_symbols
from normalizer import normalize_entry
from analyzer.deduplicator import is_duplicate
from alert_engine.telegram_notifier import send_telegram_alert
from shared.logger import get_logger
from shared.config import RSS_ENTRY_LIMIT

logger = get_logger("collector")

def read_feed(feed_url):
    feed = feedparser.parse(feed_url)

    source_name = feed.feed.get("title", "Unknown Feed")
    
    logger.info(
        "Feed loaded: %s", 
        source_name
    )

    events = []

    for entry in feed.entries[:RSS_ENTRY_LIMIT]:    

        event = normalize_entry(
            entry,
            source_name=source_name
        )

        event = detect_symbols(event)

        if event["relevant"]:

            if is_duplicate(event):
                logger.info(
                    "Duplicate skipped: %s",
                    event["headline"]
             )    
                continue            

            event = calculate_impact_score(event)
            event = evaluate_alert(event)

            logger.info(
                "Processed event symbol=%s score=%s decision=%s",
                event.get("symbols"),
                event.get("impact_score"),
                event.get("alert_decision")
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

        events.append(event)

    return events


if __name__ == "__main__":

    feed_url = (
        "https://feeds.finance.yahoo.com/rss/2.0/"
        "headline?s=META,NVDA&region=US&lang=en-US"
    )

    events = read_feed(feed_url)

    # Only allow relevant META/NVDA events to continue
    relevant_events = [
        event
        for event in events
        if event["relevant"]
    ]

    print(
        f"\nCollected: {len(events)} | "
        f"Relevant: {len(relevant_events)}"
    )

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

