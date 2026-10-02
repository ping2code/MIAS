"""API response views (``api-v1``). These describe HTTP responses only; canonical analytical objects are never
modelled or re-serialized here (``/canonical`` routes return stored bytes untouched)."""
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict


class _View(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ErrorDetail(_View):
    code: str
    message: str
    request_id: str


class ErrorView(_View):
    error: ErrorDetail


class LivenessView(_View):
    status: Literal["live"]


class CheckView(_View):
    name: str
    status: Literal["pass", "fail"]


class ReadinessView(_View):
    status: Literal["ready", "not_ready"]
    checks: list[CheckView]


class AnalyticalFormat(_View):
    object: str
    format_version: str
    rules_version: str


class VersionView(_View):
    service: str
    api_version: str
    build: str
    analytical_formats: list[AnalyticalFormat]


# --- Phase 13C read views (projections of existing validated fields only; no scores, ranks or judgments) ---


class Meta(_View):
    api_version: str
    view: str
    request_id: str
    served_at: str


class ListMeta(Meta):
    limit: int
    next_cursor: Optional[str]


class AttentionItem(_View):
    code: str
    category: str


class MarketIntelligenceView(_View):
    intelligence_id: str
    intelligence_format_version: str
    rules_version: str
    symbol: str
    as_of: str
    synthesis_id: str
    timeframe_pattern: str
    technical_status: str
    market_context_available: bool
    conflict_codes: list[str]
    attention: list[AttentionItem]


class OptionsIntelligenceView(_View):
    options_intelligence_id: str
    options_intelligence_format_version: str
    rules_version: str
    symbol: str
    as_of: str
    snapshot_id: str
    contract_count: int


class TradeSetupView(_View):
    assessment_id: str
    assessment_format_version: str
    rules_version: str
    symbol: str
    as_of: str
    outcome_status: str
    no_setup_reasons: list[str]
    market_bias_state: str
    eligible_side: Optional[str]
    candidate_count: int
    market_intelligence_id: str
    options_intelligence_id: str
    policy_id: str


class InvalidationCheckView(_View):
    invalidation_id: str
    invalidation_format_version: str
    rules_version: str
    symbol: str
    as_of: str
    result: str
    reason: str
    assessment_id: str
    side: str
    required_pattern: str
    observed_pattern: str
    observed_technical_status: str
    market_intelligence_id: str


class Transition(_View):
    previous: str
    current: str


class SourceRefView(_View):
    role: str
    object_kind: str
    id: str


class AlertView(_View):
    alert_id: str
    alert_format_version: str
    rules_version: str
    alert_code: str
    symbol: str
    subject_kind: str
    assessment_id: Optional[str]
    transition: Optional[Transition]
    as_of: str
    facts: dict[str, Any]
    source_refs: list[SourceRefView]


class DeliveryView(_View):
    alert_id: str
    channel: str
    sequence: int
    status: Literal["delivered", "failed"]
    provider_message_id: Optional[str]
    attempts: int
    safe_error_code: Optional[str]
    attempted_at: str
    completed_at: str
    delivery_contract_version: str
    render_version: str


def _envelope(item_model, name):
    return type(name, (_View,), {"__annotations__": {"data": item_model, "meta": Meta}})


def _list_envelope(item_model, name):
    return type(name, (_View,), {"__annotations__": {"data": list[item_model], "meta": ListMeta}})


MarketIntelligenceResponse = _envelope(MarketIntelligenceView, "MarketIntelligenceResponse")
MarketIntelligenceList = _list_envelope(MarketIntelligenceView, "MarketIntelligenceList")
OptionsIntelligenceResponse = _envelope(OptionsIntelligenceView, "OptionsIntelligenceResponse")
OptionsIntelligenceList = _list_envelope(OptionsIntelligenceView, "OptionsIntelligenceList")
TradeSetupResponse = _envelope(TradeSetupView, "TradeSetupResponse")
TradeSetupList = _list_envelope(TradeSetupView, "TradeSetupList")
InvalidationCheckResponse = _envelope(InvalidationCheckView, "InvalidationCheckResponse")
InvalidationCheckList = _list_envelope(InvalidationCheckView, "InvalidationCheckList")
AlertResponse = _envelope(AlertView, "AlertResponse")
AlertList = _list_envelope(AlertView, "AlertList")
DeliveryList = type("DeliveryList", (_View,), {"__annotations__": {"data": list[DeliveryView], "meta": Meta}})
