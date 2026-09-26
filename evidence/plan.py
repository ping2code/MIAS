"""Phase 6 collection dry run: prints the frozen collection plan. No network, no database, no market-data key.

    python -m evidence.plan

It prints:

- the universe, intervals and hypotheses, with their frozen hashes;
- the pinned start, or (when unpinned) the start candidate implied by freezing at the current HEAD;
- the daily vendor request estimate at the configured pacing;
- the persistence targets;
- the recommended once-daily scheduler settings.
"""
import argparse
from datetime import datetime, timezone
import json
import math
import sys

from evidence.registry import (ENGINE_VERSION, HYPOTHESES, HYPOTHESIS_HASHES, PROSPECTIVE_FORMAT_VERSION, PROTOCOL,
                               PROTOCOL_HASH, REGISTRY_HASH)

VENDOR_PAGE_LIMIT = 50_000  # Base minute aggregates per page (limit counts minutes, not target bars).
MINUTES_PER_DAY_UPPER = 960  # 04:00-20:00 ET: the upper bound of minute aggregates per session.
FETCHES = (("5m", 10), ("30m (derives 1h, 1d)", 450))  # Runner warm-up sessions per source fetch.
PACING_SECONDS = 12.0


def request_estimate(symbols=PROTOCOL.collection_symbols, pacing=PACING_SECONDS):
    per_symbol = {name: math.ceil(sessions * MINUTES_PER_DAY_UPPER / VENDOR_PAGE_LIMIT) for name, sessions in FETCHES}
    total = sum(per_symbol.values()) * len(symbols)
    return dict(per_symbol_pages=per_symbol, requests_per_day_upper_bound=total,
                minutes_at_pacing=round(total * pacing / 60, 1), pacing_seconds=pacing)


def plan(pin=None, candidate=None):
    return dict(
        mode="dry_run", network=False, prospective_format_version=PROSPECTIVE_FORMAT_VERSION,
        universe=list(PROTOCOL.collection_symbols), intervals=list(PROTOCOL.collection_intervals),
        engine_version=ENGINE_VERSION, provider=PROTOCOL.provider, registry_hash=REGISTRY_HASH,
        protocol_hash=PROTOCOL_HASH, hypothesis_hashes=HYPOTHESIS_HASHES,
        hypotheses={h.id: dict(state=list(h.state), interval=list(h.interval), symbols=list(h.symbols))
                    for h in HYPOTHESES},
        identities_per_session=len(PROTOCOL.collection_symbols) * len(PROTOCOL.collection_intervals),
        min_complete_sessions=PROTOCOL.min_complete_sessions,
        pin=None if pin is None else {k: str(v) for k, v in pin.items()},
        start_candidate_if_frozen_at_head=candidate,
        requests=request_estimate(),
        persistence=dict(ledger="technical_evidence_ledger (migration 0007, append-only)",
                         snapshots="technical_snapshots (shadow persistence; soft reference)"),
        scheduler=dict(TECHNICAL_SCHEDULE_ENABLED="true (operator decision; disabled by default)",
                       TECHNICAL_INTERVAL_SECONDS=86400, TECHNICAL_TIMEOUT_SECONDS=1800,
                       TECHNICAL_EVIDENCE_LEDGER_ENABLED="true", TECHNICAL_SNAPSHOT_PERSISTENCE_SHADOW_ENABLED="true",
                       start_time="about 08:00 America/New_York (after the free tier publishes the prior session)"))


def main(argv=None, out=None):
    from evidence.pin import PinError, build_pin, commit_time_of, current_commit, load_pin
    from market_data.calendar import default_calendar
    argparse.ArgumentParser(prog="python -m evidence.plan").parse_args(argv)
    calendar = default_calendar()
    pin = candidate = None
    try:
        pin = load_pin(calendar=calendar)
    except PinError:
        commit = current_commit()
        if commit:
            try:
                head = build_pin(commit, commit_time_of(commit), calendar)
                candidate = dict(commit=commit, prospective_start_session=head["prospective_start_session"],
                                 earliest_evaluation_session=head["earliest_evaluation_session"],
                                 note="candidate only; the start is fixed by the merged freeze commit's pin")
            except Exception:
                candidate = None
    result = plan(pin, candidate)
    result["generated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(json.dumps(result, indent=2, sort_keys=True), file=out or sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
