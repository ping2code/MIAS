"""Phase 6 daily evidence collection: session evidence from the technical runner's bars into the append-only ledger.

This module is called by ``technical.runner`` only when
``TECHNICAL_EVIDENCE_LEDGER_ENABLED=true`` (default false) or ``--evidence-check``.
It never imports ``evaluation``, and it never computes returns or outcome
statistics.

**Identities:** one per (session, symbol, interval) for the frozen intervals 5m, 1h
and 1d:

- 5m bars are fetched directly.
- 1h and 1d are derived from 30m bars, exactly as the runner derives them.
- Only regular-session bars count.

**Eligible sessions** are the XNYS sessions that satisfy all of these:

- on or after the pinned prospective start;
- inside the runner's warm-up window for that interval (natural backfill: 5m 10
  sessions, 1h 90, 1d 450);
- closed at or before the provider's data cutoff (``as_of``);
- not yet accepted in the ledger.

Sessions already accepted are never re-submitted. So a later run can never turn
the snapshot reference (below) into a spurious conflict.

**Completeness (frozen protocol):** the bar count must equal the XNYS expectation
(5m 78 or 42 on an early close; 1h 7 or 4; 1d 1). Anything else is one terminal
``incomplete_session`` failure row for that identity.

**Snapshot reference (soft):** only for the latest eligible session, and only when
the runner's latest snapshot for that interval falls in that session. The
reference is the snapshot identity and its content hash. The Phase 4C warm-up is
anchored to the reference day, so it is deterministic for that day. The ledger
therefore requires shadow snapshot persistence, and the audit verifies that the
row exists.

**Provider failures:** a symbol whose fetch terminally failed, after the client's
normal retry policy, gets one failure row per missing eligible identity.
"""
from dataclasses import dataclass
import math

from evidence.registry import ENGINE_VERSION, PROTOCOL, registry_identity
from market_data.models import EXCHANGE_TZ, Interval

LEDGER_LABELS = PROTOCOL.collection_intervals  # ("5m", "1h", "1d")
PROVIDER_ERROR_KINDS = dict(auth="provider_auth", rate_limit="rate_limit", budget="rate_limit", transport="transport",
                            http="transport", redirect="transport", payload="provider_payload")


class EvidenceConfigError(ValueError):
    """Invalid evidence-ledger configuration; the message names the setting only."""


@dataclass(frozen=True)
class EvidenceSettings:
    enabled: bool


def load_evidence_settings(environ):
    raw = (environ.get("TECHNICAL_EVIDENCE_LEDGER_ENABLED") or "false").strip().lower()
    if raw not in ("true", "false"):
        raise EvidenceConfigError("TECHNICAL_EVIDENCE_LEDGER_ENABLED must be true or false")
    return EvidenceSettings(raw == "true")


def expected_bar_count(day, label, calendar):
    """Frozen completeness rule: regular-session bars per session (5m 78/42, 1h 7/4, 1d 1)."""
    if label == "1d":
        return 1
    times = calendar.session_times(day)
    return math.ceil((times.close - times.open).total_seconds() / Interval.parse(label).seconds)


def last_closed_session(as_of, calendar):
    day = as_of.astimezone(EXCHANGE_TZ).date()
    times = calendar.session_times(day)
    if times is not None and as_of >= times.close:
        return day
    return calendar.previous_trading_day(day)


def eligible_sessions(window_start, as_of, calendar, *, start):
    first = max(start, window_start.astimezone(EXCHANGE_TZ).date())
    last = last_closed_session(as_of, calendar)
    return calendar.trading_days(first, last) if first <= last else []


def session_items(symbol, context, calendar, as_of, *, start, accepted=frozenset(), latest_snapshots=None):
    """Planned identities for one fetched symbol: dicts with session, interval, bars, expected, complete, snapshot.

    ``context[label]`` needs ``series`` (the runner's bars) and ``warmup_start``.
    ``latest_snapshots`` maps a label to ``(timestamp, content_hash)`` of the runner's
    latest snapshot, or to None.
    """
    items = []
    for label in LEDGER_LABELS:
        if label not in context:
            continue
        by_day = {}
        for bar in context[label]["series"]:
            if bar.regular:
                by_day.setdefault(bar.timestamp.astimezone(EXCHANGE_TZ).date(), []).append(bar)
        days = eligible_sessions(context[label]["warmup_start"], as_of, calendar, start=start)
        snapshot = (latest_snapshots or {}).get(label)
        for day in days:
            if (day, symbol, label) in accepted:
                continue
            bars = by_day.get(day, [])
            expected = expected_bar_count(day, label, calendar)
            reference = None
            if snapshot is not None and day == days[-1] and snapshot[0].astimezone(EXCHANGE_TZ).date() == day:
                reference = snapshot
            items.append(dict(symbol=symbol, interval=label, session=day, bars=bars, expected=expected,
                              complete=len(bars) == expected, snapshot=reference))
    return items


def missing_items(symbol, labels, warmup_starts, calendar, as_of, *, start, accepted=frozenset()):
    """Eligible identities for a symbol whose fetch failed (no bars): the targets of the failed attempt."""
    return [dict(symbol=symbol, interval=label, session=day)
            for label in labels if label in LEDGER_LABELS
            for day in eligible_sessions(warmup_starts[label], as_of, calendar, start=start)
            if (day, symbol, label) not in accepted]


def build_rows(items, *, failures=(), provider_settings, code_commit, collected_at, calendar,
               engine_version=ENGINE_VERSION):
    """Ledger rows for planned items: complete sessions become evidence, others incomplete_session failures."""
    from persistence.technical_evidence_ledger import build_evidence, build_failure
    rows = []
    common = dict(engine_version=engine_version, registry=registry_identity(), provider_settings=provider_settings,
                  code_commit=code_commit, collected_at=collected_at, calendar=calendar)
    for item in items:
        ident = dict(session_date=item["session"], symbol=item["symbol"], interval=item["interval"], **common)
        if item["complete"]:
            stamp, digest = item["snapshot"] or (None, None)
            rows.append(build_evidence(bars=item["bars"], snapshot_timestamp=stamp, snapshot_content_hash=digest,
                                       **ident))
        else:
            rows.append(build_failure(error_kind="incomplete_session",
                                      error_detail=f"bars {len(item['bars'])} expected {item['expected']}", **ident))
    for item, kind, detail in failures:
        rows.append(build_failure(error_kind=kind, error_detail=detail, session_date=item["session"],
                                  symbol=item["symbol"], interval=item["interval"], **common))
    return rows


def accepted_keys(session):
    """(session, symbol, interval) already accepted under the frozen registry and engine."""
    import sqlalchemy as sa
    from evidence.registry import REGISTRY_HASH
    from persistence.models import technical_evidence_ledger as t
    query = sa.select(t.c.market_session_date, t.c.symbol, t.c.interval).where(
        t.c.record_status == "collected", t.c.registry_hash == REGISTRY_HASH, t.c.engine_version == ENGINE_VERSION)
    return {tuple(r) for r in session.execute(query)}


def write_rows(session, rows):
    """Insert-only writes; returns outcome counts (inserted, duplicate, conflict, failed)."""
    from persistence.technical_evidence_ledger import EvidenceLedgerRepository
    repo = EvidenceLedgerRepository(session)
    counts = dict(inserted=0, duplicate=0, conflict=0, failed=0)
    for row in rows:
        if row["record_status"] == "failed":
            repo.record_failure(row)
            counts["failed"] += 1
        else:
            counts[repo.record_collected(row)["outcome"]] += 1
    return counts


def check_summary(items):
    """Operational-validation view: completeness and hashes only (no prices, states or returns)."""
    summary = {}
    for item in items:
        entry = summary.setdefault(f"{item['symbol']} {item['interval']}", dict(sessions=0, complete=0, incomplete=[],
                                                                               snapshot_reference=None))
        entry["sessions"] += 1
        entry["complete"] += item["complete"]
        if not item["complete"]:
            entry["incomplete"].append(f"{item['session'].isoformat()} bars={len(item['bars'])}/{item['expected']}")
        if item["snapshot"] is not None:
            entry["snapshot_reference"] = item["snapshot"][0].isoformat()
    return summary
