SOURCE_QUALITY = {
    "Reuters": 15,
    "Bloomberg": 15,
    "Wall Street Journal": 15,
    "WSJ": 15,
    "Barron's": 12,
    "Yahoo Finance": 10,
    "CNBC": 10,
    "Investing.com": 8,
    "The Motley Fool": 6,
    "24/7 Wall St.": 5,
    "MarketBeat": 5,
}


def get_source_quality_score(publisher):
    if not publisher:
        return 0

    publisher_lower = publisher.lower()

    for source_name, score in SOURCE_QUALITY.items():
        if source_name.lower() in publisher_lower:
            return score

    return 3
