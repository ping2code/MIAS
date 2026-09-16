def evaluate_alert(event):
    score = event.get("impact_score", 0)

    if score >= 70:
        decision = "ALERT"
    elif score >= 40:
        decision = "DISPLAY_ONLY"
    else:
        decision = "IGNORE"

    event["alert_decision"] = decision

    return event
