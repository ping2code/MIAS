"""Phase 6 prospective collection status (no-peek): coverage and gate progress only, never outcome statistics.

    python -m evaluation.prospective_status [--as-of YYYY-MM-DD]

This command reads the pin and the evidence ledger only. It never fetches bars and
never computes states, returns or hypothesis statistics.

It reports:

- the frozen registry hashes, and whether they match the pin;
- the prospective start and the earliest evaluation session;
- complete sessions against the requirement, and whether the gate is open;
- expected, accepted and missing identities; backfilled rows, conflicts, and
  failures without a later success.

Exit codes:

- 0: status printed;
- 2: no valid pin, or configuration error;
- 1: database unavailable.
"""
import argparse
from datetime import date, datetime, timezone
import json
import os
import sys

from evaluation.prospective import gate
from evidence.registry import HYPOTHESIS_HASHES, PROSPECTIVE_FORMAT_VERSION, PROTOCOL_HASH, REGISTRY_HASH


def status(rows, pin, calendar, now):
    from market_data.models import EXCHANGE_TZ
    from persistence.technical_evidence_tools import coverage, last_completed_session
    start = pin["prospective_start_session"]
    through = last_completed_session(now, calendar)
    cover = coverage(rows, start=start, through=through, calendar=calendar)
    state = gate(pin, cover["complete_sessions"], now.astimezone(EXCHANGE_TZ).date())
    return dict(prospective_format_version=PROSPECTIVE_FORMAT_VERSION, registry_hash=REGISTRY_HASH,
                protocol_hash=PROTOCOL_HASH, hypothesis_hashes=HYPOTHESIS_HASHES,
                prospective_freeze_commit=pin.get("prospective_freeze_commit"),
                prospective_start_session=start.isoformat(), through_session=through.isoformat(),
                gate=dict(state, status="OPEN" if state["open"] else "LOCKED"),
                coverage={k: v for k, v in cover.items() if k not in ("missing_identities", "complete_session_dates")},
                missing_identities=len(cover["missing_identities"]),
                note="collection status only; no hypothesis statistics are computed before the gate")


def main(argv=None, environ=None, *, engine=None, calendar=None, now=None, pin=None, out=None):
    from evidence.pin import PinError, load_pin
    from persistence.config import ConfigurationError, DatabaseSettings
    from persistence.database import PersistenceError, make_engine, transaction
    from persistence.technical_evidence_tools import load_rows
    out = out or sys.stdout
    parser = argparse.ArgumentParser(prog="python -m evaluation.prospective_status")
    parser.add_argument("--as-of", type=date.fromisoformat, help="report as of this date's end (default: now)")
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
    if args.as_of:
        now = datetime.combine(args.as_of, datetime.max.time(), tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    owned = engine is None
    try:
        if owned:
            engine = make_engine(DatabaseSettings.from_env(os.environ if environ is None else environ))
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
    print(json.dumps(status(rows, pin, calendar, now), indent=2, sort_keys=True), file=out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
