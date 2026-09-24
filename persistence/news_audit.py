"""Read-only News/RSS operational audits (Phase 2R); never merges, canonicalizes, rewrites or replays.

    python -m persistence.news_audit url-variants [--json] [--max-events N] [--batch-size N]
    python -m persistence.news_audit repeats      [--json] [--max-events N] [--batch-size N] [--all]

``url-variants`` groups durable news events whose canonical URLs share host and
path but differ in the rest (query string or fragment). Under ``news-url-v1`` such
URLs are separate durable articles by design; the audit is evidence for a future
product decision and changes nothing. ``repeats`` reports events observed again
after their first observation, with version and provenance counts. The schema
keeps only the first and last observation time per event, so the exact number of
repeat observations is reported as ``None`` rather than inferred.

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
from urllib.parse import urlsplit

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


def _news_events(session, max_events, batch_size):
    """Keyset-paged (first_seen_at, id) scan of news events with version/provenance facts; bounded."""
    _bounded("max_events", max_events, 100_000)
    _bounded("batch_size", batch_size, 5_000)
    scanned, last, truncated = [], None, False
    while True:
        query = sa.select(events.c.id, events.c.identity_version, events.c.first_seen_at, events.c.last_seen_at,
                          events.c.current_version_id).where(events.c.source_family == "news")
        if last is not None:
            query = query.where(sa.tuple_(events.c.first_seen_at, events.c.id) > sa.tuple_(
                sa.literal(last[0], type_=events.c.first_seen_at.type), sa.literal(last[1], type_=events.c.id.type)))
        page = session.execute(query.order_by(events.c.first_seen_at, events.c.id).limit(batch_size)).all()
        if not page:
            break
        if len(scanned) == max_events:  # Bound reached and more events exist.
            truncated = True
            break
        if len(scanned) + len(page) > max_events:
            page, truncated = page[:max_events - len(scanned)], True
        last = (page[-1].first_seen_at, page[-1].id)
        ids = [row.id for row in page]
        facts = {row.id: dict(versions=0, provenance=0, publishers=set(), url=None) for row in page}
        for row in session.execute(sa.select(event_versions.c.event_id, event_versions.c.id, event_versions.c.publisher,
                                             event_versions.c.canonical_url)
                                   .where(event_versions.c.event_id.in_(ids))).mappings():
            entry = facts[row["event_id"]]
            entry["versions"] += 1
            entry["publishers"].add(row["publisher"])
            if row["id"] in {r.current_version_id for r in page}:
                entry["url"] = row["canonical_url"]
        for row in session.execute(sa.select(event_versions.c.event_id, sa.func.count(event_provenance.c.id))
                                   .join(event_provenance, event_provenance.c.event_version_id == event_versions.c.id)
                                   .where(event_versions.c.event_id.in_(ids)).group_by(event_versions.c.event_id)).all():
            facts[row[0]]["provenance"] = row[1]
        for row in page:
            entry = facts[row.id]
            scanned.append(dict(event_row_id=row.id, identity_version=row.identity_version, canonical_url=entry["url"],
                                publishers=sorted(p for p in entry["publishers"] if p), versions=entry["versions"],
                                provenance=entry["provenance"], first_seen=row.first_seen_at, last_seen=row.last_seen_at))
        if truncated or len(page) < batch_size:
            break
    return scanned, truncated


def audit_url_variants(session, *, max_events=10_000, batch_size=500):
    """Host+path groups with more than one distinct durable URL; deterministic, never merged."""
    scanned, truncated = _news_events(session, max_events, batch_size)
    groups = {}
    for item in scanned:
        if not item["canonical_url"]:
            continue  # Link-less fallback identities have no URL to compare.
        parts = urlsplit(item["canonical_url"].strip())
        key = ((parts.hostname or "").lower(), parts.path)
        groups.setdefault(key, []).append(item)
    result = []
    for (host, path), members in sorted(groups.items()):
        if len({m["canonical_url"].strip().lower() for m in members}) < 2:
            continue
        members = sorted(members, key=lambda m: (m["canonical_url"], m["event_row_id"]))
        result.append(dict(host=host, path=path, count=len(members), urls=[m["canonical_url"] for m in members],
                           event_ids=[m["event_row_id"] for m in members],
                           publishers=sorted({p for m in members for p in m["publishers"]}),
                           first_observed=_as_text(min(m["first_seen"] for m in members)),
                           last_observed=_as_text(max(m["last_seen"] for m in members))))
    return dict(events_scanned=len(scanned), truncated=truncated, variant_group_count=len(result),
                note="separate durable events under news-url-v1; no canonicalization or merge", groups=result)


def audit_repeat_observations(session, *, max_events=10_000, batch_size=500, repeated_only=True):
    """Durable first/last observation per news event plus version/provenance counts."""
    scanned, truncated = _news_events(session, max_events, batch_size)
    items = []
    repeated = 0
    for item in scanned:
        is_repeat = item["last_seen"] > item["first_seen"]
        repeated += int(is_repeat)
        if repeated_only and not is_repeat:
            continue
        items.append(dict(event_row_id=item["event_row_id"], identity_version=item["identity_version"],
                          canonical_url=item["canonical_url"], versions=item["versions"], provenance=item["provenance"],
                          first_observed=_as_text(item["first_seen"]), last_observed=_as_text(item["last_seen"]),
                          span_seconds=int((item["last_seen"] - item["first_seen"]).total_seconds()),
                          repeat_observed=is_repeat, observation_count=None, duplicate_count=None))
    items.sort(key=lambda i: (i["first_observed"], i["event_row_id"]))
    return dict(events_scanned=len(scanned), repeated_events=repeated, truncated=truncated, note=REPEAT_NOTE, events=items)


@contextmanager
def read_only(engine):
    with transaction(engine) as session:
        if engine.dialect.name == "postgresql":
            session.execute(sa.text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
        yield session


def main(argv=None, *, engine=None, out=None, err=None):
    out, err = out or sys.stdout, err or sys.stderr
    parser = argparse.ArgumentParser(prog="python -m persistence.news_audit", description="Read-only News audits.")
    sub = parser.add_subparsers(dest="command", required=True)
    variants, repeats = sub.add_parser("url-variants"), sub.add_parser("repeats")
    repeats.add_argument("--all", action="store_true", help="include events observed only once")
    for command in (variants, repeats):
        command.add_argument("--json", action="store_true")
        command.add_argument("--max-events", type=int, default=10_000)
        command.add_argument("--batch-size", type=int, default=500)
    try:
        args = parser.parse_args(argv)
    except SystemExit as exit_:
        return 2 if exit_.code else 0
    owned = engine is None
    try:
        if owned:
            engine = make_engine(replace(DatabaseSettings.from_env(), application_name="mias_news_audit"))
        with read_only(engine) as session:
            if args.command == "url-variants":
                result = audit_url_variants(session, max_events=args.max_events, batch_size=args.batch_size)
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
        key = "variant_group_count" if args.command == "url-variants" else "repeated_events"
        print(f"[{args.command}] {key}: {result[key]}", file=out)
        for item in result.get("groups") or result.get("events"):
            print(json.dumps(item, sort_keys=True, default=str), file=out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
