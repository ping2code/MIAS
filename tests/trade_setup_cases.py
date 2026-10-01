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


# --- Phase 10C screening fixtures: real Phase 9 snapshots built from synthetic quoted records ---
# Screening-neutral policy: every optional rule off, so each test enables exactly the rule it exercises.
SCREEN_POLICY = dict(BASE_POLICY, require_iv=False, allow_unverified_time_basis=True, max_spread_relative=None,
                     abs_delta_min=None, abs_delta_max=None)
EXPIRY = "2026-10-16"  # DTE 16 from the 2026-09-30 snapshot


def quote(bid="9.9", ask="10.1", **kw):
    """A quote group observed before as_of; the default is complete with mid 10, spread 0.2, relative 0.02."""
    from tests.options_snapshot_cases import BEFORE
    fields = dict(dict(observed_at=BEFORE), bid=bid, ask=ask, **kw)
    return {k: v for k, v in fields.items() if v is not None}


def record(strike=700, option_type="call", expiration=EXPIRY, *, day="current", **kw):
    """A quoted chain record: complete quote, current-session day (volume 1200), untimed IV/Greeks/OI, 100 shares."""
    from tests.options_intelligence_cases import OLDER_SESSION, PREVIOUS_SESSION, day as day_group
    from tests.options_snapshot_cases import BEFORE, chain_record
    days = dict(current=day_group(BEFORE, 1200), previous=day_group(PREVIOUS_SESSION, 50),
                older=day_group(OLDER_SESSION, 50), none=False)
    kw.setdefault("quote", quote())
    return chain_record("META", expiration, option_type, strike, day=days[day] if isinstance(day, str) else day, **kw)


def chain(records, fmt="phase9-v2"):
    """A sealed OptionsIntelligence (plain dict) for these records, through the real Phase 9 pipeline."""
    from options_intelligence.builder import build
    from tests.options_intelligence_cases import CALENDAR
    from tests.options_snapshot_cases import full_snapshot
    snapshot = full_snapshot(list(records), provenance=dict(PROVENANCE, truncated=False), calendar_state="regular")
    return build(snapshot, calendar=CALENDAR, format=fmt).to_dict()


def anchor(side):
    """An opposite-side complete-quote contract so global gate 7 passes whatever the tested contract's quote."""
    return record(500, "put" if side == "call" else "call")


def screen_policy(**overrides):
    return make_policy(**dict(SCREEN_POLICY, **overrides))


def screened(records, mi_name="all_bullish", fmt="phase9-v2", with_anchor=True, **overrides):
    """(assessment, options intelligence) for records under SCREEN_POLICY + overrides."""
    from trade_setup.builder import assess
    side = "put" if mi_name == "all_bearish" else "call"
    oi = chain(list(records) + ([anchor(side)] if with_anchor else []), fmt)
    return assess(market_intelligence(mi_name), oi, screen_policy(**overrides)), oi


def contract_id(oi, rec):
    return next(c["contract_id"] for c in oi["contracts"] if c["provider_symbol"] == rec["provider_symbol"])


def verdict(rec, **kw):
    """"candidate", or the frozenset of rejection reasons, for one record screened next to an anchor."""
    assessment, oi = screened([rec], **kw)
    cid = contract_id(oi, rec)
    if any(c.source.contract_id == cid for c in assessment.candidates):
        return "candidate"
    return frozenset(x.reason_code for x in assessment.rejections if cid in x.contract_ids)


def resealed_oi(oi, cid, **fields):
    """A copy of a sealed OptionsIntelligence with one contract's fields replaced and the id resealed."""
    from copy import deepcopy
    from trade_setup.canonical import content_id
    data = deepcopy(oi)
    next(c for c in data["contracts"] if c["contract_id"] == cid).update(fields)
    data["options_intelligence_id"] = content_id({k: v for k, v in data.items() if k != "options_intelligence_id"})
    return data
