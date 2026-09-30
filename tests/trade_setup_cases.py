"""Phase 10B test inputs (test-only): policies, sealed MarketIntelligence and OptionsIntelligence objects.

Upstream packages are imported here, in tests only, to build real sealed inputs; trade_setup itself never imports
them. Every policy below is fully explicit (the engine has no defaults).
"""
from functools import lru_cache

from tests import market_intelligence_cases as mi_cases
from tests import options_intelligence_cases as oi_cases
from tests.options_snapshot_cases import PROVENANCE, chain_record, full_snapshot, live_snapshot
from trade_setup.policy import make_policy

MI_AS_OF_GAP = 585600  # 2026-09-23T20:05Z (Phase 8 goldens) to 2026-09-30T14:45Z (Phase 9 snapshots)

BASE_POLICY = dict(
    allowed_sides=["call", "put"], min_dte=0, max_dte=60, allow_same_day_expiry=False, allow_locked_quote=False,
    max_spread_relative="0.1", abs_delta_min="0.2", abs_delta_max="0.7", min_volume=None, min_open_interest=None,
    max_premium_per_contract=None, require_iv=True, allow_unverified_time_basis=False,
    require_current_session_day=False, require_complete_chain=False, max_input_gap_seconds=10 ** 7,
    block_on_market_context_opposition=False, block_on_market_context_not_current=False)


def policy(**overrides):
    return make_policy(**dict(BASE_POLICY, **overrides))


@lru_cache(maxsize=None)
def market_intelligence(name):
    """A real, sealed Phase 8 MarketIntelligence (as a plain dict) for a Phase 8 golden case."""
    from market_intelligence.builder import build
    if name == "bullish_current_no_opposition":
        return _bullish_current()
    return build(mi_cases.synthesis(name)).to_dict()


def _bullish_current():
    """all_bullish technicals with a market-closed (not stale) context whose own returns are positive or zero, so
    Phase 8 emits neither market_context_opposition_present nor market_context_not_current."""
    from evidence_synthesis.builder import synthesize
    from market_intelligence.builder import build
    from tests import evidence_synthesis_corpus as corpus
    from tests.test_evidence_packet import packet
    return build(synthesize(packet(market_context=corpus.earlier_session_ctx(),
                                   technical_rows=corpus.states(*corpus.BULL)))).to_dict()


@lru_cache(maxsize=None)
def options_intelligence(name="quoted_complete", fmt="phase9-v2"):
    from options_intelligence.builder import build
    snapshots = {
        "quoted_complete": lambda: full_snapshot(oi_cases.quoted_chain(), underlying_price=oi_cases.PRICE,
                                                 calendar_state="regular",
                                                 provenance=dict(PROVENANCE, truncated=False)),
        "quoted_truncated": lambda: oi_cases.snapshot("quoted_with_price"),
        "locked_only": lambda: full_snapshot([chain_record(quote=dict(bid="1", ask="1"))],
                                             provenance=dict(PROVENANCE, truncated=False)),
        "live_like_no_quotes": lambda: live_snapshot([chain_record()], provenance=dict(PROVENANCE, truncated=False)),
        "nvda": lambda: live_snapshot([chain_record("NVDA", strike=190)], underlying="NVDA"),
    }
    return build(snapshots[name](), calendar=oi_cases.CALENDAR, format=fmt).to_dict()
