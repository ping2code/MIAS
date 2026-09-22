"""Deterministic Treasury importance; never AI opinion/prediction penalties."""

SCORES = {"debt_limit": 95, "extraordinary_measures": 90, "quarterly_refunding": 85,
          "issuance_policy": 85, "borrowing_estimates": 80, "material_press_release": 80,
          "auction_announcement": 40, "routine_release": 10}


def score_treasury_event(event, *, yield_threshold_bps=15):
    category = event["treasury_category"]
    if category == "auction_result":
        score = {"Note": 70, "Bond": 70, "TIPS": 60, "FRN": 60, "Bill": 40}[event["security_type"]]
    elif category == "yield_curve":
        metrics = event["metrics"]
        eligible_move = (metrics.get("prior_observation_date") and
                         any(abs(v) >= yield_threshold_bps for v in metrics.get("movement_bps", {}).values()))
        score = 70 if eligible_move else 30
    else:
        score = SCORES[category]
    event.update(impact_score=score, original_impact_score=score, quality_adjustment=0,
                 impact_level="HIGH" if score >= 70 else "MEDIUM" if score >= 40 else "LOW",
                 score_reasons=[f"Official Treasury {category}: {score}", "Official releases/data receive no AI quality penalty"])
    return event
