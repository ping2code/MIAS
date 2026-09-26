"""Read-only reconciliation and audit of the prospective evidence ledger (Phase 6). No outcome statistics exist here.

    python -m persistence.technical_evidence_tools reconcile [--through YYYY-MM-DD]
    python -m persistence.technical_evidence_tools audit

- **reconcile:** expected XNYS sessions from the pinned prospective start through the
  last completed session, × the frozen symbols × intervals, compared with accepted
  rows. It reports missing identities, complete sessions, backfilled rows, failures
  without a later success, and conflicts. Weekends and holidays are never expected.
- **audit:** recomputes every ``evidence_hash``; checks the frozen registry hash and
  engine version; checks status and hash invariants, that no row predates the start,
  ``backfilled`` consistency, snapshot-reference existence and content hash, and
  that the append-only triggers exist.

Only SELECT statements run. The ledger holds no returns or states, so neither
command can reveal hypothesis outcomes.
"""
import argparse
from datetime import date, datetime, timezone
import json
import os
import sys

import sqlalchemy as sa

from evidence.registry import ENGINE_VERSION, PROTOCOL, REGISTRY_HASH
from market_data.models import EXCHANGE_TZ
from persistence.models import technical_evidence_ledger, technical_snapshots
from persistence.technical_evidence_ledger import evidence_hash, expected_collection_date

TRIGGERS = ("technical_evidence_ledger_no_update_delete", "technical_evidence_ledger_no_truncate")
SQLITE_TRIGGERS = ("technical_evidence_ledger_no_update", "technical_evidence_ledger_no_delete")


def expected_sessions(start, through, calendar):
    return calendar.trading_days(start, through) if start <= through else []


def coverage(rows, *, start, through, calendar, symbols=PROTOCOL.collection_symbols,
             intervals=PROTOCOL.collection_intervals):
    """Counts only (no-peek): expected/collected/missing identities and complete sessions."""
    sessions = expected_sessions(start, through, calendar)
    accepted = {(r["market_session_date"], r["symbol"], r["interval"]) for r in rows
                if r["record_status"] == "collected" and r["registry_hash"] == REGISTRY_HASH
                and r["engine_version"] == ENGINE_VERSION}
    conflicted = {(r["market_session_date"], r["symbol"], r["interval"]) for r in rows if r["record_status"] == "conflict"}
    failed = {(r["market_session_date"], r["symbol"], r["interval"]) for r in rows if r["record_status"] == "failed"}
    expected = [(d, s, i) for d in sessions for s in symbols for i in intervals]
    missing = [e for e in expected if e not in accepted]
    complete = [d for d in sessions if all((d, s, i) in accepted and (d, s, i) not in conflicted
                                           for s in symbols for i in intervals)]
    return dict(expected_sessions=len(sessions), expected_identities=len(expected),
                accepted_identities=len([e for e in expected if e in accepted]),
                missing_identities=[dict(session=d.isoformat(), symbol=s, interval=i) for d, s, i in missing],
                missing_sessions=sorted({d.isoformat() for d, _, _ in missing}),
                complete_sessions=len(complete), complete_session_dates=[d.isoformat() for d in complete],
                backfilled_rows=sum(1 for r in rows if r["record_status"] == "collected" and r["backfilled"]),
                conflicted_identities=len(conflicted),
                failures_without_success=len([k for k in failed if k not in accepted]),
                pre_start_rows=sum(1 for r in rows if r["market_session_date"] < start),
                non_trading_rows=sum(1 for r in rows if not calendar.is_trading_day(r["market_session_date"])))


def audit_rows(rows, *, start, calendar, snapshot_lookup=None):
    """Integrity problems only; never outcome statistics."""
    problems = dict(evidence_hash_mismatch=[], registry_mismatch=[], engine_mismatch=[], pre_start=[], non_trading=[],
                    backfilled_inconsistent=[], expected_date_inconsistent=[], snapshot_missing=[],
                    snapshot_hash_mismatch=[], duplicate_accepted=[])
    seen = {}
    for r in rows:
        ident = f"{r['market_session_date']} {r['symbol']} {r['interval']} {r['record_status']} {r['id']}"
        if r["record_status"] in ("collected", "conflict") and evidence_hash(r) != r["evidence_hash"]:
            problems["evidence_hash_mismatch"].append(ident)
        if r["registry_hash"] != REGISTRY_HASH:
            problems["registry_mismatch"].append(ident)
        if r["engine_version"] != ENGINE_VERSION:
            problems["engine_mismatch"].append(ident)
        if r["market_session_date"] < start:
            problems["pre_start"].append(ident)
        if not calendar.is_trading_day(r["market_session_date"]):
            problems["non_trading"].append(ident)
            continue
        if r["expected_collection_date"] != expected_collection_date(r["market_session_date"], calendar):
            problems["expected_date_inconsistent"].append(ident)
        if r["backfilled"] != (r["collected_at"].astimezone(EXCHANGE_TZ).date() > r["expected_collection_date"]):
            problems["backfilled_inconsistent"].append(ident)
        if r["record_status"] == "collected":
            key = (r["market_session_date"], r["symbol"], r["interval"], r["engine_version"], r["registry_hash"])
            if key in seen:
                problems["duplicate_accepted"].append(ident)
            seen[key] = True
            if r["snapshot_timestamp"] is not None and snapshot_lookup is not None:
                stored = snapshot_lookup(r["symbol"], r["interval"], r["snapshot_timestamp"], r["engine_version"])
                if stored is None:
                    problems["snapshot_missing"].append(ident)
                elif stored != r["snapshot_content_hash"]:
                    problems["snapshot_hash_mismatch"].append(ident)
    return problems, sum(len(v) for v in problems.values())


def triggers_present(connection):
    if connection.dialect.name == "postgresql":
        names = set(connection.execute(sa.text(
            "SELECT tgname FROM pg_trigger WHERE tgrelid = 'technical_evidence_ledger'::regclass")).scalars())
        return all(t in names for t in TRIGGERS)
    names = set(connection.execute(sa.text(
        "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='technical_evidence_ledger'")).scalars())
    return all(t in names for t in SQLITE_TRIGGERS)


def snapshot_lookup_for(session):
    t = technical_snapshots

    def lookup(symbol, interval, stamp, engine_version):
        return session.execute(sa.select(t.c.content_hash).where(
            t.c.symbol == symbol, t.c.interval == interval, t.c.snapshot_timestamp == stamp,
            t.c.engine_version == engine_version)).scalar_one_or_none()
    return lookup


def last_completed_session(now, calendar):
    day = now.astimezone(EXCHANGE_TZ).date()
    times = calendar.session_times(day)
    if times is not None and now >= times.close:
        return day
    return calendar.previous_trading_day(day)


def load_rows(session):
    t = technical_evidence_ledger
    return [dict(r._mapping) for r in session.execute(sa.select(t).order_by(t.c.market_session_date, t.c.symbol,
                                                                            t.c.interval, t.c.recorded_at, t.c.id))]


def main(argv=None, environ=None, *, engine=None, calendar=None, now=None, pin=None, out=None):
    from evidence.pin import PinError, load_pin
    from persistence.config import ConfigurationError, DatabaseSettings
    from persistence.database import PersistenceError, make_engine, transaction
    out = out or sys.stdout
    parser = argparse.ArgumentParser(prog="python -m persistence.technical_evidence_tools")
    parser.add_argument("command", choices=("reconcile", "audit"))
    parser.add_argument("--through", type=date.fromisoformat)
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
    owned = engine is None
    try:
        if owned:
            engine = make_engine(DatabaseSettings.from_env(os.environ if environ is None else environ))
    except ConfigurationError as error:
        print(json.dumps(dict(error=str(error))), file=out)
        return 2
    now = now or datetime.now(timezone.utc)
    start = pin["prospective_start_session"]
    try:
        with transaction(engine) as session:
            rows = load_rows(session)
            if args.command == "reconcile":
                through = args.through or last_completed_session(now, calendar)
                result, code = coverage(rows, start=start, through=through, calendar=calendar), 0
            else:
                problems, count = audit_rows(rows, start=start, calendar=calendar,
                                             snapshot_lookup=snapshot_lookup_for(session))
                result = dict(rows=len(rows), problems=count, triggers_present=triggers_present(session.connection()),
                              **problems)
                code = 1 if count or not result["triggers_present"] else 0
    except PersistenceError:
        print(json.dumps(dict(error="database unavailable")), file=out)
        return 1
    finally:
        if owned:
            engine.dispose()
    print(json.dumps(dict(command=args.command, prospective_start_session=start.isoformat(), **result), indent=2,
                     sort_keys=True, default=str), file=out)
    return code


if __name__ == "__main__":
    sys.exit(main())
