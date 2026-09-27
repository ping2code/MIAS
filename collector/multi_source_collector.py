import argparse

from collector.rss_reader import read_feed
from shared.sources import RSS_SOURCES
from shared.logger import get_logger


logger = get_logger("multi_source_collector")


def collect_all_sources(include_fed=False, *, news_enable_ai=True, news_send_alerts=True,
                        fed_enable_ai=True, fed_send_alerts=False,
                        include_macro=False, macro_enable_ai=True, macro_send_alerts=False,
                        include_treasury=False, treasury_enable_ai=True, treasury_send_alerts=False,
                        include_geopolitical=False, geopolitical_enable_ai=True, geopolitical_send_alerts=False):
    all_events = []

    total_stats = {
        "fetched": 0,
        "relevant": 0,
        "duplicates": 0,
        "processed": 0,
    }

    for source in RSS_SOURCES:

        logger.info(
            "Collecting source: %s",
            source["name"]
        )

        events, stats = read_feed(
            source["url"],
            source_label=source["name"],
            enable_ai=news_enable_ai,
            send_alerts=news_send_alerts,
        )

        all_events.extend(events)

        for key in total_stats:
            total_stats[key] += stats[key]

    if include_fed:
        from collector.fed_collector import collect_fed_events
        events, stats = collect_fed_events(
            enable_ai=fed_enable_ai, send_alerts=fed_send_alerts,
        )
        all_events.extend(events)
        for key in total_stats:
            total_stats[key] += stats[key]

    if include_macro:
        from collector.macro_collector import collect_macro_events
        events, stats = collect_macro_events(
            enable_ai=macro_enable_ai, send_alerts=macro_send_alerts,
        )
        all_events.extend(events)
        for key in total_stats:
            total_stats[key] += stats[key]

    if include_treasury:
        from collector.treasury_collector import collect_treasury_events
        events, stats = collect_treasury_events(enable_ai=treasury_enable_ai, send_alerts=treasury_send_alerts)
        all_events.extend(events)
        for key in total_stats:
            total_stats[key] += stats[key]

    if include_geopolitical:
        from collector.geopolitical_collector import collect_geopolitical_events
        events, stats = collect_geopolitical_events(enable_ai=geopolitical_enable_ai, send_alerts=geopolitical_send_alerts)
        all_events.extend(events)
        for key in total_stats:
            total_stats[key] += stats[key]

    return all_events, total_stats


def parse_args(argv=None):
    """News CLI. No flags keeps today's behavior (AI enrichment and Telegram delivery for ALERT items)."""
    parser = argparse.ArgumentParser(prog="python -m collector.multi_source_collector")
    parser.add_argument("--no-ai", action="store_true", help="skip OpenAI enrichment for news ALERT items")
    parser.add_argument("--no-send-alerts", action="store_true", help="skip Telegram delivery for news ALERT items")
    # Stray positional arguments are ignored, as this entry point always did. Unknown options fail closed, so a
    # mistyped safety flag can never silently fall back to live AI or delivery.
    args, extra = parser.parse_known_args(argv)
    unknown = [arg for arg in extra if arg.startswith("-")]
    if unknown:
        parser.error("unrecognized option(s): " + " ".join(unknown))
    return args


if __name__ == "__main__":

    args = parse_args()
    events, stats = collect_all_sources(news_enable_ai=not args.no_ai, news_send_alerts=not args.no_send_alerts)

    print("\nMIAS MULTI-SOURCE STATS")
    print("=" * 80)

    print(f"Fetched     : {stats['fetched']}")
    print(f"Relevant    : {stats['relevant']}")
    print(f"Duplicates  : {stats['duplicates']}")
    print(f"Processed   : {stats['processed']}")

    print("\nPROCESSED MIAS EVENTS")
    print("=" * 80)

    for event in events:

        if not event.get("relevant"):
            continue

        # A duplicate may have been removed before scoring
        if "alert_decision" not in event:
            continue

        print(f"Source    : {event['source']}")
        print(f"Publisher : {event.get('publisher', 'Unknown')}")
        print(f"Headline  : {event['headline']}")
        print(f"Symbols   : {event['symbols']}")
        print(f"Direct    : {event.get('direct_symbols', [])}")
        print(f"Related   : {event.get('related_symbols', [])}")
        print(f"Impact    : {event['impact_score']}/100")
        print(f"Decision  : {event['alert_decision']}")
        print("-" * 80)
