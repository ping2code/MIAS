def format_alert(event):
    symbols = ", ".join(event.get("symbols", [])) or "N/A"

    lines = [
        "",
        "================================================",
        "MIAS MARKET ALERT",
        "================================================",
        f"Ticker         : {symbols}",
        f"Impact         : {event.get('impact_score', 0)}/100",
        f"Level          : {event.get('impact_level', 'UNKNOWN')}",
        f"Decision       : {event.get('alert_decision', 'UNKNOWN')}",
        f"Headline       : {event.get('headline', 'N/A')}",
        f"Published      : {event.get('published_at', 'N/A')}",
        f"Source         : {event.get('source', 'N/A')}",
    ]

    if "market_scope" in event:
        lines.append(f"Market Scope   : {event['market_scope']}")

    if event.get("ai_summary"):
        lines.append(
            f"AI Summary     : {event['ai_summary']}"
        )

    if event.get("ai_sentiment"):
        lines.append(
            f"Sentiment      : {event['ai_sentiment']}"
        )

    if event.get("ai_confidence") is not None:
        lines.append(
            f"Confidence     : {event['ai_confidence']}%"
        )

    if event.get("ai_why_it_matters"):
        lines.append(
            f"Why it matters : {event['ai_why_it_matters']}"
        )

    if event.get("ai_event_type"):
        lines.append(
            f"Event Type     : {event['ai_event_type']}"
        )

    lines.append(
        f"Reasons        : {', '.join(event.get('score_reasons', []))}"
    )

    lines.append(
        f"URL            : {event.get('url', 'N/A')}"
    )

    lines.append(
        "================================================"
    )

    return "\n".join(lines)
