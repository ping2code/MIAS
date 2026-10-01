"""Phase 10E replay fixtures (test-only): small canonical input files and the expected runner outputs.

Inputs:
- the real Phase 8 golden ``all_bullish`` MarketIntelligence (``tests/fixtures/market_intelligence``, referenced in
  place, not copied);
- a small synthetic quoted META chain, run through the real Phase 9 pipeline as both phase9-v2 and phase9-v1;
- explicit policies;
- later MarketIntelligence files: real Phase 8 cases moved one day later and resealed (``later_mi``).

Expected outputs are the canonical runner outputs. Regenerate only on a deliberate change, and review the diff:

    python -m tests.trade_setup_replay_cases --regenerate
"""
import argparse
from pathlib import Path

from trade_setup.canonical import canonical_json

FIXTURES = Path(__file__).parent / "fixtures" / "trade_setup_replay"
MARKET_INTELLIGENCE = Path(__file__).parent / "fixtures" / "market_intelligence" / "all_bullish.intelligence.json"
LATER = ("all_bullish", "all_bearish", "missing_1d")   # holds, invalidated, not_evaluable for the call setup
POLICY_V2 = dict(allowed_sides=["call", "put"], min_dte=0, max_dte=60, allow_same_day_expiry=False,
                 allow_locked_quote=True, max_spread_relative="0.1", abs_delta_min="0.2", abs_delta_max="0.7",
                 min_volume=1, min_open_interest=100, max_premium_per_contract="2000", require_iv=True,
                 allow_unverified_time_basis=True, require_current_session_day=False, require_complete_chain=True,
                 max_input_gap_seconds=10 ** 7, block_on_market_context_opposition=False,
                 block_on_market_context_not_current=False)
POLICY_V1 = dict(POLICY_V2, min_volume=None, min_open_interest=None, max_premium_per_contract=None)


def records():
    """Every screening-relevant quote shape on a small META chain (complete, locked, one-sided, crossed, puts)."""
    from tests.trade_setup_cases import quote, record
    return [record(690), record(700, quote=quote("5", "5")), record(710, quote=quote(bid=None)),
            record(720, quote=quote("10.2", "10")), record(730, shares=None), record(700, option_type="put"),
            record(700, expiration="2026-10-23")]


def path(name):
    return FIXTURES / name


def build_inputs():
    """{file name: plain dict} for every input fixture."""
    from trade_setup.policy import make_policy
    from tests import trade_setup_cases as cases
    files = {"options_intelligence.v2.json": cases.chain(records(), "phase9-v2"),
             "options_intelligence.v1.json": cases.chain(records(), "phase9-v1"),
             "policy.v2.json": make_policy(**POLICY_V2).to_dict(), "policy.v1.json": make_policy(**POLICY_V1).to_dict()}
    for name in LATER:
        files[f"later_market_intelligence.{name}.json"] = cases.later_mi(name)
    return files


def build_outputs():
    """{file name: plain dict} for every expected runner output, from the input fixtures."""
    import json
    from trade_setup.builder import assess
    from trade_setup.invalidation import check_invalidation
    mi = json.loads(MARKET_INTELLIGENCE.read_text(encoding="utf-8"))
    load = lambda name: json.loads(path(name).read_text(encoding="utf-8"))  # noqa: E731
    outputs = {}
    for fmt in ("v2", "v1"):
        outputs[f"assessment.{fmt}.json"] = assess(mi, load(f"options_intelligence.{fmt}.json"),
                                                   load(f"policy.{fmt}.json")).to_dict()
    for name in LATER:
        outputs[f"invalidation.{name}.json"] = check_invalidation(
            outputs["assessment.v2.json"], load(f"later_market_intelligence.{name}.json")).to_dict()
    return outputs


def regenerate():
    FIXTURES.mkdir(parents=True, exist_ok=True)
    for build in (build_inputs, build_outputs):
        for name, data in build().items():
            path(name).write_text(canonical_json(data) + "\n", encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--regenerate", action="store_true")
    if parser.parse_args().regenerate:
        regenerate()
