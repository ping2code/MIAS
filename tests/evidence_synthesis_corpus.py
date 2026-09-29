"""Phase 7H replay corpus: the Phase 7G golden cases plus the extra cases the validation needs (test-only).

Every packet is a real ``phase7c-v1`` packet built by the Phase 7C assembler from fixture data (no network, no
database). The new cases only vary inputs the Phase 7G rules already define; no rule semantics are invented.

- ``tests/fixtures/evidence_synthesis/``: the 9 Phase 7G goldens (unchanged).
- ``tests/fixtures/evidence_synthesis_replay/``: the extra cases. Each has ``<name>.packet.json`` and the expected
  ``<name>.synthesis.json``.

Regenerate the replay fixtures only on a deliberate rules/format change, and review the diff:

    python -m tests.evidence_synthesis_corpus --regenerate
"""
import argparse
from datetime import date
from pathlib import Path

from evidence_packet.serialization import canonical_json
from tests import evidence_synthesis_cases as golden
from tests.test_evidence_packet import DAY, collection, packet
from tests.test_market_context import PREV, context, et, flat, standard

REPLAY_FIXTURES = Path(__file__).parent / "fixtures" / "evidence_synthesis_replay"
CORPUS_DIRS = (golden.FIXTURES, REPLAY_FIXTURES)
CLOSE = et(DAY, 16, 5)  # Packet AS_OF (20:05Z) in exchange time.


def ctx(meta_last, qqq_last="99.4", *extra):
    """MarketContext for META (prev close 100, open 100, last ``meta_last``) against QQQ and ``extra`` benchmarks."""
    return context(standard("META", "100", "100", meta_last), ("QQQ", standard("QQQ", "100", "100", qqq_last)),
                   *extra, now=CLOSE)


def misaligned_ctx():
    spy = standard("SPY", "100", "100", "101", n=8)
    del spy[78 + 5]  # SPY lacks the bar ending 10:00: the comparison is misaligned.
    return context(standard("META", "100", "100", "101", n=6), ("SPY", spy), now=CLOSE)


def earlier_session_ctx():
    """Context for session 2026-09-22 while the technical bars end on 2026-09-23."""
    return context(flat("META", date(2026, 9, 21), 78, "100") + flat("META", date(2026, 9, 22), 78, "101"),
                   now=et(date(2026, 9, 22), 16, 5))


def states(d, h, m):
    return golden.states(d, h, m)


BULL = ("bullish_setup", "bullish_momentum", "breakout_watch")

# name → builder. Only the cases that are not already Phase 7G goldens.
EXTRA = {
    "news_unavailable": lambda: packet(news=None),
    "benchmark_unavailable": lambda: packet(market_context=ctx("97.9", "99.4", ("SPY", flat("SPY", PREV, 78, "100")))),
    "benchmark_misaligned": lambda: packet(market_context=misaligned_ctx()),
    "context_opposes_one_timeframe": lambda: packet(technical_rows=states("bullish_setup", "range", "range")),
    "context_session_mismatch": lambda: packet(market_context=earlier_session_ctx()),
    "insufficient_data_state": lambda: packet(technical_rows=states("bullish_setup", "insufficient_data",
                                                                    "bullish_setup")),
    "mixed_all": lambda: packet(technical_rows=states("mixed", "mixed", "mixed")),
    "range_all": lambda: packet(technical_rows=states("range", "range", "range")),
    "zero_market_return": lambda: packet(market_context=ctx("100", "100"), technical_rows=states(*BULL)),
    "positive_market_return": lambda: packet(market_context=ctx("101", "100.5"), technical_rows=states(*BULL)),
    "missing_1d": lambda: packet(technical_rows=states(None, "bullish_setup", "bullish_setup")),
    "missing_1h": lambda: packet(technical_rows=states("bullish_setup", None, "bullish_setup")),
    "missing_1d_5m": lambda: packet(technical_rows=states(None, "bullish_setup", None)),
    "technical_unavailable": lambda: packet(technical_rows=None),
    "market_context_and_news_unavailable": lambda: packet(market_context=None, news=collection(succeeded=False)),
}

# The 21 required Phase 7H scenarios → corpus case names (Phase 7G goldens reused where they already fit).
REQUIRED = {
    "all_bullish": "all_bullish",
    "all_bearish": "all_bearish",
    "all_non_directional": "non_directional",
    "partially_directional": "meta_real_shaped",
    "1d_1h_bullish_5m_bearish": "higher_aligned_5m_opposed",
    "1d_bearish_1h_5m_bullish": "lower_aligned_1d_opposed",
    "missing_5m": "unavailable_timeframe",
    "market_context_unavailable": "market_context_unavailable",
    "empty_available_news": "empty_news",
    "unavailable_news": "news_unavailable",
    "benchmark_comparison_unavailable": "benchmark_unavailable",
    "benchmark_comparison_misaligned": "benchmark_misaligned",
    "context_opposes_one_timeframe": "context_opposes_one_timeframe",
    "context_opposes_multiple_timeframes": "all_bullish",
    "context_session_mismatch": "context_session_mismatch",
    "insufficient_data_state": "insufficient_data_state",
    "mixed_state": "mixed_all",
    "range_state": "range_all",
    "zero_market_return": "zero_market_return",
    "positive_market_return": "positive_market_return",
    "negative_market_return": "all_bullish",
}


def directory(name):
    return golden.FIXTURES if name in golden.CASES else REPLAY_FIXTURES


def packet_path(name):
    return directory(name) / f"{name}.packet.json"


def synthesis_path(name):
    return directory(name) / f"{name}.synthesis.json"


def names():
    return sorted(set(golden.CASES) | set(EXTRA))


def builder(name):
    return golden.CASES.get(name) or EXTRA[name]


def regenerate():
    from evidence_synthesis.builder import synthesize
    REPLAY_FIXTURES.mkdir(parents=True, exist_ok=True)
    for name, build in EXTRA.items():
        data = build().to_dict()
        packet_path(name).write_text(canonical_json(data) + "\n", encoding="utf-8")
        synthesis_path(name).write_text(canonical_json(synthesize(data).to_dict()) + "\n", encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(prog="python -m tests.evidence_synthesis_corpus")
    parser.add_argument("--regenerate", action="store_true", required=True)
    parser.parse_args()
    regenerate()
