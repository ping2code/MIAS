from collector.rss_reader import read_feed
from shared.sources import RSS_SOURCES
from shared.logger import get_logger


logger = get_logger("multi_source_collector")


def collect_all_sources():
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
            source_label=source["name"]
        )

        all_events.extend(events)

        for key in total_stats:
            total_stats[key] += stats[key]

    return all_events, total_stats


if __name__ == "__main__":

    events, stats = collect_all_sources()

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
