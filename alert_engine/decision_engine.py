from shared.config import (
    ALERT_THRESHOLD,
    DISPLAY_THRESHOLD,
)



def evaluate_alert(event):
    score = event.get("impact_score", 0)

    if score >= ALERT_THRESHOLD:
        decision = "ALERT"
    
    elif score >= DISPLAY_THRESHOLD:
        decision = "DISPLAY_ONLY"

    else:
        decision = "IGNORE"

    event["alert_decision"] = decision

    return event
