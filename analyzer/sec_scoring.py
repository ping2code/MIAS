SEC_FORM_SCORES = {
    "8-K": 45,
    "10-Q": 50,
    "10-K": 55,
    "6-K": 45,
    "20-F": 55,
    "4": 20,
    "3": 15,
    "5": 15,
    "144": 15,
    "N-PX": 10,
}


def score_sec_event(event):
    form = event.get("sec_form", "")

    score = SEC_FORM_SCORES.get(form, 10)

    reasons = [
        f"SEC primary source",
        f"SEC Form {form}",
    ]

    # Direct watchlist company
    if event.get("direct_symbols"):
        score += 25
        reasons.append(
            f"Direct {event['direct_symbols'][0]} filing"
        )

    # Cap score
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
