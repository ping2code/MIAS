"""Deterministic importance of official statistical releases, not direction."""

MACRO_SCORES = {"cpi": 90, "ppi": 80, "employment": 90, "pce": 90, "retail_sales": 80}
GDP_SCORES = {"advance": 85, "second": 70, "third": 70}


def score_macro_event(event):
    category = event["release_category"]
    score = (GDP_SCORES[event["release_stage"]] if category == "gdp" else MACRO_SCORES[category])
    event.update(
        impact_score=score, original_impact_score=score, quality_adjustment=0,
        impact_level="HIGH" if score >= 70 else "MEDIUM" if score >= 40 else "LOW",
        score_reasons=[f"Official {event['agency'].upper()} statistical release",
                       f"{category}: {event['release_stage']} (+{score})",
                       "Opinion/prediction penalties do not apply to official statistics"],
    )
    return event
