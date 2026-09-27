"""Phase 7D loader: read-only, zero-network loading of durable MIAS evidence into Phase 7C assembler inputs.

This is the only module in ``evidence_packet`` that performs I/O: PostgreSQL reads and
reading the supplied MarketContext file. The Phase 7C modules (models, adapters,
assembler, serialization) stay pure. The loader never writes, never migrates and
never calls a network service.

**MarketContext:** read from an explicit JSON file produced by
``market_context.runner``. The file may be that runner's output (``contexts``
list) or a single ``MarketContext.to_dict()``.

- It is rebuilt into the typed ``market_context.models.MarketContext`` and must
  round-trip byte-for-byte: rebuilt ``to_dict()`` must equal the file.
- Its symbol must match, and its ``now`` and ``as_of`` must both be at or before
  the packet ``as_of``.

**Technical (1d, 1h, 5m):**

- read with the existing ``TechnicalSnapshotRepository.snapshots``
  (``engine_version=phase4c-v2``);
- eligible rows have ``ExchangeCalendar.bar_end <= as_of`` **and**
  ``created_at <= as_of``, both inclusive;
- the latest eligible bar is chosen;
- rows are converted with the existing ``row_from_db`` and verified by the Phase 7C
  adapter (content hash, vocabularies, symbol, interval).

**News and SEC (read-only SELECTs over the existing table metadata):**

- **Publication defines the lookback window:** ``(as_of - lookback, as_of]``.
  ``observed_at`` is only an upper-bound availability check (``observed_at <=
  as_of``), so future-observed versions are never loaded.
- Candidate events have at least one version available by ``as_of`` whose
  publication is inside the window (timestamp after the window start, or a filing
  date on or after the window start's New York date).
- Items with an unknown publication time cannot be placed by publication, so only
  those fall back to ``observed_at`` inside the window. That keeps them bounded,
  and Phase 7C counts them as ``unknown_publication_time``.
- Per event, the **latest version with ``observed_at <= as_of``** is used, never
  ``current_version_id``.
- Score and decision history uses the latest row with ``recorded_at <= as_of``.
  AI history is never read.
- Provenance uses the latest row with ``retrieved_at <= as_of``.
- The collector event is rebuilt from those rows and must reproduce the durable
  identity: ``news-url-v1`` / ``news-fingerprint-v1`` via ``article_identity``;
  ``sec-v1`` via the stored fingerprint key.
- If that latest version's publication lies outside the window (an earlier version
  was inside it), the event is left out and counted as ``outside_lookback``
  (runner diagnostics only).
"""
from dataclasses import dataclass, fields
from datetime import date, datetime, timedelta
from decimal import Decimal
import json

import sqlalchemy as sa

from evidence_packet import models as m
from evidence_packet.adapters import AdapterError, adapt_technical_row
from market_context import models as mc
from market_data.models import EXCHANGE_TZ

ENGINE_VERSION = "phase4c-v2"
FAMILIES = ("news", "sec")
MAX_LOOKBACK_HOURS = 720
NEWS_SCORE_FIELDS = ("impact_score", "impact_level", "score_reasons", "original_impact_score", "quality_adjustment")


class LoadError(Exception):
    """A classified loader failure. ``exit_code``: 2 input, 3 source incomplete, 4 integrity, 5 database."""
    exit_code = 2

    def __init__(self, message):
        super().__init__(message)


class InputError(LoadError):
    exit_code = 2


class SourceIncomplete(LoadError):
    exit_code = 3


class IntegrityFailure(LoadError):
    exit_code = 4


class DatabaseFailure(LoadError):
    exit_code = 5


# --------------------------------------------------------------------------- MarketContext file

DATETIME_FIELDS = {"now", "as_of", "latest_bar_end", "expected_latest_bar_end", "latest_bar_start", "cutoff",
                   "symbol_bar_end", "benchmark_bar_end"}
DATE_FIELDS = {"session_date", "previous_close_session"}
DECIMAL_FIELDS = {"session_open", "session_high", "session_low", "latest_close", "previous_close",
                  "return_since_prev_close", "return_since_open", "session_range", "position_in_range",
                  "distance_from_high", "distance_from_low", "session_volume", "symbol_return", "benchmark_return",
                  "relative_return"}
FLOAT_FIELDS = {"vwap", "close_vs_vwap"}


def _scalar(name, value):
    if value is None:
        return None
    try:
        if name in DATETIME_FIELDS:
            parsed = datetime.fromisoformat(value)
            if parsed.utcoffset() is None:
                raise ValueError
            return parsed
        if name in DATE_FIELDS:
            return date.fromisoformat(value)
        if name in DECIMAL_FIELDS:
            if not isinstance(value, str):
                raise ValueError
            return Decimal(value)
        if name in FLOAT_FIELDS:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError
            return float(value)
    except (TypeError, ValueError, ArithmeticError):
        raise InputError(f"market context field {name} is malformed") from None
    return value


def _build(cls, data):
    if not isinstance(data, dict) or set(data) != {f.name for f in fields(cls)}:
        raise InputError(f"market context {cls.__name__} has unexpected or missing fields")
    values = {}
    for f in fields(cls):
        value = data[f.name]
        if f.name == "freshness":
            value = None if value is None else _build(mc.Freshness, value)
        elif f.name == "symbol_context":
            value = _build(mc.SeriesContext, value)
        elif f.name == "benchmark_contexts":
            value = tuple(_build(mc.SeriesContext, item) for item in _list(value))
        elif f.name == "comparisons":
            value = tuple(_build(mc.BenchmarkComparison, item) for item in _list(value))
        elif f.name == "unavailable":
            value = tuple(_list(value))
        elif f.name == "provenance":
            if not isinstance(value, dict):
                raise InputError("market context provenance must be an object")
        else:
            value = _scalar(f.name, value)
        values[f.name] = value
    return cls(**values)


def _list(value):
    if not isinstance(value, list):
        raise InputError("market context list field is malformed")
    return value


def market_context_from_dict(data):
    """Typed MarketContext rebuilt from its to_dict(); rebuilt.to_dict() must reproduce the input exactly."""
    context = _build(mc.MarketContext, data)
    if context.context_format_version != mc.CONTEXT_FORMAT_VERSION:
        raise InputError("unsupported market context format version")
    if json.dumps(context.to_dict(), sort_keys=True) != json.dumps(data, sort_keys=True):
        raise IntegrityFailure("market context file does not round-trip to the same canonical content")
    return context


def load_market_context(path, *, symbol, as_of):
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        raise SourceIncomplete("market context file not found") from None
    except (OSError, UnicodeDecodeError, ValueError):
        raise InputError("market context file is unreadable or not JSON") from None
    if isinstance(data, dict) and "contexts" in data:  # market_context.runner output
        matches = [c for c in data["contexts"] if isinstance(c, dict) and c.get("symbol") == symbol] \
            if isinstance(data["contexts"], list) else []
        if len(matches) != 1:
            raise InputError(f"market context file must contain exactly one context for {symbol}")
        data = matches[0]
    context = market_context_from_dict(data)
    if context.symbol != symbol:
        raise InputError("market context symbol does not match --symbol")
    if context.as_of > as_of or context.now > as_of:
        raise InputError("market context is later than as_of (its now/as_of must be at or before the packet as_of)")
    return context


# --------------------------------------------------------------------------- technical snapshots

def _snapshot_json(stored):
    from persistence.technical_snapshot_repository import row_from_db
    row = row_from_db(stored)
    row["provider_delay_seconds"] = int(stored["provider_delay_seconds"])
    row["content_hash"] = stored["content_hash"]
    return row


def load_technical(session, *, symbol, as_of, calendar):
    """({interval: snapshot_row}, {interval: diagnostic}) for the latest eligible snapshot per interval."""
    from persistence.technical_snapshot_repository import TechnicalSnapshotRepository
    repo = TechnicalSnapshotRepository(session)
    rows, diagnostics = {}, {}
    for interval in m.TECHNICAL_INTERVALS:
        # Repository end bound is exclusive on the bar START; any bar ending at or before as_of starts before it.
        stored = repo.snapshots(symbol, interval, end=as_of, engine_version=ENGINE_VERSION)
        eligible = []
        for record in stored:
            if not bool(record["is_completed_bar"]) or record["created_at"] > as_of:
                continue
            row = _snapshot_json(record)
            try:
                evidence = adapt_technical_row(row, symbol=symbol, interval=interval, calendar=calendar)
            except AdapterError as error:
                raise IntegrityFailure(f"stored {interval} snapshot failed verification: {error.reason}") from None
            if evidence.bar_end <= as_of:
                eligible.append((record["snapshot_timestamp"], row, evidence.bar_end))
        if eligible:
            stamp, row, bar_end = max(eligible, key=lambda item: item[0])
            rows[interval] = row
            diagnostics[interval] = dict(status="found", snapshot_timestamp=row["snapshot_timestamp"],
                                         bar_end=bar_end.astimezone(EXCHANGE_TZ).isoformat(),
                                         candidates=len(stored))
        else:
            diagnostics[interval] = dict(status="missing", candidates=len(stored))
    return rows, diagnostics


# --------------------------------------------------------------------------- news / SEC events

@dataclass(frozen=True)
class EventLoad:
    inputs: tuple
    diagnostics: dict


def _latest(rows, key):
    return max(rows, key=key) if rows else None


def _outside_lookback(version, window_start):
    """Publication defines the window; only an unknown publication time falls back to observed_at."""
    if version["published_at"] is not None:
        return version["published_at"] <= window_start
    if version["publication_date"] is not None:
        return version["publication_date"] < window_start.astimezone(EXCHANGE_TZ).date()
    return version["observed_at"] <= window_start  # Unknown: Phase 7C counts it as unknown_publication_time.


def _news_event(version, prov, score, decision):
    from persistence.adapters.news import article_identity
    attrs, pattrs = version["attributes"] or {}, prov["attributes"] or {}
    event = dict(source=pattrs.get("feed") or prov["source_name"], publisher=version["publisher"],
                 headline=version["headline"], url=prov["canonical_url"], published_at=pattrs.get("published_at"),
                 summary=version["summary"], symbols=attrs.get("symbols"), direct_symbols=attrs.get("direct_symbols"),
                 related_symbols=attrs.get("related_symbols"), relevant=attrs.get("relevant"),
                 news_fingerprint=pattrs.get("collector_fingerprint"),
                 alert_decision=decision.get("alert_decision"),
                 **{k: score[k] for k in NEWS_SCORE_FIELDS if k in score})
    try:
        identity = article_identity(event)
    except (KeyError, TypeError):
        identity = None
    return event, identity


def _sec_event(version, prov, score, decision, event_key):
    attrs, pattrs = version["attributes"] or {}, prov["attributes"] or {}
    filed = pattrs.get("filing_date")
    if filed is None and version["publication_date"] is not None:
        filed = version["publication_date"].isoformat()
    event = dict(source=version["source_name"], publisher=version["publisher"], headline=version["headline"],
                 url=version["canonical_url"], accession_number=attrs.get("accession_number"),
                 sec_form=attrs.get("sec_form"), published_at=filed, summary=version["summary"],
                 symbols=attrs.get("symbols"), direct_symbols=attrs.get("direct_symbols"),
                 related_symbols=attrs.get("related_symbols"), relevant=attrs.get("relevant"),
                 event_type=version["event_type"], sec_fingerprint=event_key,
                 alert_decision=decision.get("alert_decision"),
                 **{k: score[k] for k in NEWS_SCORE_FIELDS if k in score})
    return event, ("sec-v1", event_key)


def load_events(session, *, as_of, lookback):
    from persistence.models import event_history, event_provenance, event_versions, events
    window_start = as_of - lookback
    start_date = window_start.astimezone(EXCHANGE_TZ).date()
    v, e = event_versions, events
    available = sa.and_(e.c.source_family.in_(FAMILIES), v.c.observed_at <= as_of)
    in_window = sa.or_(v.c.published_at > window_start,
                       sa.and_(v.c.published_at.is_(None), v.c.publication_date >= start_date),
                       sa.and_(v.c.published_at.is_(None), v.c.publication_date.is_(None),
                               v.c.observed_at > window_start))
    event_ids = sa.select(v.c.event_id).join(e, e.c.id == v.c.event_id).where(available, in_window)
    query = (sa.select(v, e.c.source_family, e.c.identity_version, e.c.event_key)
             .join(e, e.c.id == v.c.event_id)
             .where(available, v.c.event_id.in_(event_ids)))
    candidates = [dict(r._mapping) for r in session.execute(query)]
    by_event = {}
    for row in candidates:
        by_event.setdefault(row["event_id"], []).append(row)
    chosen = sorted((max(rows, key=lambda r: (r["observed_at"], r["recorded_at"], r["id"])) for rows in by_event.values()),
                    key=lambda r: (r["source_family"], r["identity_version"], r["event_key"]))
    diagnostics = {family: dict(loaded=0, outside_lookback=0, no_provenance=0, passed=0) for family in FAMILIES}
    ids = [row["id"] for row in chosen]
    histories, provenance = {}, {}
    if ids:
        for h in session.execute(sa.select(event_history).where(
                event_history.c.event_version_id.in_(ids), event_history.c.kind.in_(("score", "decision")),
                event_history.c.recorded_at <= as_of)):
            h = dict(h._mapping)
            histories.setdefault((h["event_version_id"], h["kind"]), []).append(h)
        for p in session.execute(sa.select(event_provenance).where(
                event_provenance.c.event_version_id.in_(ids), event_provenance.c.retrieved_at <= as_of)):
            p = dict(p._mapping)
            provenance.setdefault(p["event_version_id"], []).append(p)
    inputs = []
    for version in chosen:
        family = version["source_family"]
        stats = diagnostics[family]
        stats["loaded"] += 1
        if _outside_lookback(version, window_start):
            stats["outside_lookback"] += 1
            continue
        prov = _latest(provenance.get(version["id"], []), key=lambda p: (p["retrieved_at"], p["provenance_key"]))
        if prov is None:
            stats["no_provenance"] += 1
            continue
        latest = {kind: _latest(histories.get((version["id"], kind), []), key=lambda h: (h["recorded_at"], h["content_hash"]))
                  for kind in ("score", "decision")}
        score = (latest["score"] or {}).get("attributes") or {}
        decision = (latest["decision"] or {}).get("attributes") or {}
        if family == "news":
            event, identity = _news_event(version, prov, score, decision)
        else:
            event, identity = _sec_event(version, prov, score, decision, version["event_key"])
        if identity != (version["identity_version"], version["event_key"]):
            raise IntegrityFailure(f"{family} event does not reproduce its durable identity")
        stats["passed"] += 1
        inputs.append(m.NewsInput(family=family, event=event, observed_at=version["observed_at"],
                                  collector_outcome=decision.get("collector_outcome") or "processed"))
    return EventLoad(tuple(inputs), diagnostics)


# --------------------------------------------------------------------------- orchestration

def load_sources(engine, *, symbol, as_of, lookback_hours, calendar):
    """(technical_rows, technical_diagnostics, EventLoad) from one read-only transaction."""
    from persistence.database import PersistenceError
    from persistence.news_audit import read_only
    if not 1 <= lookback_hours <= MAX_LOOKBACK_HOURS:
        raise InputError("news lookback hours out of range")
    try:
        with read_only(engine) as session:
            rows, technical = load_technical(session, symbol=symbol, as_of=as_of, calendar=calendar)
            events = load_events(session, as_of=as_of, lookback=timedelta(hours=lookback_hours))
    except PersistenceError:
        raise DatabaseFailure("database read failed") from None
    return rows, technical, events
