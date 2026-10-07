"""Pure projections from validated canonical objects (plain dicts) to ``api-v1`` view dicts.

Each view copies fields that already exist in the sealed object, under stable names; the only derived values are
counts of lists the object already holds. No score, rank, confidence, recommendation or "best" selection is produced,
and the canonical object is never modified. The options-intelligence views can also be built from the index's
rebuildable read model (``artifact_store.options_activity``); ``options-intelligence-activity-v1`` adds its descriptive
same-session comparison.
"""
from artifact_store.options_activity import COMPARABLE, compare

VIEW_NAMES = {"market-intelligence": "market-intelligence-summary-v1",
              "options-intelligence": "options-intelligence-summary-v1",
              "trade-setup": "trade-setup-summary-v1", "invalidation-check": "invalidation-check-summary-v1",
              "alert": "alert-summary-v1"}
DELIVERY_VIEW = "alert-deliveries-v1"
ACTIVITY_VIEW = "options-intelligence-activity-v1"
DELIVERY_FIELDS = ("alert_id", "channel", "sequence", "status", "provider_message_id", "attempts", "safe_error_code",
                   "attempted_at", "completed_at", "delivery_contract_version", "render_version")


def market_intelligence(d):
    ref = d["synthesis_ref"]
    return dict(intelligence_id=d["intelligence_id"], intelligence_format_version=d["intelligence_format_version"],
                rules_version=d["rules_version"], symbol=ref["symbol"], as_of=ref["as_of"],
                synthesis_id=ref["synthesis_id"], timeframe_pattern=d["timeframe_structure"]["pattern"],
                technical_status=d["evidence_coverage"]["technical_status"],
                market_context_available=d["market_context_alignment"]["available"],
                conflict_codes=[c["code"] for c in d["conflicts"]],
                attention=[dict(code=a["code"], category=a["category"]) for a in d["attention"]])


def options_intelligence(d):
    ref = d["snapshot_ref"]
    return dict(options_intelligence_id=d["options_intelligence_id"],
                options_intelligence_format_version=d["options_intelligence_format_version"],
                rules_version=d["rules_version"], symbol=ref["underlying"], as_of=ref["as_of"],
                snapshot_id=ref["snapshot_id"], contract_count=len(d["contracts"]))


def options_intelligence_from_summary(artifact_id, s):
    """The options-intelligence-summary-v1 view from the index's read model: identical to ``options_intelligence``
    of the full object (tested), without reading or parsing the stored file."""
    return dict(options_intelligence_id=artifact_id,
                options_intelligence_format_version=s.options_intelligence_format_version,
                rules_version=s.rules_version, symbol=s.symbol, as_of=s.as_of, snapshot_id=s.snapshot_id,
                contract_count=s.contract_count)


def options_activity(artifact_id, s, prior_status, prior_id, prior_summary):
    """options-intelligence-activity-v1 from read models only (see artifact_store.options_activity)."""
    derived = compare(s, prior_summary, prior_status)
    derived["comparison"]["prior_options_intelligence_id"] = (
        prior_id if derived["comparison"]["status"] == COMPARABLE else None)
    return dict(options_intelligence_id=artifact_id, symbol=s.symbol, as_of=s.as_of,
                contract_count=s.contract_count, expiration_count=s.expiration_count,
                call_volume=s.call_volume, put_volume=s.put_volume, put_call_volume_ratio=s.put_call_volume_ratio,
                put_call_volume_ratio_reason=s.put_call_volume_ratio_reason, iv_median=s.iv_median,
                current_session_volume_gt_oi_count=s.current_session_volume_gt_oi_count, call_breadth=s.call_breadth, put_breadth=s.put_breadth,
                call_concentration=s.call_concentration, put_concentration=s.put_concentration,
                concentration_reason=s.concentration_reason, **derived)


def trade_setup(d):
    inputs, provenance = d["inputs"], d["provenance"]
    return dict(assessment_id=d["assessment_id"], assessment_format_version=d["assessment_format_version"],
                rules_version=d["rules_version"], symbol=inputs["symbol"], as_of=inputs["assessment_as_of"],
                outcome_status=d["outcome"]["status"], no_setup_reasons=list(d["outcome"]["no_setup_reasons"]),
                market_bias_state=d["market_bias"]["state"], eligible_side=d["market_bias"]["side"],
                candidate_count=len(d["candidates"]), market_intelligence_id=provenance["market_intelligence_id"],
                options_intelligence_id=provenance["options_intelligence_id"], policy_id=provenance["policy_id"])


def invalidation_check(d):
    setup, mi_ref = d["setup_ref"], d["market_intelligence_ref"]
    return dict(invalidation_id=d["invalidation_id"], invalidation_format_version=d["invalidation_format_version"],
                rules_version=d["rules_version"], symbol=d["symbol"], as_of=mi_ref["as_of"], result=d["result"],
                reason=d["reason"], assessment_id=setup["assessment_id"], side=setup["side"],
                required_pattern=d["required_market_state"]["required_pattern"],
                observed_pattern=d["observed_market_state"]["pattern"],
                observed_technical_status=d["observed_market_state"]["technical_status"],
                market_intelligence_id=mi_ref["intelligence_id"])


def alert(d):
    subject = d["subject"]
    return dict(alert_id=d["alert_id"], alert_format_version=d["alert_format_version"],
                rules_version=d["rules_version"],
                alert_code=d["alert_code"], symbol=subject["symbol"], subject_kind=subject["kind"],
                assessment_id=subject["assessment_id"], transition=d["transition"], as_of=d["as_of"],
                facts=dict(d["facts"]),
                source_refs=[dict(role=s["role"], object_kind=s["object_kind"], id=s["id"]) for s in d["source_refs"]])


def delivery(receipt):
    """Only the closed, secret-free receipt fields (the receipt format already excludes tokens and payloads)."""
    return {k: receipt[k] for k in DELIVERY_FIELDS}


PROJECTIONS = {"market-intelligence": market_intelligence, "options-intelligence": options_intelligence,
               "trade-setup": trade_setup, "invalidation-check": invalidation_check, "alert": alert}
