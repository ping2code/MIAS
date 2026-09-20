from dotenv import load_dotenv
from openai import OpenAI
import json

load_dotenv()

client = OpenAI()


def analyze_market_event(event):
    headline = event.get("headline", "")
    summary = event.get("summary", "")
    symbols = event.get("symbols", [])

    prompt = f"""
Analyze this market event.

Symbols: {symbols}
Headline: {headline}
Summary: {summary}

Return ONLY valid JSON with exactly these fields:

{{
  "summary": "one short sentence",
  "sentiment": "STRONGLY_BULLISH|BULLISH|NEUTRAL|BEARISH|STRONGLY_BEARISH",
  "confidence": 0,
  "why_it_matters": "one short sentence",
  "event_type": "short category"
}}

Confidence must be an integer from 0 to 100.
"""

    response = client.responses.create(
        model="gpt-5.6",
        input=prompt,
    )

    result = json.loads(response.output_text)

    event["ai_summary"] = result["summary"]
    event["ai_sentiment"] = result["sentiment"]
    event["ai_confidence"] = result["confidence"]
    event["ai_why_it_matters"] = result["why_it_matters"]
    event["ai_event_type"] = result["event_type"]

    return event
