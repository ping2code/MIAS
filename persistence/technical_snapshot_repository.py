"""Durable technical snapshots: row mapping, content identity and conflict-safe writes (Phase 4C).

**Identity:** (symbol, interval, snapshot_timestamp, engine_version). ``snapshot_timestamp``
is the start of the completed bar. A new engine version is a different computation,
so it gets its own rows and never rewrites old ones.

**Content:** ``content_hash`` is the SHA-256 of the canonical JSON of every material
field (all technical values and the warm-up context). It excludes the row id,
``created_at`` and the run-time setting ``provider_delay_seconds``.

**Writes** (``store``) are immutable:

- ``inserted``: new identity;
- ``duplicate``: same identity, same content hash; nothing changes;
- ``conflict``: same identity, different content. The existing row is left
  untouched, and the rejected content is recorded once per distinct hash in
  ``technical_snapshot_conflicts`` for audit.

Rows are never updated or deleted by this module. Recomputing history under new
rules means a new ``engine_version``, done by explicit tooling.
"""
from datetime import datetime, timezone
import hashlib
import json
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from persistence.models import (BREAKOUT_STATES, TECHNICAL_INTERVALS, TECHNICAL_STATES, TECHNICAL_TRENDS,
                                technical_snapshot_conflicts, technical_snapshots)

EMA_KEYS = ("ema9", "ema20", "ema50", "ema200")
TIMESTAMP_FIELDS = ("snapshot_timestamp", "warmup_start")
MATERIAL_FIELDS = (
    "symbol", "interval", "snapshot_timestamp", "engine_version", "source_provider", "is_completed_bar", "session_type",
    "price", *EMA_KEYS, "vwap", "rsi14", "atr14", "volume", "average_volume", "relative_volume", "trend",
    "last_high_type", "last_low_type", "significant_high", "significant_low", "gap_type", "gap_percent", "gap_absolute",
    "breakout_state", "breakout_level", "momentum", "ema_alignment", "vwap_position", "technical_state", "confidence",
    "agreeing", "conflicting", "support_levels", "resistance_levels", "reasons", "ema_state", "vwap_state", "evidence",
    "warmup_start", "warmup_bars")
ROW_FIELDS = MATERIAL_FIELDS + ("provider_delay_seconds", "content_hash")


class SnapshotRowError(ValueError):
    """A snapshot cannot be mapped to the durable schema (for example, a non-default EMA/RSI/ATR configuration)."""


def _iso(value):
    return value.astimezone(timezone.utc).isoformat()


def canonical_json(values):
    return json.dumps(values, sort_keys=True, separators=(",", ":"), allow_nan=False)


def content_hash(row):
    return hashlib.sha256(canonical_json({k: row[k] for k in MATERIAL_FIELDS}).encode()).hexdigest()


def snapshot_row(snapshot, *, provider, engine_version, provider_delay_seconds, warmup_start, warmup_bars,
                 session_type=None):
    """JSON-safe row for one completed-bar ``TechnicalSnapshot`` (timestamps as UTC ISO strings)."""
    if tuple(snapshot.ema) != EMA_KEYS:
        raise SnapshotRowError("snapshot EMA periods must be 9/20/50/200 for the durable schema")
    if snapshot.evidence.get("rsi_period") != 14:
        raise SnapshotRowError("snapshot RSI period must be 14 for the durable schema")
    signal = snapshot.signal
    row = dict(
        symbol=snapshot.symbol, interval=snapshot.interval, snapshot_timestamp=_iso(snapshot.timestamp),
        engine_version=engine_version, source_provider=provider, provider_delay_seconds=int(provider_delay_seconds),
        is_completed_bar=True, session_type=session_type, price=snapshot.price,
        **{key: snapshot.ema[key] for key in EMA_KEYS}, vwap=snapshot.vwap, rsi14=snapshot.rsi, atr14=snapshot.atr,
        volume=snapshot.volume, average_volume=snapshot.average_volume, relative_volume=snapshot.relative_volume,
        trend=snapshot.trend, last_high_type=snapshot.last_high_type, last_low_type=snapshot.last_low_type,
        significant_high=snapshot.significant_high, significant_low=snapshot.significant_low,
        gap_type=snapshot.gap_type, gap_percent=snapshot.gap_percent, gap_absolute=snapshot.gap_absolute,
        breakout_state=snapshot.breakout_state, breakout_level=snapshot.breakout_level, momentum=snapshot.momentum,
        ema_alignment=snapshot.ema_state.get("alignment"), vwap_position=snapshot.vwap_state.get("position"),
        technical_state=signal.state, confidence=signal.confidence, agreeing=signal.agreeing,
        conflicting=signal.conflicting, support_levels=list(snapshot.support_levels),
        resistance_levels=list(snapshot.resistance_levels), reasons=list(signal.reasons),
        ema_state=dict(snapshot.ema_state), vwap_state=dict(snapshot.vwap_state), evidence=dict(snapshot.evidence),
        warmup_start=_iso(warmup_start), warmup_bars=int(warmup_bars))
    row = json.loads(canonical_json(row))  # Plain JSON types only (tuples become lists), exactly as hashed.
    row["content_hash"] = content_hash(row)
    return row


def validate_row(row):
    missing = [k for k in ROW_FIELDS if k not in row]
    if missing:
        raise SnapshotRowError(f"snapshot row is missing {', '.join(missing)}")
    checks = (("interval", TECHNICAL_INTERVALS), ("technical_state", TECHNICAL_STATES), ("trend", TECHNICAL_TRENDS),
              ("breakout_state", BREAKOUT_STATES), ("confidence", ("LOW", "MEDIUM", "HIGH")))
    for key, allowed in checks:
        if row[key] not in allowed:
            raise SnapshotRowError(f"snapshot row has an invalid {key}")
    if row["content_hash"] != content_hash(row):
        raise SnapshotRowError("snapshot row content_hash does not match its content")


def _db_values(row):
    values = {k: row[k] for k in ROW_FIELDS}
    for key in TIMESTAMP_FIELDS:
        values[key] = datetime.fromisoformat(row[key])
    return values


class TechnicalSnapshotRepository:
    def __init__(self, session):
        self.session = session

    def store(self, row, *, now=None):
        """Insert, recognise an idempotent duplicate, or record a conflict. Returns a small outcome dict."""
        validate_row(row)
        values = _db_values(row)
        now = now or datetime.now(timezone.utc)
        try:
            with self.session.begin_nested():
                new_id = str(uuid4())
                self.session.execute(sa.insert(technical_snapshots).values(id=new_id, created_at=now, **values))
            return dict(outcome="inserted", id=new_id)
        except IntegrityError:
            pass  # Identity already present (or a concurrent insert won); decide below.
        existing = self.session.execute(sa.select(technical_snapshots.c.id, technical_snapshots.c.content_hash).where(
            technical_snapshots.c.symbol == values["symbol"], technical_snapshots.c.interval == values["interval"],
            technical_snapshots.c.snapshot_timestamp == values["snapshot_timestamp"],
            technical_snapshots.c.engine_version == values["engine_version"])).one_or_none()
        if existing is None:
            raise SnapshotRowError("snapshot violates a table constraint")
        if existing.content_hash == row["content_hash"]:
            return dict(outcome="duplicate", id=existing.id)
        try:
            with self.session.begin_nested():
                self.session.execute(sa.insert(technical_snapshot_conflicts).values(
                    id=str(uuid4()), snapshot_id=existing.id, symbol=values["symbol"], interval=values["interval"],
                    snapshot_timestamp=values["snapshot_timestamp"], engine_version=values["engine_version"],
                    existing_hash=existing.content_hash, rejected_hash=row["content_hash"],
                    rejected_snapshot={k: row[k] for k in MATERIAL_FIELDS}, detected_at=now))
            recorded = True
        except IntegrityError:
            recorded = False  # This exact rejected content was already recorded.
        return dict(outcome="conflict", id=existing.id, conflict_recorded=recorded)

    def snapshots(self, symbol, interval, start=None, end=None, engine_version=None):
        t = technical_snapshots
        query = sa.select(t).where(t.c.symbol == symbol, t.c.interval == interval)
        if start is not None:
            query = query.where(t.c.snapshot_timestamp >= start)
        if end is not None:
            query = query.where(t.c.snapshot_timestamp < end)
        if engine_version is not None:
            query = query.where(t.c.engine_version == engine_version)
        return [dict(r._mapping) for r in self.session.execute(query.order_by(t.c.snapshot_timestamp, t.c.engine_version))]

    def conflicts(self, symbol=None, interval=None):
        c = technical_snapshot_conflicts
        query = sa.select(c)
        if symbol is not None:
            query = query.where(c.c.symbol == symbol)
        if interval is not None:
            query = query.where(c.c.interval == interval)
        return [dict(r._mapping) for r in self.session.execute(query.order_by(c.c.snapshot_timestamp, c.c.detected_at))]


def persist_technical_snapshot(engine, row):
    """One short transaction per snapshot."""
    from persistence.database import transaction
    with transaction(engine) as session:
        return TechnicalSnapshotRepository(session).store(row)
