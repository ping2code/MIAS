QUALITY_PENALTIES = {
    "analyst commentary": -10,
    "prediction": -15,
    "prediction article": -15,
    "stock pick": -15,
    "stock-pick article": -15,
    "opinion": -10,
    "opinion article": -10,
    "comparison": -8,
    "ai industry milestone": -5,
}

NO_PENALTY_TYPES = {
    "earnings",
    "guidance",
    "sec filing",
    "official announcement",
    "partnership",
    "strategic partnership",
    "acquisition",
    "merger",
    "regulatory action",
    "product launch",
}


def adjust_alert_quality(event):
    original_score = event.get("impact_score", 0)

    event_type = (
        event.get("ai_event_type", "")
        .strip()
        .lower()
    )

    adjustment = 0
    reason = None

    if event_type in NO_PENALTY_TYPES:
        adjustment = 0

    else:
        for category, penalty in QUALITY_PENALTIES.items():
            if category in event_type:
                adjustment = penalty
                reason = (
                    f"AI quality adjustment: "
                    f"{event_type} ({penalty})"
                )
                break

    adjusted_score = max(
        0,
        min(100, original_score + adjustment)
    )

    event["original_impact_score"] = original_score
    event["quality_adjustment"] = adjustment
    event["impact_score"] = adjusted_score

    if reason:
        event.setdefault(
            "score_reasons",
            []
        ).append(reason)

    if adjusted_score >= 70:
        event["impact_level"] = "HIGH"
    elif adjusted_score >= 40:
        event["impact_level"] = "MEDIUM"
    else:
        event["impact_level"] = "LOW"

    return event
