def format_alert(event):
    symbols = ", ".join(event.get("symbols", []))

    message = (
        "\n"
        "================================================\n"
        "MIAS MARKET ALERT\n"
        "================================================\n"
        f"Ticker     : {symbols}\n"
        f"Impact     : {event.get('impact_score', 0)}/100\n"
        f"Level      : {event.get('impact_level', 'UNKNOWN')}\n"
        f"Decision   : {event.get('alert_decision', 'UNKNOWN')}\n"
        f"Headline   : {event.get('headline', 'N/A')}\n"
        f"Published  : {event.get('published_at', 'N/A')}\n"
        f"Source     : {event.get('source', 'N/A')}\n"
        f"Reasons    : {', '.join(event.get('score_reasons', []))}\n"
        f"URL        : {event.get('url', 'N/A')}\n"
        "================================================\n"
    )

    return message
