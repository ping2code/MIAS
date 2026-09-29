"""Golden cases for Phase 7G synthesis tests: fixture packets and their expected synthesis (test-only).

Each case is a real ``phase7c-v1`` packet assembled by the Phase 7C assembler from fixture data. Technical states
are set per timeframe, and each row's ``content_hash`` is re-sealed with the existing durable-row ``content_hash``.

- The **packet JSON** is stored, so tests don't depend on engine float output.
- The **expected synthesis JSON** is stored next to it.

Regenerate only on a deliberate rules/format change, and review the diff:

    python -m tests.evidence_synthesis_cases --regenerate
"""
import argparse
from pathlib import Path

from evidence_packet.serialization import canonical_json
from persistence.technical_snapshot_repository import content_hash
from tests.test_evidence_packet import collection, packet
from tests.test_evidence_packet_adapters import technical_rows

FIXTURES = Path(__file__).parent / "fixtures" / "evidence_synthesis"


def rows_with(states):
    """Technical rows (1d/1h/5m) with the given states; None drops that interval (the packet marks it missing)."""
    rows = technical_rows()
    out = {}
    for interval, state in states.items():
        if state is None:
            continue
        row = dict(rows[interval], technical_state=state)
        row["content_hash"] = content_hash(row)
        out[interval] = row
    return out


def states(d, h, m):
    return rows_with({"1d": d, "1h": h, "5m": m})


CASES = {
    "all_bullish": lambda: packet(technical_rows=states("bullish_setup", "bullish_momentum", "breakout_watch")),
    "all_bearish": lambda: packet(technical_rows=states("bearish_setup", "bearish_momentum", "breakdown_watch")),
    "non_directional": lambda: packet(technical_rows=states("range", "mixed", "range")),
    "higher_aligned_5m_opposed": lambda: packet(technical_rows=states("bullish_setup", "bullish_setup",
                                                                      "bearish_setup")),
    "lower_aligned_1d_opposed": lambda: packet(technical_rows=states("bearish_setup", "bullish_setup",
                                                                     "bullish_momentum")),
    "unavailable_timeframe": lambda: packet(technical_rows=states("bullish_setup", "bullish_setup", None)),
    "market_context_unavailable": lambda: packet(market_context=None),
    "empty_news": lambda: packet(news=collection()),
    "meta_real_shaped": lambda: packet(),
}


def packet_path(name):
    return FIXTURES / f"{name}.packet.json"


def synthesis_path(name):
    return FIXTURES / f"{name}.synthesis.json"


def regenerate():
    from evidence_synthesis.builder import synthesize
    FIXTURES.mkdir(parents=True, exist_ok=True)
    for name, build in CASES.items():
        data = build().to_dict()
        packet_path(name).write_text(canonical_json(data) + "\n", encoding="utf-8")
        synthesis_path(name).write_text(canonical_json(synthesize(data).to_dict()) + "\n", encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(prog="python -m tests.evidence_synthesis_cases")
    parser.add_argument("--regenerate", action="store_true", required=True)
    parser.parse_args()
    regenerate()
