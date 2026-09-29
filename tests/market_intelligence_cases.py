"""Golden cases for Phase 8A Market Intelligence tests (test-only).

Inputs are the Phase 7H replay corpus syntheses (byte-pinned there), plus one extra case the corpus lacks: a single
opposing pair (bullish 1d, range 1h, bearish 5m), synthesized from the stored ``all_bullish`` packet. The expected
MarketIntelligence JSON is stored in ``tests/fixtures/market_intelligence/<name>.intelligence.json``.

Regenerate only on a deliberate rules/format change, and review the diff:

    python -m tests.market_intelligence_cases --regenerate
"""
import argparse
import json
from pathlib import Path

from evidence_synthesis.canonical import canonical_json
from tests import evidence_synthesis_corpus as corpus

FIXTURES = Path(__file__).parent / "fixtures" / "market_intelligence"
SINGLE_PAIR = ("bullish_setup", "range", "bearish_setup")


def packet(name):
    """The EvidencePacket dict behind a case (for packet-pointer resolution)."""
    if name == "single_pair_opposition":
        from tests.test_evidence_synthesis_replay import with_states
        return with_states(json.loads(corpus.packet_path("all_bullish").read_text(encoding="utf-8")), SINGLE_PAIR)
    return json.loads(corpus.packet_path(name).read_text(encoding="utf-8"))


def synthesis(name):
    """The EvidenceSynthesis dict for a case."""
    if name == "single_pair_opposition":
        from evidence_synthesis.builder import synthesize
        return synthesize(packet(name)).to_dict()
    return json.loads(corpus.synthesis_path(name).read_text(encoding="utf-8"))


def names():
    return sorted([*corpus.names(), "single_pair_opposition"])


def intelligence_path(name):
    return FIXTURES / f"{name}.intelligence.json"


def regenerate():
    from market_intelligence.builder import build
    FIXTURES.mkdir(parents=True, exist_ok=True)
    for name in names():
        intelligence_path(name).write_text(canonical_json(build(synthesis(name)).to_dict()) + "\n", encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(prog="python -m tests.market_intelligence_cases")
    parser.add_argument("--regenerate", action="store_true", required=True)
    parser.parse_args()
    regenerate()
