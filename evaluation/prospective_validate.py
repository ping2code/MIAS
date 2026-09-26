"""Phase 6 prospective validation of the frozen H1' and H2' (locked by default; research only, no trading logic).

    python -m evaluation.prospective_validate [--early-look-override] [--out FILE]

**Gate:** the command refuses to fetch bars or compute anything until both hold:

- the pinned earliest evaluation session has been reached;
- at least 120 complete prospective sessions exist in the ledger.

When locked, it prints:

    Evaluation locked. Earliest evaluation date: ... Eligible sessions: N / 120. No hypothesis statistics were computed.

It then exits 3. ``--early-look-override`` runs anyway, and the whole output is
labelled ``NON_PREREGISTERED_EARLY_LOOK``: it is not a preregistered result and
must never be reported as one.

**When allowed:**

- It re-fetches bars. The start of the warm-up history is the pinned start minus
  the runner's warm-up sessions.
- It derives 1h and 1d exactly as the runner does.
- Every accepted session's bar hash is verified against the ledger.
- Only H1' and H2' are evaluated, exactly as frozen in ``evidence.registry``.

The output carries full provenance: format version; registry, protocol and
hypothesis hashes; the pin; the code commit; the evaluated window; excluded
sessions; seed and iterations.

Exit codes:

- 0: evaluated;
- 3: locked;
- 2: no valid pin, or configuration error;
- 1: provider or database failure.
"""
import argparse
from datetime import datetime, timedelta, timezone
import json
import os
import sys

from evaluation import prospective
from evidence.registry import (ENGINE_VERSION, H1P, H2P, HYPOTHESIS_HASHES, PROSPECTIVE_FORMAT_VERSION, PROTOCOL,
                               PROTOCOL_HASH, REGISTRY_HASH, SEED)
from market_data.models import EXCHANGE_TZ, Interval

def warmup_begin(start_session, target, calendar):
    """Midnight ET of the runner's warm-up window before the prospective start (5m 10, 1h 90, 1d 450 sessions)."""
    from technical.runner import WARMUP_SESSIONS
    day = start_session
    for _ in range(WARMUP_SESSIONS[target]):
        day = calendar.previous_trading_day(day)
    return datetime.combine(day, datetime.min.time(), tzinfo=EXCHANGE_TZ)


def fetch_series(provider, symbols, start_session, calendar):
    """{(symbol, label): bars} for 5m (direct) and 1h/1d (derived from 30m), each with the runner's warm-up."""
    from market_data.aggregation import derive_completed
    as_of = provider.as_of()
    begin = {t: warmup_begin(start_session, t, calendar) for t in (Interval.M5, Interval.H1, Interval.D1)}
    out = {}
    for symbol in symbols:
        m5 = provider.get_bars(symbol, Interval.M5, begin[Interval.M5], as_of + timedelta(seconds=1))
        m30 = provider.get_bars(symbol, Interval.M30, begin[Interval.D1], as_of + timedelta(seconds=1))
        out[(symbol, "5m")] = [b for b in m5 if b.regular]
        for target in (Interval.H1, Interval.D1):
            source = [b for b in m30 if b.timestamp >= begin[target]]
            out[(symbol, target.label)] = [b for b in derive_completed(source, target, calendar, as_of) if b.regular]
    return out


def evaluate(series, accepted, complete, *, calendar, seed=SEED, iterations=1000):
    """Frozen H1' and H2' over re-fetched series; returns (h1, h2, excluded sessions)."""
    from technical.engine import TechnicalEngine
    excluded, h1_input, h2_input = [], {}, {}
    for (symbol, label), bars in sorted(series.items()):
        wanted_h1 = label == H1P.interval[0] and symbol in H1P.symbols
        wanted_h2 = label in H2P.interval and symbol in H2P.symbols
        if not (wanted_h1 or wanted_h2) or not bars:
            continue
        eligible, dropped = prospective.eligible_sessions(bars, accepted, complete, symbol=symbol, interval=label)
        excluded += dropped
        snapshots = TechnicalEngine(calendar=calendar).replay(bars)
        if wanted_h1:
            h1_input[symbol] = prospective.h1_symbol(bars, [s.signal.state for s in snapshots], eligible)
        if wanted_h2:
            h2_input[(symbol, label)] = prospective.h2_cells(bars, snapshots, eligible, symbol=symbol, interval=label)
    return (prospective.evaluate_h1(h1_input, seed=seed, iterations=iterations),
            prospective.evaluate_h2(h2_input, seed=seed, iterations=iterations), excluded)


def main(argv=None, environ=None, *, engine=None, provider=None, calendar=None, now=None, pin=None, out=None):
    from evidence.pin import PinError, current_commit, load_pin
    from market_data.config import MarketDataConfigError, load_market_data_settings
    from market_data.http import ProviderError
    from market_data.models import MarketDataError
    from persistence.config import ConfigurationError, DatabaseSettings
    from persistence.database import PersistenceError, make_engine, transaction
    from persistence.technical_evidence_tools import coverage, last_completed_session, load_rows
    environ = os.environ if environ is None else environ
    out = out or sys.stdout
    parser = argparse.ArgumentParser(prog="python -m evaluation.prospective_validate")
    parser.add_argument("--early-look-override", action="store_true",
                        help="run before the gate; output is labelled NON_PREREGISTERED_EARLY_LOOK")
    parser.add_argument("--out", help="also write the JSON result to this file")
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return 2
    if calendar is None:
        from market_data.calendar import default_calendar
        calendar = default_calendar()
    try:
        pin = pin or load_pin(calendar=calendar)
    except PinError as error:
        print(json.dumps(dict(error=str(error))), file=out)
        return 2
    now = now or datetime.now(timezone.utc)
    start = pin["prospective_start_session"]
    through = last_completed_session(now, calendar)
    owned = engine is None
    try:
        if owned:
            engine = make_engine(DatabaseSettings.from_env(environ))
        with transaction(engine) as session:
            rows = load_rows(session)
    except ConfigurationError as error:
        print(json.dumps(dict(error=str(error))), file=out)
        return 2
    except PersistenceError:
        print(json.dumps(dict(error="database unavailable")), file=out)
        return 1
    finally:
        if owned and engine is not None:
            engine.dispose()
    cover = coverage(rows, start=start, through=through, calendar=calendar)
    state = prospective.gate(pin, cover["complete_sessions"], now.astimezone(EXCHANGE_TZ).date())
    if not state["open"] and not args.early_look_override:
        print(prospective.locked_message(state), file=out)  # No-peek: no fetch, no state, no statistic.
        return 3
    label = prospective.PREREGISTERED if state["open"] else prospective.EARLY_LOOK
    try:
        if provider is None:
            from market_data.providers import build_provider
            provider = build_provider(load_market_data_settings(environ))
        series = fetch_series(provider, PROTOCOL.collection_symbols, start, calendar)
    except (MarketDataConfigError, ConfigurationError) as error:
        print(json.dumps(dict(error=str(error))), file=out)
        return 2
    except (ProviderError, MarketDataError) as error:
        print(json.dumps(dict(error="provider failure", kind=getattr(error, "kind", "data"))), file=out)
        return 1
    accepted = {(r["market_session_date"], r["symbol"], r["interval"]): r for r in rows
                if r["record_status"] == "collected" and r["registry_hash"] == REGISTRY_HASH
                and r["engine_version"] == ENGINE_VERSION}
    complete = {datetime.fromisoformat(d).date() for d in cover["complete_session_dates"]}
    h1, h2, excluded = evaluate(series, accepted, complete, calendar=calendar)
    result = dict(
        evaluation_label=label, prospective_format_version=PROSPECTIVE_FORMAT_VERSION, registry_hash=REGISTRY_HASH,
        protocol_hash=PROTOCOL_HASH, hypothesis_hashes=HYPOTHESIS_HASHES, engine_version=ENGINE_VERSION,
        prospective_freeze_commit=pin.get("prospective_freeze_commit"), prospective_start_session=start.isoformat(),
        earliest_evaluation_session=pin["earliest_evaluation_session"].isoformat(), gate=state,
        code_commit=current_commit(), evaluated_at=now.isoformat(), through_session=through.isoformat(),
        complete_sessions=len(complete), excluded_sessions=excluded, seed=SEED, provider=provider.settings.provider,
        hypotheses={H1P.id: dict(h1, evaluation_label=label), H2P.id: dict(h2, evaluation_label=label)},
        note=("research only; no trading logic. " + ("" if state["open"] else
              "NON_PREREGISTERED_EARLY_LOOK: gate not reached; not a preregistered result.")).strip())
    text = json.dumps(result, indent=2, sort_keys=True, default=str)
    if args.out:
        with open(args.out, "w") as handle:
            handle.write(text + "\n")
    print(text, file=out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
