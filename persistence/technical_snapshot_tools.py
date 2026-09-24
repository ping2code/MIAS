"""Read-only status, reconciliation and audit for technical snapshots (Phase 4C).

    python -m persistence.technical_snapshot_tools status [--symbol META] [--interval 5m]
    python -m persistence.technical_snapshot_tools reconcile --symbol META --interval 5m --start 2026-09-21 \
        --end 2026-09-24 [--engine-version phase4c-v1]
    python -m persistence.technical_snapshot_tools audit [--engine-version phase4c-v1 ...] [--provider polygon ...]

- Uses ``DATABASE_URL`` from the process environment (never ``.env``). Only SELECT
  statements run; there is no repair or delete mode.
- **reconcile** compares stored rows with the calendar's expected completed-bar grid
  (regular session; trading days for 1d) and reports missing, unexpected, duplicate
  and conflicting identities. Missing rows are informational: the runner stores the
  latest bar per run, so coverage depends on scheduler cadence.
- **audit** reports duplicate identity groups, recorded conflicts, missing required
  metrics, invalid enum values, timestamps off the grid or persisted before the bar
  completed, unexpected providers or engine versions, and content-hash mismatches.

Output is JSON. Exit codes: 0 clean, 1 problems found (audit) or database error, 2 configuration or usage error.
"""
import argparse
from datetime import datetime, time, timedelta, timezone
import json
import os
import sys

import sqlalchemy as sa

from market_data.models import EXCHANGE_TZ, Interval, Session
from persistence.models import (BREAKOUT_STATES, TECHNICAL_INTERVALS, TECHNICAL_STATES, TECHNICAL_TRENDS,
                                technical_snapshot_conflicts, technical_snapshots)
from persistence.technical_snapshot_repository import MATERIAL_FIELDS, content_hash

MOMENTUM = (None, "overbought_like", "strong", "neutral", "weak", "oversold_like")
ALIGNMENT = (None, "bullish_alignment", "bearish_alignment", "mixed")
VWAP_POSITION = (None, "above_vwap", "below_vwap", "crossing_above_vwap", "crossing_below_vwap")
HIGH_TYPES, LOW_TYPES = (None, "HH", "LH", "EH"), (None, "HL", "LL", "EL")
ENUMS = dict(interval=TECHNICAL_INTERVALS, technical_state=TECHNICAL_STATES, trend=TECHNICAL_TRENDS,
             breakout_state=BREAKOUT_STATES, confidence=("LOW", "MEDIUM", "HIGH"), momentum=MOMENTUM,
             ema_alignment=ALIGNMENT, vwap_position=VWAP_POSITION, last_high_type=HIGH_TYPES, last_low_type=LOW_TYPES,
             gap_type=(None, "gap_up", "gap_down", "no_gap"), session_type=(None, "regular", "pre", "post"))


def expected_grid(calendar, interval, start_day, end_day, as_of):
    """Completed regular-session bar starts (1d: midnight of each trading day) for start_day <= day < end_day."""
    interval = Interval.parse(interval)
    out, day = [], start_day
    while day < end_day:
        times = calendar.session_times(day)
        if times is not None:
            if not interval.intraday:
                if times.close <= as_of:
                    out.append(datetime.combine(day, time(0), tzinfo=EXCHANGE_TZ))
            else:
                stamp = times.open
                while stamp < times.close:
                    if min(stamp + interval.delta, times.close) <= as_of:
                        out.append(stamp)
                    stamp += interval.delta
        day += timedelta(days=1)
    return out


def bar_end(calendar, interval, stamp):
    """End of the bar starting at ``stamp`` (truncated at its session segment), or None if it cannot map to a session."""
    interval = Interval.parse(interval)
    local = stamp.astimezone(EXCHANGE_TZ)
    times = calendar.session_times(local.date())
    if times is None:
        return None
    if not interval.intraday:
        return times.close if local.time() == time(0) else None
    session = calendar.classify(stamp)
    if session is Session.CLOSED:
        return None
    seg_start, seg_end = times.segment(session)
    if (local - seg_start).total_seconds() % interval.seconds:
        return None
    return min(stamp + interval.delta, seg_end)


def _row_for_hash(row):
    values = {k: row[k] for k in MATERIAL_FIELDS}
    for key in ("snapshot_timestamp", "warmup_start"):
        values[key] = row[key].astimezone(timezone.utc).isoformat()
    return values


def audit_rows(rows, conflicts, calendar, *, engine_versions, providers):
    """Pure audit over row dicts; returns (report, problem_count)."""
    identities = {}
    for row in rows:
        key = (row["symbol"], row["interval"], row["snapshot_timestamp"], row["engine_version"])
        identities[key] = identities.get(key, 0) + 1
    duplicates = [dict(symbol=k[0], interval=k[1], snapshot_timestamp=k[2].isoformat(), engine_version=k[3], rows=n)
                  for k, n in identities.items() if n > 1]
    missing_metrics, invalid_enums, bad_times, early, hash_mismatch = [], [], [], [], []
    for row in rows:
        ident = f"{row['symbol']} {row['interval']} {row['snapshot_timestamp'].isoformat()} {row['engine_version']}"
        if row["technical_state"] != "insufficient_data" and None in (row["ema20"], row["rsi14"], row["atr14"]):
            missing_metrics.append(ident)
        bad = [k for k, allowed in ENUMS.items() if row[k] not in allowed]
        if bad:
            invalid_enums.append(dict(identity=ident, fields=bad))
        end = bar_end(calendar, row["interval"], row["snapshot_timestamp"]) if row["interval"] in TECHNICAL_INTERVALS else None
        if end is None:
            bad_times.append(ident)
        elif end > row["created_at"]:
            early.append(ident)
        values = _row_for_hash(row)
        if content_hash(json.loads(json.dumps(values, allow_nan=False))) != row["content_hash"]:
            hash_mismatch.append(ident)
    report = dict(
        rows=len(rows), duplicate_identity_groups=duplicates, conflicts=len(conflicts),
        conflict_identities=sorted({f"{c['symbol']} {c['interval']} {c['snapshot_timestamp'].isoformat()} "
                                    f"{c['engine_version']}" for c in conflicts}),
        missing_required_metrics=missing_metrics, invalid_enum_values=invalid_enums,
        timestamps_off_grid=bad_times, persisted_before_bar_completed=early,
        unexpected_providers=sorted({r["source_provider"] for r in rows} - set(providers)),
        unexpected_engine_versions=sorted({r["engine_version"] for r in rows} - set(engine_versions)),
        content_hash_mismatches=hash_mismatch)
    problems = (len(duplicates) + len(conflicts) + len(missing_metrics) + len(invalid_enums) + len(bad_times)
                + len(early) + len(report["unexpected_providers"]) + len(report["unexpected_engine_versions"])
                + len(hash_mismatch))
    report["problems"] = problems
    return report, problems


def reconcile_rows(rows, conflicts, expected):
    stored = {}
    for row in rows:
        stored.setdefault(row["snapshot_timestamp"], []).append(row)
    expected_set = set(expected)
    return dict(expected=len(expected), stored_rows=len(rows), stored_identities=len(stored),
                missing=[s.isoformat() for s in expected if s not in stored],
                unexpected=[s.isoformat() for s in sorted(stored) if s not in expected_set],
                duplicates=[s.isoformat() for s, group in sorted(stored.items()) if len(group) > 1],
                conflicts=len(conflicts))


def status(session, symbol=None, interval=None):
    t = technical_snapshots
    query = sa.select(t.c.symbol, t.c.interval, t.c.engine_version, sa.func.count(), sa.func.min(t.c.snapshot_timestamp),
                      sa.func.max(t.c.snapshot_timestamp)).group_by(t.c.symbol, t.c.interval, t.c.engine_version)
    if symbol:
        query = query.where(t.c.symbol == symbol)
    if interval:
        query = query.where(t.c.interval == interval)
    groups = [dict(symbol=s, interval=i, engine_version=v, rows=n, first=lo.isoformat(), last=hi.isoformat())
              for s, i, v, n, lo, hi in session.execute(query.order_by(t.c.symbol, t.c.interval, t.c.engine_version))]
    conflicts = session.execute(sa.select(sa.func.count()).select_from(technical_snapshot_conflicts)).scalar_one()
    return dict(groups=groups, conflicts=conflicts)


def _load(session, model, *conditions):
    return [dict(r._mapping) for r in session.execute(sa.select(model).where(*conditions))]


def _day(value):
    return datetime.strptime(value, "%Y-%m-%d").date()


def main(argv=None, environ=None, *, engine=None, calendar=None, now=None, out=None):
    from persistence.config import ConfigurationError, DatabaseSettings
    from persistence.database import PersistenceError, make_engine, transaction
    from persistence.technical_settings import DEFAULT_ENGINE_VERSION
    out = out or sys.stdout
    parser = argparse.ArgumentParser(prog="python -m persistence.technical_snapshot_tools")
    parser.add_argument("command", choices=("status", "reconcile", "audit"))
    parser.add_argument("--symbol")
    parser.add_argument("--interval")
    parser.add_argument("--start", type=_day)
    parser.add_argument("--end", type=_day)
    parser.add_argument("--engine-version", action="append")
    parser.add_argument("--provider", action="append")
    try:
        args = parser.parse_args(argv)
        if args.command == "reconcile" and not (args.symbol and args.interval and args.start and args.end):
            raise ValueError
        if args.interval:
            Interval.parse(args.interval)
    except (SystemExit, ValueError, Exception):
        print(json.dumps(dict(error="usage")), file=out)
        return 2
    owned = engine is None
    try:
        if owned:
            engine = make_engine(DatabaseSettings.from_env(os.environ if environ is None else environ))
    except ConfigurationError as error:
        print(json.dumps(dict(error=str(error))), file=out)
        return 2
    if calendar is None:
        from market_data.calendar import default_calendar
        calendar = default_calendar()
    now = now or datetime.now(timezone.utc)
    try:
        with transaction(engine) as session:
            if args.command == "status":
                result, code = status(session, args.symbol, args.interval), 0
            elif args.command == "reconcile":
                t, c = technical_snapshots, technical_snapshot_conflicts
                start = datetime.combine(args.start, time(0), tzinfo=EXCHANGE_TZ)
                end = datetime.combine(args.end, time(0), tzinfo=EXCHANGE_TZ)
                version = (args.engine_version or [DEFAULT_ENGINE_VERSION])[0]
                rows = _load(session, t, t.c.symbol == args.symbol.upper(), t.c.interval == args.interval,
                             t.c.snapshot_timestamp >= start, t.c.snapshot_timestamp < end, t.c.engine_version == version)
                conflicts = _load(session, c, c.c.symbol == args.symbol.upper(), c.c.interval == args.interval,
                                  c.c.snapshot_timestamp >= start, c.c.snapshot_timestamp < end,
                                  c.c.engine_version == version)
                result = dict(symbol=args.symbol.upper(), interval=args.interval, engine_version=version,
                              **reconcile_rows(rows, conflicts, expected_grid(calendar, args.interval, args.start,
                                                                                args.end, now)))
                code = 0
            else:
                rows = _load(session, technical_snapshots, sa.true())
                conflicts = _load(session, technical_snapshot_conflicts, sa.true())
                result, problems = audit_rows(rows, conflicts, calendar,
                                              engine_versions=args.engine_version or [DEFAULT_ENGINE_VERSION],
                                              providers=args.provider or ["polygon"])
                code = 1 if problems else 0
    except PersistenceError:
        print(json.dumps(dict(error="database unavailable")), file=out)
        return 1
    finally:
        if owned:
            engine.dispose()
    print(json.dumps(dict(command=args.command, **result), indent=2, sort_keys=True), file=out)
    return code


if __name__ == "__main__":
    sys.exit(main())
