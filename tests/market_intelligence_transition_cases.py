"""Phase 8B transition pairs (test-only): a previous synthesis and a later current synthesis for the same symbol.

- **Previous:** a Phase 7H corpus synthesis (packet ``as_of`` 2026-09-23T20:05Z).
- **Current:** a packet assembled by the Phase 7C assembler at ``LATER`` (five minutes after the previous one),
  from the same fixture inputs with only the named change. Every input stays at or before both cutoffs, so both
  syntheses are cutoff-safe.

The expected MarketIntelligence (current compared with previous) is stored in
``tests/fixtures/market_intelligence_transitions/<name>.intelligence.json``. Regenerate only on a deliberate
rules/format change, and review the diff:

    python -m tests.market_intelligence_transition_cases --regenerate
"""
import argparse
from datetime import timedelta
import json
from pathlib import Path

from evidence_synthesis.canonical import canonical_json
from tests import evidence_synthesis_corpus as corpus
from tests.test_evidence_packet import AS_OF, collection, packet

FIXTURES = Path(__file__).parent / "fixtures" / "market_intelligence_transitions"
LATER = AS_OF + timedelta(minutes=5)
BULL = corpus.BULL
ISOLATED_5M = ("bullish_setup", "bullish_setup", "bearish_setup")
REORDERED_BULL = ("bullish_momentum", "bullish_setup", "breakout_watch")

# name -> (previous corpus case, keyword arguments for the current packet at LATER)
PAIRS = {
    "no_change": ("meta_real_shaped", lambda: {}),
    "entered_alignment": ("meta_real_shaped", lambda: dict(technical_rows=corpus.states(*BULL))),
    "exited_alignment": ("all_bullish", lambda: {}),
    "state_changed_same_direction": ("all_bullish", lambda: dict(technical_rows=corpus.states(*REORDERED_BULL))),
    "opposition_appeared": ("all_bullish", lambda: dict(technical_rows=corpus.states(*ISOLATED_5M))),
    "opposition_resolved": ("higher_aligned_5m_opposed", lambda: dict(technical_rows=corpus.states(*BULL))),
    "timeframe_gap_appeared": ("meta_real_shaped",
                               lambda: dict(technical_rows=corpus.states("bullish_setup", "bullish_setup", None))),
    "insufficient_data_appeared": ("meta_real_shaped",
                                   lambda: dict(technical_rows=corpus.states("bullish_setup", "insufficient_data",
                                                                             "bullish_setup"))),
    "context_became_unavailable": ("meta_real_shaped", lambda: dict(market_context=None)),
    "context_became_available": ("market_context_unavailable", lambda: {}),
    "technical_became_available": ("technical_unavailable", lambda: {}),
    "news_became_unavailable": ("meta_real_shaped", lambda: dict(news=None)),
    "news_became_available": ("news_unavailable", lambda: {}),
    "news_identities_added": ("empty_news", lambda: {}),
    "news_identities_removed": ("meta_real_shaped", lambda: dict(news=collection())),
    "comparison_misaligned_appeared": ("meta_real_shaped", lambda: dict(market_context=corpus.misaligned_ctx())),
    "session_mismatch_appeared": ("meta_real_shaped", lambda: dict(market_context=corpus.earlier_session_ctx())),
    "reference_signs_changed": ("meta_real_shaped", lambda: dict(market_context=corpus.ctx("101", "100.5"))),
}


def previous(name):
    return json.loads(corpus.synthesis_path(PAIRS[name][0]).read_text(encoding="utf-8"))


def current_packet(name):
    return packet(as_of=LATER, **PAIRS[name][1]())


def current(name):
    from evidence_synthesis.builder import synthesize
    return synthesize(current_packet(name)).to_dict()


def previous_packet(name):
    return json.loads(corpus.packet_path(PAIRS[name][0]).read_text(encoding="utf-8"))


def names():
    return sorted(PAIRS)


def intelligence_path(name):
    return FIXTURES / f"{name}.intelligence.json"


def regenerate():
    from market_intelligence.builder import build
    FIXTURES.mkdir(parents=True, exist_ok=True)
    for name in names():
        intelligence_path(name).write_text(canonical_json(build(current(name), previous(name)).to_dict()) + "\n",
                                           encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(prog="python -m tests.market_intelligence_transition_cases")
    parser.add_argument("--regenerate", action="store_true", required=True)
    parser.parse_args()
    regenerate()
