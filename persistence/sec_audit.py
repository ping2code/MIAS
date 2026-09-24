"""Read-only SEC operational audits (Phase 2P); never merges, rewrites or replays.

    python -m persistence.sec_audit shared-accessions [--json] [--max-groups N] [--batch-size N]
    python -m persistence.sec_audit repeats           [--json] [--max-events N] [--batch-size N] [--all]

``shared-accessions`` lists accession numbers recorded under more than one durable
SEC event (the collector's identity is symbol-specific, so one filing seen under
two watchlist issuers is two runtime identities). ``repeats`` reports durable
events observed again after their first observation (Redis TTL expiry, lost Redis
state or Redis fail-open). The schema keeps only the first and last observation
time per event; the exact number of repeat observations is not stored durably, so
it is reported as ``None`` rather than inferred (process-local shadow counters
carry the per-process ``duplicate`` count).

The database comes from the process environment (``DatabaseSettings.from_env``);
``.env`` is never read and ``shared.config`` is never imported. Every query runs
in a read-only transaction on PostgreSQL. Exit codes: 0 ok, 2 usage, 3 database
unavailable or not configured. Credentials are never printed.
"""
import argparse
from contextlib import contextmanager
from dataclasses import replace
import json
import sys

import sqlalchemy as sa

from persistence.config import ConfigurationError, DatabaseSettings
from persistence.database import make_engine, transaction
from persistence.models import events, event_versions, event_provenance

REPEAT_NOTE = "exact repeat count not stored durably; see process-local shadow duplicate counters"


def _bounded(name, value, upper):
    if type(value) is not int or not 1 <= value <= upper:
        raise ValueError(f"{name} must be between 1 and {upper}")


def _as_text(value):
    return None if value is None else value.isoformat() if hasattr(value, "isoformat") else str(value)


def _sec_rows():
    """Version-level rows for SEC events joined with their accession provenance."""
    return (sa.select(event_provenance.c.document_id.label("accession"), events.c.id.label("event_row_id"),
                      events.c.event_key, events.c.current_version_id, event_versions.c.id.label("version_id"),
                      event_versions.c.canonical_url, event_versions.c.attributes)
            .join(event_versions, event_versions.c.id == event_provenance.c.event_version_id)
            .join(events, events.c.id == event_versions.c.event_id)
            .where(events.c.source_family == "sec", event_provenance.c.document_id.is_not(None)))


def audit_shared_accessions(session, *, max_groups=1000, batch_size=500):
    """Accessions under more than one durable SEC event; keyset-paged by accession, deterministic."""
    _bounded("max_groups", max_groups, 100_000)
    _bounded("batch_size", batch_size, 5_000)
    rows = _sec_rows().subquery()
    groups, last, truncated, scanned = [], None, False, 0
    while True:
        query = (sa.select(rows.c.accession).group_by(rows.c.accession)
                 .having(sa.func.count(sa.distinct(rows.c.event_row_id)) > 1).order_by(rows.c.accession))
        if last is not None:
            query = query.where(rows.c.accession > last)
        page = session.execute(query.limit(batch_size)).scalars().all()
        if not page:
            break
        if len(groups) == max_groups:  # Bound reached and more groups exist.
            truncated = True
            break
        if len(groups) + len(page) > max_groups:
            page, truncated = page[:max_groups - len(groups)], True
        last = page[-1]
        detail = session.execute(sa.select(rows).where(rows.c.accession.in_(page))
                                 .order_by(rows.c.accession, rows.c.event_key, rows.c.version_id)).mappings().all()
        by_accession = {}
        for row in detail:
            scanned += 1
            members = by_accession.setdefault(row["accession"], {})
            member = members.setdefault(row["event_row_id"], dict(
                event_row_id=row["event_row_id"], event_key=row["event_key"], symbols=[], urls=[], forms=[]))
            attrs = row["attributes"] or {}
            for key, value in (("symbols", attrs.get("symbols") or []), ("urls", [row["canonical_url"]]),
                               ("forms", [attrs.get("sec_form")])):
                member[key] = sorted({*member[key], *(v for v in value if v is not None)})
        for accession in page:
            members = sorted(by_accession[accession].values(), key=lambda m: m["event_key"])
            groups.append(dict(accession=accession, count=len(members),
                               event_ids=[m["event_row_id"] for m in members],
                               event_keys=[m["event_key"] for m in members],
                               symbols=sorted({s for m in members for s in m["symbols"]}),
                               urls=sorted({u for m in members for u in m["urls"]}),
                               forms=sorted({f for m in members for f in m["forms"]}), members=members))
        if truncated or len(page) < batch_size:
            break
    return dict(shared_accession_count=len(groups), truncated=truncated, rows_scanned=scanned, groups=groups)


def audit_repeat_observations(session, *, max_events=10_000, batch_size=500, repeated_only=True):
    """Durable first/last observation per SEC event; keyset-paged by (first_seen_at, id)."""
    _bounded("max_events", max_events, 100_000)
    _bounded("batch_size", batch_size, 5_000)
    items, scanned, repeated, last, truncated = [], 0, 0, None, False
    while True:
        query = sa.select(events.c.id, events.c.event_key, events.c.first_seen_at, events.c.last_seen_at,
                          events.c.current_version_id).where(events.c.source_family == "sec")
        if last is not None:
            query = query.where(sa.tuple_(events.c.first_seen_at, events.c.id) > sa.tuple_(
                sa.literal(last[0], type_=events.c.first_seen_at.type), sa.literal(last[1], type_=events.c.id.type)))
        page = session.execute(query.order_by(events.c.first_seen_at, events.c.id).limit(batch_size)).all()
        if not page:
            break
        if scanned == max_events:  # Bound reached and more events exist.
            truncated = True
            break
        if scanned + len(page) > max_events:
            page, truncated = page[:max_events - scanned], True
        last = (page[-1].first_seen_at, page[-1].id)
        scanned += len(page)
        ids = [row.id for row in page]
        facts = {}
        for row in session.execute(sa.select(event_versions.c.event_id, event_versions.c.id,
                                             event_versions.c.attributes, event_provenance.c.document_id)
                                   .join(event_provenance, event_provenance.c.event_version_id == event_versions.c.id,
                                         isouter=True)
                                   .where(event_versions.c.event_id.in_(ids))
                                   .order_by(event_versions.c.event_id, event_versions.c.id)).mappings():
            entry = facts.setdefault(row["event_id"], dict(versions=set(), accessions=set(), forms=set()))
            entry["versions"].add(row["id"])
            if row["document_id"]:
                entry["accessions"].add(row["document_id"])
            entry["forms"].add((row["attributes"] or {}).get("sec_form"))
        for row in page:
            is_repeat = row.last_seen_at > row.first_seen_at
            repeated += int(is_repeat)
            if repeated_only and not is_repeat:
                continue
            entry = facts.get(row.id, dict(versions=set(), accessions=set(), forms=set()))
            items.append(dict(accession=",".join(sorted(entry["accessions"])) or None, event_row_id=row.id,
                              event_key=row.event_key, forms=sorted(f for f in entry["forms"] if f),
                              versions=len(entry["versions"]), repeat_observed=is_repeat,
                              first_observed=_as_text(row.first_seen_at), last_observed=_as_text(row.last_seen_at),
                              span_seconds=int((row.last_seen_at - row.first_seen_at).total_seconds()),
                              observation_count=None, duplicate_count=None))
        if truncated or len(page) < batch_size:
            break
    items.sort(key=lambda i: (i["first_observed"], i["event_key"]))
    return dict(events_scanned=scanned, repeated_events=repeated, truncated=truncated, note=REPEAT_NOTE, events=items)


@contextmanager
def read_only(engine):
    with transaction(engine) as session:
        if engine.dialect.name == "postgresql":
            session.execute(sa.text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
        yield session


def main(argv=None, *, engine=None, out=None, err=None):
    out, err = out or sys.stdout, err or sys.stderr
    parser = argparse.ArgumentParser(prog="python -m persistence.sec_audit", description="Read-only SEC audits.")
    sub = parser.add_subparsers(dest="command", required=True)
    shared = sub.add_parser("shared-accessions")
    shared.add_argument("--max-groups", type=int, default=1000)
    repeats = sub.add_parser("repeats")
    repeats.add_argument("--max-events", type=int, default=10_000)
    repeats.add_argument("--all", action="store_true", help="include events observed only once")
    for command in (shared, repeats):
        command.add_argument("--json", action="store_true")
        command.add_argument("--batch-size", type=int, default=500)
    try:
        args = parser.parse_args(argv)
    except SystemExit as exit_:
        return 2 if exit_.code else 0
    owned = engine is None
    try:
        if owned:
            engine = make_engine(replace(DatabaseSettings.from_env(), application_name="mias_sec_audit"))
        with read_only(engine) as session:
            if args.command == "shared-accessions":
                result = audit_shared_accessions(session, max_groups=args.max_groups, batch_size=args.batch_size)
            else:
                result = audit_repeat_observations(session, max_events=args.max_events, batch_size=args.batch_size,
                                                   repeated_only=not args.all)
    except ConfigurationError as error:  # A ValueError subclass: must be handled first.
        print("database not configured: " + str(error)[:200], file=err)  # Credential-free by construction.
        return 3
    except ValueError as error:
        print("usage error: " + str(error)[:200], file=err)
        return 2
    except Exception as error:
        print(f"database unavailable or operation failed ({type(error).__name__})", file=err)
        return 3
    finally:
        if owned and engine is not None:
            engine.dispose()
    if args.json:
        print(json.dumps(result, sort_keys=True, indent=2, default=str), file=out)
    else:
        key = "shared_accession_count" if args.command == "shared-accessions" else "repeated_events"
        print(f"[{args.command}] {key}: {result[key]}", file=out)
        for item in result.get("groups") or result.get("events"):
            print(json.dumps(item, sort_keys=True, default=str), file=out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
