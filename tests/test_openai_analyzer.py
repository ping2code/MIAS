from analyzer.openai_analyzer import analyze_market_event

event = {
    "headline": "NVIDIA announces major AI infrastructure partnership",
    "summary": (
        "NVIDIA announced a new partnership aimed at expanding "
        "AI data center infrastructure."
    ),
    "symbols": ["NVDA"],
}

event = analyze_market_event(event)

print("Summary        :", event["ai_summary"])
print("Sentiment      :", event["ai_sentiment"])
print("Confidence     :", event["ai_confidence"])
print("Why it matters :", event["ai_why_it_matters"])
print("Event type     :", event["ai_event_type"])
