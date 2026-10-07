/**
 * mias-api response shapes, copied from the Phase 13 locked contract (docs/phase13e-api-contract.md §17).
 * No field is invented here; optional/nullable fields follow the API exactly.
 */

export type ArtifactFamily =
  | "market-intelligence"
  | "options-intelligence"
  | "trade-setups"
  | "invalidation-checks"
  | "alerts";

export const ARTIFACT_FAMILIES: readonly ArtifactFamily[] = [
  "market-intelligence",
  "options-intelligence",
  "trade-setups",
  "invalidation-checks",
  "alerts",
];

export interface LivenessView {
  status: "live";
}

export interface CheckView {
  name: string;
  status: "pass" | "fail";
}

export interface ReadinessView {
  status: "ready" | "not_ready";
  checks: CheckView[];
}

export interface AnalyticalFormat {
  object: string;
  format_version: string;
  rules_version: string;
}

export interface VersionView {
  service: string;
  api_version: string;
  build: string;
  analytical_formats: AnalyticalFormat[];
}

export interface Meta {
  api_version: string;
  view: string;
  request_id: string;
  served_at: string;
}

export interface ListMeta extends Meta {
  limit: number;
  next_cursor: string | null;
}

export interface AttentionItem {
  code: string;
  category: string;
}

export interface MarketIntelligenceView {
  intelligence_id: string;
  intelligence_format_version: string;
  rules_version: string;
  symbol: string;
  as_of: string;
  synthesis_id: string;
  timeframe_pattern: string;
  technical_status: string;
  market_context_available: boolean;
  conflict_codes: string[];
  attention: AttentionItem[];
}

export interface OptionsIntelligenceView {
  options_intelligence_id: string;
  options_intelligence_format_version: string;
  rules_version: string;
  symbol: string;
  as_of: string;
  snapshot_id: string;
  contract_count: number;
}

/** `options-intelligence-activity-v1` (derived, descriptive): GET /api/v1/options-intelligence/activity. */
export interface ActivityComparison {
  status: "comparable" | "no_prior_snapshot" | "prior_from_other_session" | "prior_ambiguous";
  prior_options_intelligence_id: string | null;
  prior_as_of: string | null;
  session_date: string;
}

export type ActivitySide = "CALL" | "PUT" | "BALANCED" | "UNAVAILABLE";
export type ActivityMomentum = ActivitySide | "INSUFFICIENT_PRIOR" | "VOLUME_CORRECTION";
export type ActivityConcentration = "below_spot" | "at_spot" | "above_spot" | "mixed" | "unavailable";
export type ChangeReason = "not_comparable" | "value_unavailable" | "prior_zero" | null;

export interface OptionsIntelligenceActivityView {
  options_intelligence_id: string;
  symbol: string;
  as_of: string;
  contract_count: number;
  expiration_count: number;
  call_volume: number | null;
  put_volume: number | null;
  put_call_volume_ratio: string | null;
  put_call_volume_ratio_reason: string | null;
  iv_median: string | null;
  volume_gt_oi_count: number;
  call_breadth: number;
  put_breadth: number;
  call_concentration: ActivityConcentration;
  put_concentration: ActivityConcentration;
  concentration_reason: string | null;
  comparison: ActivityComparison;
  call_volume_change: number | null;
  call_volume_change_pct: string | null;
  call_volume_change_reason: ChangeReason;
  put_volume_change: number | null;
  put_volume_change_pct: string | null;
  put_volume_change_reason: ChangeReason;
  volume_gt_oi_change: number | null;
  call_breadth_change: number | null;
  put_breadth_change: number | null;
  activity_bias: ActivitySide;
  momentum_15m: ActivityMomentum;
  trend_summary: string;
}

export interface TradeSetupView {
  assessment_id: string;
  assessment_format_version: string;
  rules_version: string;
  symbol: string;
  as_of: string;
  outcome_status: string;
  no_setup_reasons: string[];
  market_bias_state: string;
  eligible_side: string | null;
  candidate_count: number;
  market_intelligence_id: string;
  options_intelligence_id: string;
  policy_id: string;
}

export interface InvalidationCheckView {
  invalidation_id: string;
  invalidation_format_version: string;
  rules_version: string;
  symbol: string;
  as_of: string;
  result: string;
  reason: string;
  assessment_id: string;
  side: string;
  required_pattern: string;
  observed_pattern: string;
  observed_technical_status: string;
  market_intelligence_id: string;
}

export interface Transition {
  previous: string;
  current: string;
}

export interface SourceRefView {
  role: string;
  object_kind: string;
  id: string;
}

export interface AlertView {
  alert_id: string;
  alert_format_version: string;
  rules_version: string;
  alert_code: string;
  symbol: string;
  subject_kind: string;
  assessment_id: string | null;
  transition: Transition | null;
  as_of: string;
  facts: Record<string, unknown>;
  source_refs: SourceRefView[];
}

export interface FamilyViews {
  "market-intelligence": MarketIntelligenceView;
  "options-intelligence": OptionsIntelligenceView;
  "trade-setups": TradeSetupView;
  "invalidation-checks": InvalidationCheckView;
  alerts: AlertView;
}

/** The id field of each family's view. */
export const ID_FIELD = {
  "market-intelligence": "intelligence_id",
  "options-intelligence": "options_intelligence_id",
  "trade-setups": "assessment_id",
  "invalidation-checks": "invalidation_id",
  alerts: "alert_id",
} as const satisfies { [F in ArtifactFamily]: keyof FamilyViews[F] };

export interface ItemResponse<T> {
  data: T;
  meta: Meta;
}

export interface ListResponse<T> {
  data: T[];
  meta: ListMeta;
}

export interface HistoryParams {
  symbol?: string;
  asOfFrom?: string;
  asOfTo?: string;
  limit?: number;
  cursor?: string;
}

/** Exact canonical bytes as text, plus what the API said about them. */
export interface CanonicalArtifact {
  text: string;
  etag: string | null;
  cacheControl: string | null;
}

/** Every successful call reports the request id the API echoed. */
export interface ApiResult<T> {
  value: T;
  requestId: string | null;
  status: number;
}

/** One Phase 12E delivery receipt (view `alert-deliveries-v1`), as returned; never inferred by the UI. */
export interface DeliveryView {
  alert_id: string;
  channel: string;
  sequence: number;
  status: "delivered" | "failed";
  provider_message_id: string | null;
  attempts: number;
  safe_error_code: string | null;
  attempted_at: string;
  completed_at: string;
  delivery_contract_version: string;
  render_version: string;
}

export interface DeliveryListResponse {
  data: DeliveryView[];
  meta: Meta;
}
