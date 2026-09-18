from alert_engine.decision_engine import evaluate_alert
from alert_engine.formatter import format_alert
from alert_engine.telegram_notifier import send_telegram_alert


event = {
    "symbols": ["NVDA"],
    "direct_symbols": ["NVDA"],
    "related_symbols": [],
    "impact_score": 85,
    "impact_level": "HIGH",
    "headline": "MIAS end-to-end Telegram integration test",
    "published_at": "2026-09-17T21:30:00-05:00",
    "source": "MIAS Test",
    "score_reasons": [
        "Direct NVDA mention",
        "Keyword: ai",
        "High-impact integration test",
    ],
    "url": "https://example.com/mias-test",
}

event = evaluate_alert(event)

if event["alert_decision"] == "ALERT":
    message = format_alert(event)

    print(message)

    try:
        result = send_telegram_alert(message)
        print("Telegram : SENT")
        print(f"Message ID: {result['result']['message_id']}")
    except Exception as error:
        print(f"Telegram : FAILED - {error}")
