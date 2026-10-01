"""SessionSchedule (``phase11-sessions-v1``): a sealed, content-addressed list of regular XNYS sessions.

Each session is ``{session_date, regular_open, regular_close}`` with canonical UTC instants. Sessions are strictly
increasing. The sealed schedule supplied to an evaluation is authoritative. It is built outside the pure core,
from ``market_data.calendar``, preferably after the target date, so known ad-hoc closures and early closes are
represented. The pure core never imports a calendar library.

Horizons: ``session_n`` is the n-th session whose ``regular_open`` is strictly later than ``assessment_as_of``.
Only full forward sessions count.
"""
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from setup_evaluation import model as m
from setup_evaluation import rules as r
from setup_evaluation.canonical import content_id


class ScheduleError(ValueError):
    """The schedule is invalid or does not cover a horizon (the message names the problem)."""


def _require(condition, message):
    if not condition:
        raise ScheduleError(message)


def _instant(value):
    if not isinstance(value, str):
        raise ScheduleError("schedule instants must be ISO 8601 strings")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ScheduleError("schedule instants must be ISO 8601 strings") from None
    _require(parsed.utcoffset() == timedelta(0) and parsed.isoformat() == value,
             "schedule instants must be canonical UTC")
    return parsed


def make_schedule(sessions, calendar="XNYS"):
    """Seal ``[(session_date, regular_open, regular_close), ...]`` (dates or ISO strings; aware datetimes or
    canonical UTC strings)."""
    rows = []
    for day, open_, close in sessions:
        open_, close = (v.astimezone(timezone.utc).isoformat() if isinstance(v, datetime) else v for v in (open_, close))
        rows.append(m.Session(day if isinstance(day, str) else day.isoformat(), open_, close))
    draft = m.SessionSchedule(r.SCHEDULE_FORMAT_VERSION, "", calendar, tuple(rows))
    schedule = replace(draft, schedule_id=content_id(draft.body()))
    validated_schedule(schedule)
    return schedule


def validated_schedule(schedule):
    """The plain schedule dict, validated: keys, version, id, canonical instants, strictly increasing sessions."""
    data = schedule.to_dict() if isinstance(schedule, m.SessionSchedule) else schedule
    _require(isinstance(data, dict) and set(data) == set(m.SCHEDULE_FIELDS),
             "schedule must have exactly the phase11-sessions-v1 keys")
    _require(data["schedule_format_version"] == r.SCHEDULE_FORMAT_VERSION, "unsupported schedule format version")
    _require(isinstance(data["schedule_id"], str)
             and content_id({k: v for k, v in data.items() if k != "schedule_id"}) == data["schedule_id"],
             "schedule_id does not match the schedule (tampered or corrupt)")
    _require(data["calendar"] == "XNYS", "schedule calendar must be XNYS")
    sessions = data["sessions"]
    _require(isinstance(sessions, list) and sessions, "schedule sessions must be a non-empty list")
    previous = None
    for s in sessions:
        _require(isinstance(s, dict) and set(s) == {"session_date", "regular_open", "regular_close"}
                 and isinstance(s["session_date"], str) and r.DATE.fullmatch(s["session_date"]),
                 "schedule session is malformed")
        open_, close = _instant(s["regular_open"]), _instant(s["regular_close"])
        _require(open_ < close, "schedule session opens after it closes")
        _require(previous is None or (s["session_date"] > previous[0] and open_ > previous[1]),
                 "schedule sessions must be strictly increasing")
        previous = (s["session_date"], close)
    return data


def resolve(schedule, assessment_as_of, n):
    """(session dict) for the n-th session whose regular_open is strictly later than ``assessment_as_of``.

    The schedule must cover the anchor: its first session must open at or before ``assessment_as_of``, so no
    earlier forward session can be missing from its start. It must also contain at least n forward sessions."""
    data = validated_schedule(schedule)
    sessions = data["sessions"]
    _require(_instant(sessions[0]["regular_open"]) <= assessment_as_of,
             "schedule does not cover the assessment time (it must start at or before assessment_as_of)")
    forward = [s for s in sessions if _instant(s["regular_open"]) > assessment_as_of]
    _require(len(forward) >= n, "schedule does not cover the horizon")
    return forward[n - 1]
