from datetime import datetime, timezone


HIGH_IMPACT_KEYWORDS = {
    "ai": 15,
    "artificial intelligence": 15,
    "acquisition": 15,
    "acquire": 15,
    "earnings": 15,
    "guidance": 15,
    "buyback": 10,
    "dividend": 10,
    "partnership": 10,
    "launch": 10,
    "regulation": 10,
}


COMPANY_NAMES = {
    "META": ["meta", "facebook"],
    "NVDA": ["nvidia", "nvda"],
}


def calculate_impact_score(event):
    score = 0
    reasons = []

    text = (
        f"{event.get('headline', '')} "
        f"{event.get('summary', '')}"
    ).lower()

    # 1. Direct company mention
    for symbol in event.get("direct_symbols", []):
        score += 40
        reasons.append(f"Direct {symbol} mention")

    for symbol in event.get("related_symbols", []):
        score += 15
        reasons.append(f"Related {symbol} mention")        

    # 2. Important event keywords
    for keyword, points in HIGH_IMPACT_KEYWORDS.items():
        if keyword in text:
            score += points
            reasons.append(f"Keyword: {keyword}")

    # 3. Recent publication bonus
    published_at = event.get("published_at")

    if published_at:
        try:
            published = datetime.fromisoformat(published_at)
            now = datetime.now(timezone.utc)

            age_hours = (now - published).total_seconds() / 3600

            if age_hours <= 6:
                score += 10
                reasons.append("Published within 6 hours")

        except ValueError:
            pass

    # Never exceed 100
    score = min(score, 100)

    if score >= 70:
        level = "HIGH"
    elif score >= 40:
        level = "MEDIUM"
    else:
        level = "LOW"

    event["impact_score"] = score
    event["impact_level"] = level
    event["score_reasons"] = reasons

    return event
