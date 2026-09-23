"""Policy importance is independent of sentiment; official quality adjustment is zero."""

SCORES = {"supply_disruption": 95, "export_controls": 90, "entity_list": 85,
          "sanctions": 85, "investment_restriction": 85, "trade_restriction": 85,
          "regulatory_remedy": 80, "routine": 10}


def score_geopolitical_event(event):
    score = SCORES[event["geopolitical_category"]] if event["relevant"] else 10
    if event["policy_stage"] == "proposed":
        score = min(score, 60)
    elif event["policy_stage"] == "complaint":
        score = min(score, 75)
    event.update(impact_score=score, original_impact_score=score, quality_adjustment=0,
                 impact_level="HIGH" if score >= 70 else "MEDIUM" if score >= 40 else "LOW",
                 score_reasons=[f"Official {event['geopolitical_category']}: {score}",
                                "Importance only; no directional inference or AI quality penalty"])
    return event
