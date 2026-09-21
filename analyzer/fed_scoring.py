"""Deterministic scores for the official monetary-policy feed."""

FED_CATEGORY_SCORES = {
    "policy_statement": 85,
    "policy_action": 85,
    "economic_projections": 80,
    "minutes": 70,
    "policy_communication": 40,
}


def score_fed_event(event):
    category = event.get("fed_category", "policy_communication")
    score = FED_CATEGORY_SCORES.get(category, 40)
    event["impact_score"] = score
    event["impact_level"] = "HIGH" if score >= 70 else "MEDIUM" if score >= 40 else "LOW"
    event["score_reasons"] = [
        "Federal Reserve primary source",
        f"Monetary-policy category: {category} (+{score})",
    ]
    return event
