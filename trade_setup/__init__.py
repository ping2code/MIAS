"""Trade Setup (Phase 10): a deterministic TradeSetupAssessment from sealed MarketIntelligence and OptionsIntelligence
plus an explicit, required policy.

Phase 10B implements the policy, structural input validation, input compatibility, market bias, directional
eligibility, the no-setup model and the assessment shell. Phase 10C implements contract screening
(``builder.assess``). Phase 10D implements the separate point-in-time InvalidationCheck
(``invalidation.check_invalidation``). Phase 10E adds ``runner``, the only module that performs I/O (local files).
Long single-leg calls and puts only; no ranking, sizing, targets, stops or reward/risk. See docs/phase10-overview.md.
"""
