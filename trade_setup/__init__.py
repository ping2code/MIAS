"""Trade Setup (Phase 10): a deterministic TradeSetupAssessment from sealed MarketIntelligence and OptionsIntelligence
plus an explicit, required policy.

Phase 10B implements the policy, structural input validation, input compatibility, market bias, directional
eligibility, the no-setup model and the assessment shell. Contract screening (10C), premium risk and invalidation
evaluation (10D) and the runner (10E) are not implemented yet. Long single-leg calls and puts only; no ranking,
sizing, targets, stops or reward/risk. See docs/phase10b-trade-setup-core.md.
"""
