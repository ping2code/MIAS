"""Phase 6 prospective start pin: resolving and validating the frozen start rule.

The frozen rule (``evidence.registry.PROTOCOL.prospective_start_rule``): the first
XNYS session whose **regular-market open is strictly after** the committer
timestamp of the Phase 6 freeze commit.

A commit cannot contain its own hash, so the resolved values live in
``evidence/prospective_start.json``, committed right after the freeze commit:

- ``prospective_freeze_commit``, ``freeze_commit_time``;
- ``prospective_start_session``, ``earliest_evaluation_session``;
- ``registry_hash``.

``validate_pin`` recomputes everything from the calendar and the frozen registry.
The collector refuses to write ledger rows without a valid pin, and so does the
validator.

    python -m evidence.pin --commit <sha>    # prints the pin JSON for a freeze commit (uses git; no network)
"""
import argparse
from datetime import date, datetime
import json
import os
import re
import subprocess
import sys

from evidence.registry import PROTOCOL, REGISTRY_HASH, REGISTRY_VERSION
from market_data.models import EXCHANGE_TZ

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PIN_PATH = os.path.join(ROOT, "evidence", "prospective_start.json")
COMMIT = re.compile(r"[0-9a-f]{40}")
MONTHS = 6


class PinError(ValueError):
    """The prospective start is not pinned, or the pin is inconsistent with the frozen rule."""


def first_session_open_after(moment, calendar):
    day = moment.astimezone(EXCHANGE_TZ).date()
    while True:
        times = calendar.session_times(day)
        if times is not None and times.open > moment:
            return day
        day = calendar.next_trading_day(day)


def add_months(day, months):
    month = day.month - 1 + months
    year, month = day.year + month // 12, month % 12 + 1
    for candidate in range(day.day, 27, -1) if day.day > 28 else (day.day,):
        try:
            return date(year, month, candidate)
        except ValueError:
            continue
    return date(year, month, 28)


def earliest_evaluation_session(start_session, calendar):
    """First XNYS session on or after start + 6 calendar months (frozen protocol rule)."""
    day = add_months(start_session, MONTHS)
    return day if calendar.is_trading_day(day) else calendar.next_trading_day(day)


def build_pin(commit, commit_time, calendar):
    if not COMMIT.fullmatch(commit or ""):
        raise PinError("freeze commit must be a full 40-character lowercase hex SHA")
    if commit_time.utcoffset() is None:
        raise PinError("freeze commit time must be timezone-aware")
    start = first_session_open_after(commit_time, calendar)
    return dict(pin_format_version="phase6-v1", registry_version=REGISTRY_VERSION, registry_hash=REGISTRY_HASH,
                prospective_freeze_commit=commit, freeze_commit_time=commit_time.isoformat(),
                prospective_start_session=start.isoformat(),
                earliest_evaluation_session=earliest_evaluation_session(start, calendar).isoformat(),
                min_complete_sessions=PROTOCOL.min_complete_sessions, rule=PROTOCOL.prospective_start_rule)


def validate_pin(pin, calendar):
    """The validated pin (with dates parsed) or PinError; recomputes every derived field."""
    if not isinstance(pin, dict):
        raise PinError("prospective start is not pinned (evidence/prospective_start.json missing)")
    try:
        commit_time = datetime.fromisoformat(pin["freeze_commit_time"])
        expected = build_pin(pin["prospective_freeze_commit"], commit_time, calendar)
    except (KeyError, TypeError, ValueError) as error:
        raise PinError(f"prospective start pin is malformed: {error}") from None
    if pin.get("registry_hash") != REGISTRY_HASH:
        raise PinError("pin registry_hash differs from the frozen registry")
    for key in ("prospective_start_session", "earliest_evaluation_session", "registry_version", "min_complete_sessions"):
        if pin.get(key) != expected[key]:
            raise PinError(f"pin {key} is inconsistent with the frozen rule")
    return dict(pin, prospective_start_session=date.fromisoformat(pin["prospective_start_session"]),
                earliest_evaluation_session=date.fromisoformat(pin["earliest_evaluation_session"]))


def load_pin(path=PIN_PATH, calendar=None):
    from market_data.calendar import default_calendar
    if not os.path.exists(path):
        raise PinError("prospective start is not pinned (evidence/prospective_start.json missing)")
    with open(path) as handle:
        return validate_pin(json.load(handle), calendar or default_calendar())


def commit_time_of(commit):
    raw = subprocess.run(["git", "-C", ROOT, "show", "-s", "--format=%cI", commit], capture_output=True, text=True,
                         timeout=30, check=True).stdout.strip()
    return datetime.fromisoformat(raw)


def current_commit():
    """HEAD of this checkout, from git (a code_commit for ledger rows), or None if unavailable."""
    try:
        return subprocess.run(["git", "-C", ROOT, "rev-parse", "HEAD"], capture_output=True, text=True, timeout=30,
                              check=True).stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def main(argv=None, out=None):
    from market_data.calendar import default_calendar
    parser = argparse.ArgumentParser(prog="python -m evidence.pin")
    parser.add_argument("--commit", required=True)
    args = parser.parse_args(argv)
    try:
        pin = build_pin(args.commit, commit_time_of(args.commit), default_calendar())
    except (PinError, subprocess.SubprocessError, ValueError) as error:
        print(f"pin error: {error}", file=sys.stderr)
        return 2
    print(json.dumps(pin, indent=2), file=out or sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
