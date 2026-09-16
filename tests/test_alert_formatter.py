from alert_engine.decision_engine import evaluate_alert
from alert_engine.formatter import format_alert


event = {
    "symbols": ["NVDA"],
    "impact_score": 85,
    "impact_level": "HIGH",
    "headline": "Test NVIDIA AI announcement",
    "published_at": "2026-09-16T16:30:00+00:00",
    "source": "MIAS Test Source",
    "score_reasons": [
        "Direct NVDA mention",
        "Keyword: ai",
        "Published within 6 hours",
    ],
    "url": "https://example.com/test",
}


event = evaluate_alert(event)

print(format_alert(event))
