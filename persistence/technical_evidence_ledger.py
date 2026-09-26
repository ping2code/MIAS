"""Append-only prospective evidence ledger repository (Phase 6, migration 0007).

The ledger records collection, provenance and integrity only. It never holds
returns, technical states, prices, raw bars, vendor payloads or keys.

**Evidence identity:** ``(market_session_date, symbol, interval, engine_version,
registry_hash)``. At most one accepted (``collected``) row per identity is enforced
by a partial unique index.

**``evidence_hash``:** the evidence content and configuration identity. It is the
SHA-256 of the canonical JSON of the material fields. It excludes execution
metadata (row id, ``collected_at``, ``recorded_at``, ``code_commit``,
``backfilled``, ``expected_collection_date``). So an identical retry, even from
newer code, is a **duplicate** that preserves the original row's provenance.

**``bar_content_hash``:** SHA-256 of ``bars-v1|symbol|interval|session`` plus one
canonical line per completed session bar (UTC ISO timestamp and exact decimal
OHLCV). It is one-way; bars are never stored.

**Writes are insert-only.** There is no update or delete API, and database
triggers reject UPDATE, DELETE and TRUNCATE.

- ``record_collected`` returns ``inserted``, ``duplicate`` (same evidence hash; no
  row written) or ``conflict`` (a different hash appends one conflict row per
  distinct hash; the original is never replaced).
- ``record_failure`` appends one row per *terminal* failed collection attempt
  (never per internal HTTP retry). A later success is a normal ``collected`` row.
"""
from datetime import date, datetime, timezone
import hashlib
import json
import re
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from market_data.models import EXCHANGE_TZ, format_decimal
from persistence.models import LEDGER_ERROR_KINDS, LEDGER_INTERVALS, technical_evidence_ledger

FORMAT_VERSION = "phase6-v1"
COMMIT = re.compile(r"[0-9a-f]{7,40}")
MATERIAL = ("ledger_format_version", "market_session_date", "symbol", "interval", "engine_version", "registry_hash",
            "provider", "adjusted", "data_delay_seconds", "include_extended_hours", "bar_count", "bar_content_hash",
            "snapshot_timestamp", "snapshot_content_hash")
SAFE_DETAIL = re.compile(r"[^A-Za-z0-9 _.:/=,()\-+%]")


class LedgerError(ValueError):
    """Invalid evidence: refused before any write (fail closed)."""


def _canonical(values):
    return json.dumps(values, sort_keys=True, separators=(",", ":"), allow_nan=False)


def bar_content_hash(bars, *, symbol, interval, session_date):
    lines = [f"bars-v1|{symbol}|{interval}|{session_date.isoformat()}"]
    for bar in sorted(bars, key=lambda b: b.timestamp):
        stamp = bar.timestamp.astimezone(timezone.utc).isoformat()
        lines.append("|".join([stamp, *(format_decimal(v) for v in (bar.open, bar.high, bar.low, bar.close,
                                                                     bar.volume))]))
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()


def evidence_hash(row):
    material = {k: row[k] for k in MATERIAL}
    for key in ("market_session_date", "snapshot_timestamp"):
        value = material[key]
        if isinstance(value, datetime):
            material[key] = value.astimezone(timezone.utc).isoformat()
        elif isinstance(value, date):
            material[key] = value.isoformat()
    return hashlib.sha256(_canonical(material).encode()).hexdigest()


def expected_collection_date(session_date, calendar):
    return calendar.next_trading_day(session_date)


def _base(*, session_date, symbol, interval, engine_version, registry, provider_settings, code_commit, collected_at,
          calendar):
    if not calendar.is_trading_day(session_date):
        raise LedgerError("market_session_date is not an XNYS trading day")
    if interval not in LEDGER_INTERVALS:
        raise LedgerError("unsupported evidence interval")
    if not COMMIT.fullmatch(code_commit or ""):
        raise LedgerError("code_commit must be 7-40 lowercase hex characters")
    if collected_at.utcoffset() is None:
        raise LedgerError("collected_at must be timezone-aware")
    expected = expected_collection_date(session_date, calendar)
    return dict(ledger_format_version=FORMAT_VERSION, market_session_date=session_date, symbol=symbol,
                interval=interval, engine_version=engine_version, registry_version=registry["version"],
                registry_hash=registry["hash"], hypothesis_hashes=dict(registry["hypotheses"]),
                provider=provider_settings.provider, adjusted=bool(provider_settings.adjusted),
                data_delay_seconds=int(provider_settings.delay_seconds),
                include_extended_hours=bool(provider_settings.include_extended_hours), code_commit=code_commit,
                collected_at=collected_at, expected_collection_date=expected,
                backfilled=collected_at.astimezone(EXCHANGE_TZ).date() > expected)


def build_evidence(*, bars, snapshot_timestamp=None, snapshot_content_hash=None, **kwargs):
    row = _base(**kwargs)
    if not bars:
        raise LedgerError("collected evidence needs at least one completed bar")
    if any(b.symbol != row["symbol"] or b.interval.label != row["interval"] for b in bars):
        raise LedgerError("bars do not match the evidence symbol/interval")
    if any(b.timestamp.astimezone(EXCHANGE_TZ).date() != row["market_session_date"] for b in bars):
        raise LedgerError("bars fall outside the evidence session")
    if (snapshot_timestamp is None) != (snapshot_content_hash is None):
        raise LedgerError("snapshot reference needs both timestamp and content hash")
    row.update(record_status="collected", bar_count=len(bars),
               bar_content_hash=bar_content_hash(bars, symbol=row["symbol"], interval=row["interval"],
                                                 session_date=row["market_session_date"]),
               snapshot_timestamp=snapshot_timestamp, snapshot_content_hash=snapshot_content_hash,
               error_kind=None, error_detail=None, conflicts_with=None)
    row["evidence_hash"] = evidence_hash(row)
    return row


def sanitize_detail(text):
    return SAFE_DETAIL.sub("_", str(text or ""))[:200]


def build_failure(*, error_kind, error_detail="", **kwargs):
    if error_kind not in LEDGER_ERROR_KINDS:
        raise LedgerError("unknown error_kind")
    row = _base(**kwargs)
    row.update(record_status="failed", bar_count=None, bar_content_hash=None, snapshot_timestamp=None,
               snapshot_content_hash=None, evidence_hash=None, error_kind=error_kind,
               error_detail=sanitize_detail(error_detail), conflicts_with=None)
    return row


class EvidenceLedgerRepository:
    """Insert-only access. There is deliberately no update or delete method."""

    def __init__(self, session):
        self.session = session

    def _insert(self, row, now):
        values = dict(row, id=str(uuid4()), recorded_at=now or datetime.now(timezone.utc))
        self.session.execute(sa.insert(technical_evidence_ledger).values(**values))
        return values["id"]

    def record_collected(self, row, *, now=None):
        if row.get("record_status") != "collected" or row["evidence_hash"] != evidence_hash(row):
            raise LedgerError("collected row is malformed or its evidence_hash does not match")
        try:
            with self.session.begin_nested():
                return dict(outcome="inserted", id=self._insert(row, now))
        except IntegrityError:
            pass  # The identity already has accepted evidence (or a concurrent insert won).
        t = technical_evidence_ledger
        accepted = self.session.execute(sa.select(t.c.id, t.c.evidence_hash).where(
            t.c.record_status == "collected", t.c.market_session_date == row["market_session_date"],
            t.c.symbol == row["symbol"], t.c.interval == row["interval"],
            t.c.engine_version == row["engine_version"], t.c.registry_hash == row["registry_hash"])).one_or_none()
        if accepted is None:
            raise LedgerError("evidence row violates a ledger constraint")
        if accepted.evidence_hash == row["evidence_hash"]:
            return dict(outcome="duplicate", id=accepted.id)
        conflict = dict(row, record_status="conflict", conflicts_with=accepted.id)
        try:
            with self.session.begin_nested():
                return dict(outcome="conflict", id=accepted.id, conflict_id=self._insert(conflict, now))
        except IntegrityError:
            return dict(outcome="conflict", id=accepted.id, conflict_id=None)  # Already recorded once.

    def record_failure(self, row, *, now=None):
        if row.get("record_status") != "failed":
            raise LedgerError("failure row is malformed")
        return dict(outcome="recorded", id=self._insert(row, now))

    def rows(self, *, status=None, symbol=None, interval=None, start=None, end=None):
        t = technical_evidence_ledger
        query = sa.select(t)
        for column, value in ((t.c.record_status, status), (t.c.symbol, symbol), (t.c.interval, interval)):
            if value is not None:
                query = query.where(column == value)
        if start is not None:
            query = query.where(t.c.market_session_date >= start)
        if end is not None:
            query = query.where(t.c.market_session_date <= end)
        order = (t.c.market_session_date, t.c.symbol, t.c.interval, t.c.recorded_at, t.c.id)
        return [dict(r._mapping) for r in self.session.execute(query.order_by(*order))]

    def accepted(self, **filters):
        return self.rows(status="collected", **filters)

    def conflicts(self, **filters):
        return self.rows(status="conflict", **filters)

    def failures(self, **filters):
        return self.rows(status="failed", **filters)
